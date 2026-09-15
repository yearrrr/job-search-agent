"""原件只写新文件；文本定位和待确认候选分开保存。"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from ..schemas import CATEGORY_LABELS, Conflict, InputError, bounded, valid_corpus
from .common import get_row, now, once, uid

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT = 120000
MAX_CANDIDATES = 200
SECTION_CATEGORIES = {
    "教育背景": "education",
    "教育经历": "education",
    "education": "education",
    "项目经历": "project",
    "项目经验": "project",
    "projects": "project",
    "专业技能": "skill",
    "技能": "skill",
    "skills": "skill",
    "实习经历": "experience",
    "工作经历": "experience",
    "实践经历": "experience",
}


def parse_file(content: bytes, suffix: str) -> list[dict]:
    if suffix == ".pdf":
        # 不在 Web 进程内执行 PDF 解析：复杂或损坏文件有独立进程超时边界。
        try:
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "job_search_agent.services.pdf_worker"],
                input=content,
                capture_output=True,
                timeout=12,
                check=False,
            )
            parsed = json.loads(result.stdout)
            if result.returncode or not parsed.get("ok"):
                raise InputError(parsed.get("error", "PDF 解析失败，请粘贴文本替代。"))
            pages = parsed["pages"]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            raise InputError("PDF 解析超时或不可用，请改用较小文件或粘贴文本。") from None
    else:
        try:
            pages = [content.decode("utf-8-sig")]
        except UnicodeDecodeError:
            raise InputError("文本不是 UTF-8 编码，请另存为 UTF-8 或粘贴文本。") from None
    if sum(map(len, pages)) > MAX_TEXT:
        raise InputError("提取文本超过 12 万字符，请拆分资料或粘贴关键内容。")
    snippets = []
    for page_no, page in enumerate(pages, 1):
        for line_no, line in enumerate(page.splitlines(), 1):
            if line.strip():
                if "\x00" in line or len(line) > 4000:
                    raise InputError("文本含无效字符或单行过长，请整理后粘贴。")
                snippets.append(
                    {
                        "id": uid(),
                        "page": page_no if suffix == ".pdf" else None,
                        "line": line_no,
                        "text": line,
                    }
                )
    if not snippets:
        raise InputError("未提取到文字；空文件或扫描 PDF 请粘贴可复制的文本，本版不做 OCR。")
    if len(snippets) > 3000:
        raise InputError("文本行数超过限制，请拆分资料。")
    return snippets


def local_candidates(snippets):
    category = "other"
    result = []
    previous = None
    for snippet in snippets:
        normalized = re.sub(r"^[#*\-\s]+|[：:\s]+$", "", snippet["text"]).lower()
        if normalized in SECTION_CATEGORIES:
            category = SECTION_CATEGORIES[normalized]
            previous = None
            continue
        # 标记、标题不作为经历；正文逐条保留，不补写日期、数字、学历。
        if normalized.startswith(("虚构测试", "仅用于测试", "个人简历")) or normalized.isdecimal():
            previous = None
            continue
        # PDF 的视觉换行不是语义边界。合并同页相邻、尚未结束的句子，保留每一行引用。
        if (
            snippet["page"] is not None
            and previous is not None
            and result
            and snippet["page"] == previous["page"]
            and snippet["line"] == previous["line"] + 1
            and not previous["text"].rstrip().endswith(("。", "！", "？", ".", "!", "?", "；", ";"))
            and len(result[-1]["text"]) + len(snippet["text"]) <= 4000
        ):
            result[-1]["text"] += snippet["text"].strip()
            result[-1]["source_ids"].append(snippet["id"])
            previous = snippet
            continue
        result.append(
            {
                "snippet_id": snippet["id"],
                "category": category,
                "text": snippet["text"].strip(),
                "source_ids": [snippet["id"]],
            }
        )
        previous = snippet
        if len(result) >= MAX_CANDIDATES:
            break
    return result


def insert_fact(connection, document_id, snippet_id, category, text, method, source_ids=None):
    fact_id, timestamp = uid(), now()
    connection.execute(
        """INSERT INTO facts(id,document_id,snippet_id,category,text,status,method,updated_at)
           VALUES (?,?,?,?,?,'pending',?,?)""",
        (fact_id, document_id, snippet_id, category, text, method, timestamp),
    )
    connection.execute(
        "INSERT INTO fact_history VALUES (?,?,?,?,?,?,?,?)",
        (uid(), fact_id, 1, text, category, "pending", method, timestamp),
    )
    for order, source_id in enumerate(source_ids or [snippet_id]):
        connection.execute("INSERT INTO fact_sources VALUES (?,?,?)", (fact_id, source_id, order))
    return fact_id


def attach_sources(connection, fact):
    fact["sources"] = [
        dict(r)
        for r in connection.execute(
            """SELECT s.* FROM fact_sources fs
        JOIN snippets s ON s.id=fs.snippet_id WHERE fs.fact_id=? ORDER BY fs.ordinal""",
            (fact["id"],),
        )
    ]
    if fact["sources"]:
        fact["quote"] = "\n".join(s["text"] for s in fact["sources"])
    return fact


class Documents:
    def __init__(self, database):
        self.db = database

    def import_file(self, name, content, kind, corpus, operation_id):
        corpus = valid_corpus(corpus)
        if kind not in ("resume", "project", "notes"):
            raise InputError("资料类型无效。")
        name = bounded(name, 180)
        if name != PureWindowsPath(name).name or "/" in name or ":" in name or name in (".", ".."):
            raise InputError("文件名不能包含路径，请直接选择文件。")
        suffix = Path(name).suffix.lower()
        if suffix not in (".pdf", ".txt", ".md"):
            raise InputError("只支持文字型 PDF、UTF-8 TXT 和 Markdown；也可以粘贴文本。")
        if len(content) > MAX_FILE_BYTES:
            raise InputError("文件超过 5 MB，请拆分文件或粘贴关键内容。")
        checksum = hashlib.sha256(content).hexdigest()
        # 先做无副作用解析，事务只保存本地结果，不包含远程模型调用。
        try:
            snippets = parse_file(content, suffix)
            error = None
        except InputError as exc:
            snippets, error = [], str(exc)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def save():
                existing = connection.execute(
                    "SELECT id FROM documents WHERE corpus=? AND kind=? AND sha256=?",
                    (corpus, kind, checksum),
                ).fetchone()
                if existing:
                    return {"id": existing["id"], "duplicate": True}
                document_id = uid()
                original = self.original_path(document_id, suffix)
                original.parent.mkdir(parents=True, exist_ok=True)
                with original.open("xb") as output:
                    output.write(content)
                connection.execute(
                    """INSERT INTO documents(id,name,kind,corpus,sha256,suffix,byte_size,status,error,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document_id,
                        name,
                        kind,
                        corpus,
                        checksum,
                        suffix,
                        len(content),
                        "failed" if error else "parsed",
                        error,
                        now(),
                    ),
                )
                for snippet in snippets:
                    connection.execute(
                        "INSERT INTO snippets VALUES (?,?,?,?,?)",
                        (
                            snippet["id"],
                            document_id,
                            snippet["page"],
                            snippet["line"],
                            snippet["text"],
                        ),
                    )
                if kind != "notes":
                    for item in local_candidates(snippets):
                        insert_fact(connection, document_id, method="local", **item)
                return {"id": document_id, "duplicate": False}

            return once(connection, operation_id, ["import", name, kind, corpus, checksum], save)

    def original_path(self, document_id, suffix):
        from uuid import UUID

        try:
            if str(UUID(document_id)) != document_id or suffix not in (
                ".pdf",
                ".md",
                ".txt",
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            ):
                raise ValueError
        except ValueError:
            raise InputError("原件标识无效。") from None
        folder = self.db.local_path("originals")
        path = (folder / f"{document_id}{suffix}").resolve()
        if not path.is_relative_to(folder):
            raise InputError("原件路径无效。")
        return path

    def detail(self, document_id):
        with self.db.connect() as connection:
            document = get_row(connection, "documents", document_id)
            document["snippets"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM snippets WHERE document_id=? ORDER BY page,line", (document_id,)
                )
            ]
            document["facts"] = [
                dict(r)
                for r in connection.execute(
                    """SELECT f.*,s.page,s.line,s.text AS quote FROM facts f JOIN snippets s ON s.id=f.snippet_id
                   WHERE f.document_id=? ORDER BY s.page,s.line,f.id""",
                    (document_id,),
                )
            ]
            document["runs"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM extraction_runs WHERE document_id=? ORDER BY created_at DESC",
                    (document_id,),
                )
            ]
            for fact in document["facts"]:
                attach_sources(connection, fact)
                fact["history"] = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT * FROM fact_history WHERE fact_id=? ORDER BY revision",
                        (fact["id"],),
                    )
                ]
            return document

    def update_fact(self, fact_id, text, category, status, revision, operation_id):
        text = bounded(text, 4000)
        if category not in CATEGORY_LABELS or status not in ("confirmed", "pending", "rejected"):
            raise InputError("事实分类或确认状态无效。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def update():
                fact = get_row(connection, "facts", fact_id)
                if fact["revision"] != revision:
                    raise Conflict("事实已被修改，请刷新后核对最新版本。")
                if (text, category, status) == (fact["text"], fact["category"], fact["status"]):
                    return {"id": fact["document_id"]}
                timestamp = now()
                method = (
                    "user_edit"
                    if (text, category) != (fact["text"], fact["category"])
                    else fact["method"]
                )
                connection.execute(
                    """UPDATE facts SET text=?,category=?,status=?,revision=?,method=?,
                    confirmed_at=?,updated_at=? WHERE id=?""",
                    (
                        text,
                        category,
                        status,
                        revision + 1,
                        method,
                        timestamp if status == "confirmed" else None,
                        timestamp,
                        fact_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO fact_history VALUES (?,?,?,?,?,?,?,?)",
                    (uid(), fact_id, revision + 1, text, category, status, method, timestamp),
                )
                return {"id": fact["document_id"]}

            return once(
                connection,
                operation_id,
                ["fact", fact_id, text, category, status, revision],
                update,
            )

    def add_fact(self, document_id, snippet_id, category, text, operation_id):
        text = bounded(text, 4000)
        if category not in CATEGORY_LABELS:
            raise InputError("事实分类无效。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def add():
                document = get_row(connection, "documents", document_id)
                if document["kind"] == "notes":
                    raise InputError("学习笔记不能直接作为个人经历；请另行提供项目或经历说明。")
                if not connection.execute(
                    "SELECT id FROM snippets WHERE id=? AND document_id=?",
                    (snippet_id, document_id),
                ).fetchone():
                    raise InputError("来源片段不属于此文档。")
                insert_fact(connection, document_id, snippet_id, category, text, "user_edit")
                return {"id": document_id}

            return once(
                connection, operation_id, ["add_fact", document_id, snippet_id, category, text], add
            )
