import hashlib
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from job_search_agent.agent.model import ModelError, ModelReply, Usage
from job_search_agent.db import Database
from job_search_agent.schemas import Conflict, InputError, JobInput
from job_search_agent.services.demo import SAMPLES, seed_demo
from job_search_agent.services.documents import MAX_FILE_BYTES, Documents
from job_search_agent.services.extraction import extract_candidates
from job_search_agent.services.jobs import Jobs
from job_search_agent.services.profile import Profile


def op():
    return str(uuid4())


@pytest.fixture
def db(settings):
    database = Database(settings.data_dir)
    database.initialize()
    return database


def import_md(
    db, *, corpus="demo", kind="resume", name="resume.md", content=None, operation_id=None
):
    content = content if content is not None else (SAMPLES / "fictional-resume.md").read_bytes()
    return Documents(db).import_file(name, content, kind, corpus, operation_id or op())["id"]


def fixture_job(**updates):
    data = json.loads((SAMPLES / "jobs.json").read_text(encoding="utf-8"))[0]
    return JobInput(**{**data, **updates})


def test_v1_migration_preserves_existing_data(settings):
    database = Database(settings.data_dir)
    settings.data_dir.mkdir(parents=True)
    with sqlite3.connect(database.path) as connection:
        connection.executescript("""CREATE TABLE smoke_results(task_id TEXT PRIMARY KEY,content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT 'old');
            INSERT INTO smoke_results(task_id,content) VALUES ('old','keep'); PRAGMA user_version=1;""")
    database.initialize()
    assert database.health() == 7
    assert database.save_smoke_once("old", "replace") == "keep"
    database.initialize()
    assert database.health() == 7


@pytest.mark.parametrize("suffix", [".md", ".txt", ".pdf"])
def test_supported_files_have_original_hash_and_located_pending_facts(db, suffix):
    data = (
        SAMPLES / ("fictional-resume.pdf" if suffix == ".pdf" else "fictional-resume.md")
    ).read_bytes()
    service = Documents(db)
    imported = service.import_file("简历" + suffix, data, "resume", "demo", op())
    doc = service.detail(imported["id"])
    assert doc["status"] == "parsed"
    assert (
        hashlib.sha256(service.original_path(doc["id"], suffix).read_bytes()).hexdigest()
        == doc["sha256"]
    )
    assert {"education", "project", "skill", "experience"} <= {f["category"] for f in doc["facts"]}
    assert all(f["status"] == "pending" and f["line"] > 0 for f in doc["facts"])
    assert (
        all(f["page"] == 1 for f in doc["facts"])
        if suffix == ".pdf"
        else all(f["page"] is None for f in doc["facts"])
    )
    assert Profile(db).confirmed("demo") == []


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("empty.txt", b"", "未提取到文字"),
        ("gbk.txt", "中文资料".encode("gbk"), "UTF-8"),
        ("bad.pdf", b"%PDF-broken", "损坏"),
        ("null.txt", b"hello\x00world", "无效字符"),
    ],
)
def test_failed_import_is_visible_with_original_and_no_facts(db, name, content, expected):
    service = Documents(db)
    doc = service.detail(service.import_file(name, content, "resume", "demo", op())["id"])
    assert doc["status"] == "failed" and expected in doc["error"]
    assert doc["facts"] == []
    assert service.original_path(doc["id"], doc["suffix"]).read_bytes() == content


@pytest.mark.parametrize("variant", ["blank", "encrypted", "too_many_pages"])
def test_unsupported_pdf_types_fail_clearly(db, variant):
    writer, stream = PdfWriter(), io.BytesIO()
    for _ in range(31 if variant == "too_many_pages" else 1):
        writer.add_blank_page(width=200, height=200)
    if variant == "encrypted":
        writer.encrypt("test-fixture-only")
    writer.write(stream)
    service = Documents(db)
    doc = service.detail(
        service.import_file("test.pdf", stream.getvalue(), "resume", "demo", op())["id"]
    )
    assert doc["status"] == "failed"
    assert {"blank": "未提取到文字", "encrypted": "加密", "too_many_pages": "30 页"}[
        variant
    ] in doc["error"]


@pytest.mark.parametrize(
    "name", ["../escape.txt", r"..\escape.txt", "C:\\secret.txt", "a.txt:stream", "file.exe"]
)
def test_bad_paths_or_types_do_not_create_originals(db, name):
    with pytest.raises(InputError):
        Documents(db).import_file(name, b"test", "resume", "demo", op())
    assert not (db.data_dir / "originals").exists()


