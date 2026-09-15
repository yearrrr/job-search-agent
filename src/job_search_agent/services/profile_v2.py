"""第二版资料入口：原件、页面证据、完整经历与原地确认。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import Conflict, InputError, bounded, valid_corpus
from .common import get_row, now, once, uid
from .documents import MAX_FILE_BYTES, Documents, insert_fact, parse_file
from .tasks import task_lock

VISUAL_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAIN_CATEGORIES = ("project", "experience", "skill", "education", "award", "publication")


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    category: Literal[
        "project", "experience", "skill", "education", "award", "publication", "basic", "other"
    ]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    period: str = Field(default="", max_length=100)
    organization: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    technologies: list[str] = Field(default_factory=list, max_length=25)
    review_notes: str = Field(default="", max_length=600)
    source_ids: list[str] = Field(min_length=1, max_length=30)

    def fact_text(self):
        pieces = [bounded(self.title, 200)]
        for label, value in (
            ("时间", self.period),
            ("学校 / 单位", self.organization),
            ("角色", self.role),
        ):
            if value.strip():
                pieces.append(f"{label}：{value.strip()}")
        if self.technologies:
            pieces.append("技术 / 技能：" + "、".join(bounded(t, 80) for t in self.technologies))
        pieces.append(bounded(self.description, 4000))
        return bounded("\n".join(pieces), 4000)

    def metadata(self):
        return self.model_dump(exclude={"title", "category", "source_ids"})


class ParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    entries: list[Entry] = Field(max_length=200)
    notes: str = Field(default="", max_length=1500)


class ProfileV2:
    def __init__(self, database):
        self.db = database
        self.documents = Documents(database)

    def import_file(self, name, content, kind, corpus, operation_id):
        corpus = valid_corpus(corpus)
        if kind not in ("resume", "project"):
            raise InputError("此入口只接收简历或项目经历，学习笔记请使用原入口。")
        name = bounded(name, 180)
        if name != PureWindowsPath(name).name or "/" in name or ":" in name:
            raise InputError("文件名不能包含路径。")
        suffix = Path(name).suffix.lower()
        if suffix not in VISUAL_SUFFIXES | {".txt", ".md"}:
            raise InputError("支持 PDF、PNG、JPG、WEBP、UTF-8 TXT 和 Markdown。")
        if not content or len(content) > MAX_FILE_BYTES:
            raise InputError("文件不能为空，大小不能超过 5 MB。")
        checksum = hashlib.sha256(content).hexdigest()
        # 纯文本只保留行与定位；所有语义判断由 LLM 完成。
        snippets = [] if suffix in VISUAL_SUFFIXES else parse_file(content, suffix)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")

            def save():
                previous = c.execute(
                    "SELECT id FROM documents WHERE corpus=? AND kind=? AND sha256=?",
                    (corpus, kind, checksum),
                ).fetchone()
                if previous:
                    return {"id": previous["id"], "duplicate": True}
                document_id = uid()
                path = self.documents.original_path(document_id, suffix)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(content)
                c.execute(
                    """INSERT INTO documents VALUES (?,?,?,?,?,?,?,'parsed',NULL,?)""",
                    (document_id, name, kind, corpus, checksum, suffix, len(content), now()),
                )
                c.execute("INSERT INTO v2_documents VALUES (?,?,?)", (document_id, suffix, now()))
                for s in snippets:
                    c.execute(
                        "INSERT INTO snippets VALUES (?,?,?,?,?)",
                        (s["id"], document_id, s["page"], s["line"], s["text"]),
                    )
                return {"id": document_id, "duplicate": False}

            return once(c, operation_id, ["v2-import", name, kind, corpus, checksum], save)

    def ensure_pages(self, document_id):
        with task_lock(self.db.data_dir, document_id) as acquired:
            if not acquired:
                raise Conflict("原件正在转换，请稍后继续解析。")
            doc = self.documents.detail(document_id)
            path = self.documents.original_path(document_id, doc["suffix"])
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != doc["sha256"]:
                raise InputError("原件校验失败，请检查备份；本次未发送模型请求。")
            if doc["suffix"] not in VISUAL_SUFFIXES:
                return
            with self.db.connect() as c:
                if c.execute(
                    "SELECT 1 FROM document_pages WHERE document_id=?", (document_id,)
                ).fetchone():
                    return
            # 独立目录避免中断留下的半成品被下一次复用。
            directory = self.db.local_path("page-images", document_id, uid())
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "utf8",
                        "-m",
                        "job_search_agent.services.visual_worker",
                        doc["suffix"],
                        str(directory),
                    ],
                    input=content,
                    capture_output=True,
                    timeout=35,
                    check=False,
                )
                payload = json.loads(result.stdout)
                if result.returncode or not payload.get("ok") or not payload.get("pages"):
                    raise ValueError
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise InputError(
                    "原件无法转换。PDF 限 12 页，图片限 2400 万像素；请检查文件或粘贴文本。"
                ) from None
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                for p in payload["pages"]:
                    page, source_id = int(p["page"]), uid()
                    image = (directory / p["file"]).resolve()
                    if not image.is_relative_to(directory) or not image.is_file():
                        raise InputError("页面文件无效。")
                    c.execute(
                        "INSERT INTO snippets VALUES (?,?,?,?,?)",
                        (source_id, document_id, page, 0, f"原件第 {page} 页图像"),
                    )
                    c.execute(
                        "INSERT INTO document_pages VALUES (?,?,?,?)",
                        (document_id, page, source_id, str(image.relative_to(self.db.data_dir))),
                    )

    def detail(self, document_id):
        doc = self.documents.detail(document_id)
        with self.db.connect() as c:
            doc["has_llm_entries"] = bool(
                c.execute(
                    """SELECT 1 FROM profile_entry_details e JOIN facts f ON f.id=e.fact_id
                WHERE f.document_id=? AND e.parse_job_id IS NOT NULL LIMIT 1""",
                    (document_id,),
                ).fetchone()
            )
            doc["pages"] = [
                dict(r)
                for r in c.execute(
                    "SELECT page,snippet_id FROM document_pages WHERE document_id=? ORDER BY page",
                    (document_id,),
                )
            ]
            for f in doc["facts"]:
                details = c.execute(
                    "SELECT * FROM profile_entry_details WHERE fact_id=?", (f["id"],)
                ).fetchone()
                f["title"] = details["title"] if details else f["text"].splitlines()[0][:200]
                f["metadata"] = (
                    json.loads(details["metadata_json"])
                    if details
                    else {
                        "description": f["text"],
                        "period": "",
                        "organization": "",
                        "role": "",
                        "technologies": [],
                        "review_notes": "",
                    }
                )
                f["hidden_legacy"] = (
                    doc["has_llm_entries"] and f["method"] == "local" and f["status"] == "pending"
                )
        doc["legacy_pending_count"] = sum(
            f["method"] == "local" and f["status"] == "pending" for f in doc["facts"]
        )
        doc["counts"] = {
            s: sum(f["status"] == s for f in doc["facts"])
            for s in ("pending", "confirmed", "rejected")
        }
        return doc

    def page_path(self, document_id, page):
        with self.db.connect() as c:
            row = c.execute(
                "SELECT image_path FROM document_pages WHERE document_id=? AND page=?",
                (document_id, page),
            ).fetchone()
        if not row:
            raise InputError("页面尚未生成。")
        return self.db.local_path(row["image_path"])

    def update_entries(self, document_id, items, operation_id):
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise InputError("请选择 1–200 条经历。")
        prepared, identifiers = [], set()
        for item in items:
            fact_id = item["id"]
            if fact_id in identifiers:
                raise InputError("同一条经历不能重复提交。")
            identifiers.add(fact_id)
            status, revision = item["status"], item["revision"]
            if status not in ("pending", "confirmed", "rejected") or type(revision) is not int:
                raise InputError("确认状态或版本号无效。")
            # 来源绑定数据库；浏览器不能重新指定其他文档的来源。
            entry = Entry.model_validate({**item["entry"], "source_ids": ["bound-by-server"]})
            prepared.append((fact_id, revision, status, entry, entry.fact_text()))
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")

            def save():
                doc = get_row(c, "documents", document_id)
                if doc["kind"] == "notes":
                    raise InputError("学习笔记不能作为个人经历。")
                output = []
                for fact_id, revision, status, entry, text in prepared:
                    f = get_row(c, "facts", fact_id)
                    if f["document_id"] != document_id:
                        raise InputError("经历不属于当前文档。")
                    if f["revision"] != revision:
                        raise Conflict("经历已在其他页面修改。本次全部未保存，请打开最新版本核对。")
                    previous = c.execute(
                        "SELECT title,metadata_json FROM profile_entry_details WHERE fact_id=?",
                        (fact_id,),
                    ).fetchone()
                    if (
                        previous
                        and previous["title"] == entry.title
                        and json.loads(previous["metadata_json"]) == entry.metadata()
                        and (f["text"], f["category"], f["status"])
                        == (text, entry.category, status)
                    ):
                        output.append({"id": fact_id, "revision": revision, "status": status})
                        continue
                    timestamp = now()
                    c.execute(
                        """UPDATE facts SET text=?,category=?,status=?,revision=revision+1,
                        method='user_edit',confirmed_at=?,updated_at=? WHERE id=?""",
                        (
                            text,
                            entry.category,
                            status,
                            timestamp if status == "confirmed" else None,
                            timestamp,
                            fact_id,
                        ),
                    )
                    c.execute(
                        "INSERT INTO fact_history VALUES (?,?,?,?,?,?,?,?)",
                        (
                            uid(),
                            fact_id,
                            revision + 1,
                            text,
                            entry.category,
                            status,
                            "user_edit",
                            timestamp,
                        ),
                    )
                    metadata = json.dumps(entry.metadata(), ensure_ascii=False)
                    c.execute(
                        """INSERT INTO profile_entry_details VALUES (?,?,?,NULL)
                        ON CONFLICT(fact_id) DO UPDATE SET
                        title=excluded.title,metadata_json=excluded.metadata_json""",
                        (fact_id, entry.title, metadata),
                    )
                    c.execute(
                        "INSERT INTO profile_entry_history VALUES (?,?,?,?)",
                        (fact_id, revision + 1, entry.title, metadata),
                    )
                    output.append({"id": fact_id, "revision": revision + 1, "status": status})
                return {"entries": output}

            return once(c, operation_id, ["v2-review", document_id, items], save)

    def add_manual(self, corpus, values, operation_id):
        valid_corpus(corpus)
        entry = Entry.model_validate({**values, "source_ids": ["manual"]})
        text = entry.fact_text()
        # 补充内容先以原始文本保存，仍在核对页由用户确认。
        result = self.import_file(
            bounded(entry.title, 150).replace("/", "-").replace("\\", "-").replace(":", "-")
            + ".txt",
            text.encode(),
            "project",
            corpus,
            operation_id,
        )
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM facts WHERE document_id=?", (result["id"],)).fetchone():
                sources = [
                    r["id"]
                    for r in c.execute(
                        "SELECT id FROM snippets WHERE document_id=? ORDER BY line", (result["id"],)
                    )
                ]
                fact_id = insert_fact(
                    c, result["id"], sources[0], entry.category, text, "user_edit", sources
                )
                metadata = json.dumps(entry.metadata(), ensure_ascii=False)
                c.execute(
                    "INSERT INTO profile_entry_details VALUES (?,?,?,NULL)",
                    (fact_id, entry.title, metadata),
                )
                c.execute(
                    "INSERT INTO profile_entry_history VALUES (?,?,?,?)",
                    (fact_id, 1, entry.title, metadata),
                )
        return result
