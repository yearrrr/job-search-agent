"""待编辑草稿与已确认材料是独立表，绝不进入事实检索。"""

import json
from contextlib import nullcontext

from ..schemas import Conflict, InputError, bounded
from .common import now, once, uid
from .tasks import dump, ensure_fresh, row_task


def render_draft(snapshot, sections, evidence, analysis):
    lines = [
        f"# {snapshot['title']} · 求职材料草稿",
        "",
        "【虚构测试材料】" if snapshot["corpus"] == "demo" else "【待人工核对材料】",
        "",
    ]
    for section in sections:
        lines += [f"## {section['kind']}", ""]
        for fact_id in section["fact_ids"]:
            fact = evidence[fact_id]
            lines += [fact["text"], f"来源：{fact_id} · 修订 {fact['revision']}", ""]
        if not section["fact_ids"]:
            lines += ["当前没有可用于此部分的已确认事实，请补充并确认后重新生成。", ""]
    lines += ["## 修改依据与待核实项", ""]
    for section in sections:
        lines += [f"- {section['kind']}建议：{section['rationale']}"]
    reqs = {r["id"]: r for r in snapshot["requirements"]}
    for item in analysis:
        if item["status"] == "unknown":
            lines += [f"- 待核实：{reqs[item['requirement_id']]['text']}"]
        elif item["status"] == "mismatch":
            lines += [f"- 已发现差异：{reqs[item['requirement_id']]['text']}"]
    return "\n".join(lines).strip()


class Drafts:
    def __init__(self, database):
        self.db = database

    def initialize(self, task_id, content):
        with self.db.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO draft_edits VALUES (?,1,?,?)", (task_id, content, now())
            )

    def edit(self, task_id, revision, content, operation_id, *, connection=None):
        content = bounded(content, 30000)
        owned = connection is None
        with self.db.connect() if owned else nullcontext(connection) as connection:
            if owned:
                connection.execute("BEGIN IMMEDIATE")

            def save():
                task = row_task(connection, task_id)
                if (
                    task["status"] != "waiting_confirmation"
                    or json.loads(task["view_json"]).get("wait", {}).get("kind") != "draft"
                ):
                    raise Conflict("当前草稿不可编辑，请刷新任务页面。")
                current = connection.execute(
                    "SELECT * FROM draft_edits WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not current or current["revision"] != revision:
                    raise Conflict("草稿已有更新，请刷新后编辑。")
                if current["content"] != content:
                    connection.execute(
                        "INSERT INTO draft_edits VALUES (?,?,?,?)",
                        (task_id, revision + 1, content, now()),
                    )
                return {"id": task_id, "revision": revision + int(current["content"] != content)}

            return once(connection, operation_id, ["draft.edit", task_id, revision, content], save)

    def confirm(self, task_id, revision, snapshot, metadata):
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT id FROM material_versions WHERE task_id=?", (task_id,)
            ).fetchone()
            if previous:
                return previous["id"]
            ensure_fresh(connection, snapshot)
            current = connection.execute(
                "SELECT * FROM draft_edits WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if not current or current["revision"] != revision:
                raise Conflict("草稿版本已变化，请刷新后确认。")
            version = connection.execute(
                "SELECT coalesce(max(version),0)+1 FROM material_versions WHERE job_id=?",
                (snapshot["job_id"],),
            ).fetchone()[0]
            material_id = uid()
            connection.execute(
                "INSERT INTO material_versions VALUES (?,?,?,?,?,?,?)",
                (
                    material_id,
                    task_id,
                    snapshot["job_id"],
                    version,
                    current["content"],
                    dump(
                        {
                            "snapshot": snapshot,
                            "draft_revision": revision,
                            "user_edited": revision > 1,
                            **metadata,
                        }
                    ),
                    now(),
                ),
            )
            return material_id

    def detail(self, material_id):
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM material_versions WHERE id=?", (material_id,)
            ).fetchone()
            if not row:
                raise InputError("材料版本不存在。")
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json"))
            return result

    def list_for_job(self, job_id):
        with self.db.connect() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    "SELECT id,version,task_id,created_at FROM material_versions WHERE job_id=? ORDER BY version DESC",
                    (job_id,),
                )
            ]