def test_size_limit_and_duplicate_filename_do_not_overwrite(db):
    service = Documents(db)
    with pytest.raises(InputError, match="5 MB"):
        service.import_file("big.txt", b"x" * (MAX_FILE_BYTES + 1), "resume", "demo", op())
    first = import_md(db)
    second = import_md(db, content=b"different text")
    assert first != second
    assert import_md(db) == first
    assert (
        service.original_path(first, ".md").read_bytes()
        == (SAMPLES / "fictional-resume.md").read_bytes()
    )


def test_fact_confirmation_edit_revoke_history_and_corpus_isolation(db):
    service = Documents(db)
    document_id = import_md(db)
    fact = service.detail(document_id)["facts"][0]
    action = op()
    for _ in range(2):
        service.update_fact(fact["id"], fact["text"], fact["category"], "confirmed", 1, action)
    assert len(Profile(db).confirmed("demo")) == 1
    assert Profile(db).confirmed() == []
    with pytest.raises(Conflict):
        service.update_fact(fact["id"], "old page edit", "other", "confirmed", 1, op())
    service.update_fact(fact["id"], "虚构用户明确补充：本科", "education", "confirmed", 2, op())
    updated = next(f for f in service.detail(document_id)["facts"] if f["id"] == fact["id"])
    assert updated["method"] == "user_edit" and updated["quote"] == fact["quote"]
    service.update_fact(fact["id"], updated["text"], "education", "pending", 3, op())
    assert Profile(db).confirmed("demo") == []
    with db.connect() as connection:
        history = connection.execute(
            "SELECT * FROM fact_history WHERE fact_id=? ORDER BY revision", (fact["id"],)
        ).fetchall()
    assert [h["status"] for h in history] == ["pending", "confirmed", "confirmed", "pending"]


def test_notes_cannot_be_converted_into_personal_experience(db):
    document_id = import_md(db, kind="notes")
    doc = Documents(db).detail(document_id)
    assert doc["facts"] == []
    with pytest.raises(InputError, match="学习笔记"):
        Documents(db).add_fact(
            document_id, doc["snippets"][0]["id"], "project", "I used Kubernetes", op()
        )


def test_source_id_must_belong_to_selected_document(db):
    first = import_md(db)
    other = import_md(db, content=b"other source")
    source = Documents(db).detail(other)["snippets"][0]["id"]
    with pytest.raises(InputError, match="不属于"):
        Documents(db).add_fact(first, source, "other", "forged source", op())


def test_preferences_are_explicit_and_empty_until_set(db):
    profile = Profile(db)
    assert profile.preferences("demo")["roles"] == ""
    action = op()
    profile.set_preferences("demo", "AI Agent 工程师", "", "", 0, action)
    profile.set_preferences("demo", "AI Agent 工程师", "", "", 0, action)
    assert profile.preferences("demo")["cities"] == ""
    assert profile.preferences("personal")["roles"] == ""


def test_jobs_unknown_fields_source_and_original_immutable(db):
    jobs = Jobs(db)
    raw = fixture_job()
    job_id = jobs.create(raw, op())["id"]
    job = jobs.detail(job_id)
    assert job["fields"]["location"]["text"] == "上海"
    assert job["fields"]["deadline"]["text"] == job["fields"]["salary"]["text"] == ""
    for field in job["fields"].values():
        for source in field["sources"]:
            assert raw.raw_jd.splitlines()[source["line"] - 1] == source["quote"]
    values = {k: v["text"] for k, v in job["fields"].items()}
    values["salary"] = "虚构人工补充：待确认"
    jobs.update_fields(job_id, values, 1, op())
    updated = jobs.detail(job_id)
    assert updated["raw_jd"] == raw.raw_jd and updated["status"] == "saved"
    assert updated["fields"]["salary"]["method"] == "user_edit"
    assert updated["fields"]["salary"]["sources"] == []


def test_duplicate_jobs_keep_progress_and_other_batches_separate(db):
    jobs = Jobs(db)
    first = jobs.create(fixture_job(), op())
    jobs.change_status(first["id"], "preparing", 1, op())
    again = jobs.create(fixture_job(), op())
    assert again["duplicate"] and first["id"] == again["id"]
    assert jobs.detail(first["id"])["status"] == "preparing"
    second = jobs.create(fixture_job(batch="2027 届补录", job_code="SECOND"), op())
    assert second["id"] != first["id"]
    assert jobs.detail(second["id"])["related"][0]["id"] == first["id"]


