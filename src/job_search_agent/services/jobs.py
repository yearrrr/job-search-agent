import json
import re
from contextlib import nullcontext

from ..schemas import (
    FIELD_LABELS,
    STATUS_LABELS,
    Conflict,
    InputError,
    bounded,
    valid_corpus,
    valid_date,
)
from .common import digest, get_row, now, once, uid

ALIASES = {
    "岗位职责": "responsibilities",
    "工作职责": "responsibilities",
    "职责": "responsibilities",
    "任职要求": "requirements",
    "硬性条件": "requirements",
    "基本要求": "requirements",
    "加分项": "nice_to_have",
    "优先条件": "nice_to_have",
    "工作地点": "location",
    "地点": "location",
    "薪资": "salary",
    "截止日期": "deadline",
    "投递截止": "deadline",
}


def extract_fields(raw_jd):
    """规则只归类明确小标题下的原句；无法识别的段落留在原文中供人工校对。"""
    fields = {key: {"text": "", "sources": [], "method": "local"} for key in FIELD_LABELS}
    section = None
    for number, line in enumerate(raw_jd.splitlines(), 1):
        cleaned = line.strip().lstrip("#").strip()
        if not cleaned:
            section = None
            continue
        left, separator, right = re.match(r"^([^：:]+)([：:]?)(.*)$", cleaned).groups()
        if left.strip() in ALIASES:
            section = ALIASES[left.strip()]
            cleaned = right.strip() if separator else ""
        if section and cleaned:
            fields[section]["text"] += ("\n" if fields[section]["text"] else "") + cleaned
            fields[section]["sources"].append({"line": number, "quote": line})
            if section in ("location", "salary", "deadline"):
                section = None
    return fields


def normalize(text):
    return re.sub(r"\s+", "", text).casefold()