def test_status_notes_todos_idempotency_and_conflict(db):
    jobs = Jobs(db)
    job_id = jobs.create(fixture_job(), op())["id"]
    action = op()
    for _ in range(2):
        jobs.change_status(job_id, "applied", 1, action)
    with pytest.raises(Conflict):
        jobs.change_status(job_id, "closed", 1, op())
    note_op, todo_op = op(), op()
    for _ in range(2):
        jobs.add_note_or_todo(job_id, "虚构测试备注", "", "note", note_op)
        jobs.add_note_or_todo(job_id, "虚构测试待办", "2026-10-01", "todo", todo_op)
    job = jobs.detail(job_id)
    assert len(job["history"]) == 2 and len(job["notes"]) == len(job["todos"]) == 1
    todo = job["todos"][0]
    finish_op = op()
    for _ in range(2):
        jobs.set_todo(todo["id"], True, 1, finish_op)
    assert jobs.detail(job_id)["todos"][0]["done"] == 1
    assert jobs.detail(job_id)["status"] == "applied"


def test_concurrent_duplicate_import_saves_once(db):
    operation = op()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: import_md(db, operation_id=operation), range(4)))
    assert len(set(result)) == 1
    assert len(list((db.data_dir / "originals").iterdir())) == 1


def test_same_operation_different_payload_is_rejected(db):
    action = op()
    first = import_md(db, operation_id=action)
    with pytest.raises(Conflict):
        import_md(db, operation_id=action, content=b"different")
    assert len(list((db.data_dir / "originals").iterdir())) == 1
    assert Documents(db).detail(first)["status"] == "parsed"


def test_demo_reloading_and_database_reopening(db):
    first = seed_demo(db)
    reopened = Database(db.data_dir)
    reopened.initialize()
    assert seed_demo(reopened) == first
    assert len(Jobs(reopened).list("demo")) == 3
    assert Jobs(reopened).list("personal") == []


class FakeExtractor:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.calls = output, error, 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if self.error:
            raise ModelError(self.error)
        return ModelReply(content=self.output, usage=Usage(total_tokens=20), elapsed_seconds=0.1)


@pytest.mark.parametrize("bad", ["bad_json", "foreign_source", "invented_fact", "timeout"])
def test_model_invalid_output_never_creates_facts_or_retries(db, settings, bad):
    document_id = import_md(db)
    before = Documents(db).detail(document_id)["facts"]
    result = {
        "facts": [
            {
                "source": 999 if bad == "foreign_source" else 1,
                "category": "project",
                "text": "invented achievement",
            }
        ]
    }
    model = FakeExtractor(
        "not JSON" if bad == "bad_json" else json.dumps(result),
        "timeout" if bad == "timeout" else None,
    )
    operation = op()
    run = extract_candidates(db, settings, document_id, operation, model=model)
    assert run["status"] == "failed"
    assert run["total_tokens"] is None if bad == "timeout" else run["total_tokens"] == 20
    extract_candidates(db, settings, document_id, operation, model=model)
    assert model.calls == 1
    assert Documents(db).detail(document_id)["facts"] == before


def test_model_candidates_are_pending_and_located(db, settings):
    document_id = import_md(db, content="无标准分类的补充\n能够使用 Python 编写测试。".encode())
    model = FakeExtractor(
        json.dumps({"facts": [{"source": 2, "category": "skill", "text": "Python"}]})
    )
    run = extract_candidates(db, settings, document_id, op(), model=model)
    assert run["status"] == "ok"
    new = [f for f in Documents(db).detail(document_id)["facts"] if f["method"] == "model"]
    assert new[0]["text"] == "Python" and new[0]["status"] == "pending" and new[0]["line"] == 2
    assert Profile(db).confirmed("demo") == []


def test_pdf_wraps_keep_complete_claim_and_all_source_lines(db):
    document = Documents(db).detail(
        Documents(db).import_file(
            "test.pdf", (SAMPLES / "fictional-resume.pdf").read_bytes(), "resume", "demo", op()
        )["id"]
    )
    assert not any(f["text"].isdecimal() for f in document["facts"])
    claim = next(f for f in document["facts"] if "为重复确认增加" in f["text"])
    assert "没有生产环境部署经历记录" in claim["text"]
    assert len(claim["sources"]) == 2
    assert "\n" in claim["quote"] and "署经历记录" in claim["quote"]
    Documents(db).update_fact(claim["id"], claim["text"], claim["category"], "confirmed", 1, op())
    assert Profile(db).confirmed("demo")[0]["sources"] == claim["sources"]