class Jobs:
    def __init__(self, database):
        self.db = database

    def create(self, job, operation_id, *, fields=None, connection=None):
        data = job.model_dump()
        fingerprint = digest(
            [normalize(data[k]) for k in ("company", "title", "job_code", "batch", "raw_jd")]
        )
        with nullcontext(connection) if connection is not None else self.db.connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")

            def save():
                existing = connection.execute(
                    "SELECT id FROM jobs WHERE corpus=? AND fingerprint=?",
                    (job.corpus, fingerprint),
                ).fetchone()
                if existing:
                    return {"id": existing["id"], "duplicate": True}
                job_id, timestamp = uid(), now()
                encoded_fields = json.dumps(
                    fields if fields is not None else extract_fields(job.raw_jd), ensure_ascii=False
                )
                connection.execute(
                    """INSERT INTO jobs(id,corpus,company,title,job_code,batch,source_url,
                    raw_jd,fingerprint,fields_json,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'saved',?,?)""",
                    (
                        job_id,
                        job.corpus,
                        job.company,
                        job.title,
                        job.job_code,
                        job.batch,
                        job.source_url,
                        job.raw_jd,
                        fingerprint,
                        encoded_fields,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO job_revisions VALUES (?,?,?,?,?)",
                    (uid(), job_id, 1, encoded_fields, timestamp),
                )
                connection.execute(
                    "INSERT INTO job_history VALUES (?,?,?,?,?)",
                    (uid(), job_id, None, "saved", timestamp),
                )
                return {"id": job_id, "duplicate": False}

            return once(connection, operation_id, ["create_job", data], save)

    def list(self, corpus):
        valid_corpus(corpus)
        with self.db.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM jobs WHERE corpus=? ORDER BY updated_at DESC,id", (corpus,)
                )
            ]

    def detail(self, job_id):
        with self.db.connect() as connection:
            job = get_row(connection, "jobs", job_id)
            job["fields"] = json.loads(job["fields_json"])
            source = connection.execute(
                "SELECT run_id FROM jd_confirmations WHERE job_id=? LIMIT 1", (job_id,)
            ).fetchone()
            job["jd_run_id"] = source["run_id"] if source else None
            job["history"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM job_history WHERE job_id=? ORDER BY created_at,rowid", (job_id,)
                )
            ]
            job["notes"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM job_notes WHERE job_id=? ORDER BY created_at DESC", (job_id,)
                )
            ]
            job["todos"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM todos WHERE job_id=? ORDER BY done,due_date,created_at",
                    (job_id,),
                )
            ]
            job["related"] = [
                dict(r)
                for r in connection.execute(
                    """SELECT id,title,job_code,batch FROM jobs WHERE corpus=? AND company=? AND id<>?
                   AND (title=? OR (job_code<>'' AND job_code=?)) ORDER BY created_at""",
                    (job["corpus"], job["company"], job_id, job["title"], job["job_code"]),
                )
            ]
            return job

    def update_fields(self, job_id, values, revision, operation_id):
        if set(values) != set(FIELD_LABELS):
            raise InputError("岗位字段不完整，请刷新页面。")
        values = {k: bounded(v, 12000, empty=True) for k, v in values.items()}
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def update():
                job = get_row(connection, "jobs", job_id)
                if job["revision"] != revision:
                    raise Conflict("岗位已更新，请刷新后再修改。")
                fields = json.loads(job["fields_json"])
                for key, value in values.items():
                    if value != fields[key]["text"]:
                        fields[key]["text"] = value
                        fields[key]["method"] = "user_edit"
                encoded, timestamp = json.dumps(fields, ensure_ascii=False), now()
                connection.execute(
                    "UPDATE jobs SET fields_json=?,revision=?,updated_at=? WHERE id=?",
                    (encoded, revision + 1, timestamp, job_id),
                )
                connection.execute(
                    "INSERT INTO job_revisions VALUES (?,?,?,?,?)",
                    (uid(), job_id, revision + 1, encoded, timestamp),
                )
                return {"id": job_id}

            return once(connection, operation_id, ["job_fields", job_id, values, revision], update)

    def change_status(self, job_id, status, revision, operation_id):
        if status not in STATUS_LABELS:
            raise InputError("岗位状态无效。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def update():
                job = get_row(connection, "jobs", job_id)
                if job["revision"] != revision:
                    raise Conflict("岗位已更新，请刷新后核对当前状态。")
                if job["status"] != status:
                    timestamp = now()
                    connection.execute(
                        "UPDATE jobs SET status=?,revision=?,updated_at=? WHERE id=?",
                        (status, revision + 1, timestamp, job_id),
                    )
                    connection.execute(
                        "INSERT INTO job_history VALUES (?,?,?,?,?)",
                        (uid(), job_id, job["status"], status, timestamp),
                    )
                return {
                    "id": job_id,
                    "changed": job["status"] != status,
                    "status": status,
                    "revision": revision + int(job["status"] != status),
                }

            return once(connection, operation_id, ["job_status", job_id, status, revision], update)

    def add_note_or_todo(self, job_id, text, due_date, kind, operation_id):
        text = bounded(text, 2000)
        due_date = valid_date(due_date)
        if kind not in ("note", "todo"):
            raise InputError("记录类型无效。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def add():
                get_row(connection, "jobs", job_id)
                if kind == "note":
                    connection.execute(
                        "INSERT INTO job_notes VALUES (?,?,?,?)", (uid(), job_id, text, now())
                    )
                else:
                    connection.execute(
                        "INSERT INTO todos(id,job_id,text,due_date,created_at) VALUES (?,?,?,?,?)",
                        (uid(), job_id, text, due_date, now()),
                    )
                return {"id": job_id}

            return once(connection, operation_id, [kind, job_id, text, due_date], add)

    def set_todo(self, todo_id, done, revision, operation_id):
        if type(done) is not bool:
            raise InputError("待办状态无效。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def update():
                todo = get_row(connection, "todos", todo_id)
                if todo["revision"] != revision:
                    raise Conflict("待办已被修改，请刷新。")
                connection.execute(
                    "UPDATE todos SET done=?,revision=? WHERE id=?",
                    (int(done), revision + 1, todo_id),
                )
                return {"id": todo["job_id"]}

            return once(connection, operation_id, ["todo_done", todo_id, done, revision], update)
