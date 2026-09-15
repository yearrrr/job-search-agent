import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pypdf import PdfReader

from job_search_agent.agent.model import ModelError, ModelReply, Usage
from job_search_agent.config import Settings
from job_search_agent.db import Database
from job_search_agent.main import create_app
from job_search_agent.schemas import Conflict, InputError, JobInput
from job_search_agent.services.common import uid
from job_search_agent.services.documents import Documents
from job_search_agent.services.jobs import Jobs
from job_search_agent.services.profile_v2 import ProfileV2
from job_search_agent.services.resume_export import docx_bytes, paragraphs, pdf_bytes
from job_search_agent.services.resumes_v2 import Resumes, snapshot_token


@pytest.fixture
def resume_env(tmp_path):
    settings = Settings(data_dir=tmp_path / "resumes", api_key=SecretStr("synthetic-key-only"))
    db = Database(settings.data_dir)
    db.initialize()
    profile = ProfileV2(db)
    facts = []
    for category, title, description in (
        ("basic", "虚构同学", "邮箱 demo@example.com；电话 13800000000。"),
        ("education", "计算机本科", "2027 年毕业。"),
        ("project", "文档问答项目", "使用 Python 开发 RAG 服务，编写 20 条回归测试。"),
        ("project", "数据看板项目", "使用 SQL 和 FastAPI 开发数据看板。"),
        ("skill", "编程技能", "掌握 Python 与 SQL。"),
    ):
        result = profile.add_manual(
            "demo", {"category": category, "title": title, "description": description}, uid()
        )
        fact = profile.detail(result["id"])["facts"][0]
        Documents(db).update_fact(
            fact["id"], fact["text"], category, "confirmed", fact["revision"], uid()
        )
        facts.append(fact)
    profile.add_manual(
        "personal",
        {"category": "project", "title": "私人项目", "description": "PRIVATE 未确认经历"},
        uid(),
    )
    profile.add_manual(
        "demo", {"category": "project", "title": "未确认项目", "description": "PENDING"}, uid()
    )
    job = Jobs(db).create(
        JobInput(
            company="虚构公司",
            title="AI 应用工程师",
            corpus="demo",
            raw_jd="任职要求：\n熟悉 Python 与 RAG",
        ),
        uid(),
    )
    service = Resumes(db, settings)
    return db, settings, service, job["id"], facts


def start(env, kind="resume", ids=None, mode="mock", instruction=""):
    _, _, service, job_id, facts = env
    selected = ids if ids is not None else [f["id"] for f in facts]
    snapshot = service.snapshot(job_id)
    return service.start(
        job_id, kind, selected, snapshot_token(snapshot), mode, instruction, uid()
    )["id"]


def completed(env):
    service = env[2]
    rid = start(env)
    service.runs.run(rid)
    assert service.runs.detail(rid)["status"] == "completed"
    return rid, service.draft(rid)


class Writer:
    def __init__(self, bad=None):
        self.calls, self.messages, self.bad = 0, [], bad

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(copy.deepcopy(messages))
        facts = json.loads(messages[1]["content"])["selected_facts"]
        blocks = [
            {
                "fact_id": f["id"],
                "heading": f["heading"],
                "text": f["metadata"].get("description") or f["text"],
            }
            for f in facts
        ]
        if self.bad:
            self.bad(blocks, self.calls)
        return ModelReply(
            content=json.dumps({"blocks": blocks}),
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


def test_recommendation_then_selection_writing_and_immutable_versions(resume_env):
    db, _, service, job_id, facts = resume_env
    rec = start(resume_env, "recommendation")
    service.runs.run(rec)
    result = service.runs.detail(rec)
    assert result["status"] == "completed"
    assert {p["fact_id"] for p in result["result"]["projects"]} == {
        f["id"] for f in facts if f["category"] == "project"
    }
    rid, draft = completed(resume_env)
    assert draft["revision"] == 0
    op = uid()
    saved = service.save(rid, draft["content"], 0, op)
    assert service.save(rid, draft["content"], 0, op) == saved
    first = service.confirm(rid, 1, uid())
    assert service.confirm(rid, 1, uid()) == first
    before = service.version(first["id"])
    changed = copy.deepcopy(draft["content"])
    changed["blocks"][-1]["text"] += " 熟悉基础调试。"
    service.save(rid, changed, 1, uid())
    second = service.confirm(rid, 2, uid())
    assert second["version"] == 2 and service.version(first["id"]) == before
    assert len(service.list_for_job(job_id)[1]) == 2
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM resume_draft_history").fetchone()[0] == 2


def test_writer_sends_only_selected_nonbasic_facts_and_keeps_contacts_local(resume_env):
    _, _, service, _, facts = resume_env
    rid = start(resume_env, ids=[facts[0]["id"], facts[2]["id"]], mode="deepseek")
    model = Writer()
    service.runs.run(rid, model)
    run = service.runs.detail(rid)
    assert run["status"] == "completed" and run["known_tokens"] == 30
    sent = json.dumps(model.messages, ensure_ascii=False)
    assert "demo@example.com" not in sent and "13800000000" not in sent
    assert facts[3]["id"] not in sent and "PRIVATE" not in sent and "PENDING" not in sent
    assert "demo@example.com" in run["result"]["blocks"][0]["text"]
    assert [b["fact_id"] for b in run["result"]["blocks"]] == [facts[0]["id"], facts[2]["id"]]


@pytest.mark.parametrize(
    "bad",
    [
        lambda blocks, n: blocks[0].update(fact_id="invented"),
        lambda blocks, n: blocks[0].update(text="优化性能 99%"),
        lambda blocks, n: blocks.reverse(),
        lambda blocks, n: blocks.append(blocks[0]),
    ],
)
def test_invalid_writer_output_is_corrected_within_three_calls(resume_env, bad):
    service = resume_env[2]
    rid = start(resume_env, mode="deepseek")
    model = Writer(bad)
    service.runs.run(rid, model)
    run = service.runs.detail(rid)
    assert run["status"] == "failed" and run["error_code"] == "invalid_output"
    assert model.calls == 3 and run["known_tokens"] == 90
    service.runs.run(rid, model)
    assert model.calls == 3


def test_writer_can_correct_invalid_id_without_duplicate_paid_prefix(resume_env):
    service = resume_env[2]

    def bad(blocks, n):
        if n == 1:
            blocks[0]["fact_id"] = "wrong"

    rid = start(resume_env, mode="deepseek")
    model = Writer(bad)
    service.runs.run(rid, model)
    assert service.runs.detail(rid)["status"] == "completed" and model.calls == 2


@pytest.mark.parametrize("selection", [[], ["foreign"], [1], "not-list"])
def test_selection_rejects_invalid_and_foreign_ids(resume_env, selection):
    with pytest.raises(InputError):
        start(resume_env, ids=selection)
    with resume_env[0].connect() as c:
        assert c.execute("SELECT count(*) FROM v2_runs").fetchone()[0] == 0


def test_selection_rejects_duplicates_and_stale_page(resume_env):
    db, _, service, job_id, facts = resume_env
    with pytest.raises(InputError):
        start(resume_env, ids=[facts[0]["id"]] * 2)
    snapshot = service.snapshot(job_id)
    fact = facts[2]
    current = Documents(db).detail(fact["document_id"])["facts"][0]
    Documents(db).update_fact(
        current["id"],
        current["text"] + " 已补充。",
        current["category"],
        "confirmed",
        current["revision"],
        uid(),
    )
    with pytest.raises(Conflict):
        service.start(job_id, "resume", [fact["id"]], snapshot_token(snapshot), "mock", "", uid())


def test_changed_fact_prevents_confirmation_but_keeps_old_version(resume_env):
    db, _, service, _, facts = resume_env
    rid, draft = completed(resume_env)
    service.save(rid, draft["content"], 0, uid())
    original = service.confirm(rid, 1, uid())
    changed = copy.deepcopy(draft["content"])
    changed["blocks"][0]["text"] += " 新草稿。"
    service.save(rid, changed, 1, uid())
    fact = facts[2]
    current = Documents(db).detail(fact["document_id"])["facts"][0]
    Documents(db).update_fact(
        current["id"], current["text"], current["category"], "rejected", current["revision"], uid()
    )
    with pytest.raises(Conflict):
        service.confirm(rid, 2, uid())
    assert service.confirm(rid, 1, uid()) == original
    assert b"PK" == docx_bytes(service.version(original["id"]))[:2]


def test_optimistic_draft_conflict_and_source_ids_cannot_be_swapped(resume_env):
    service = resume_env[2]
    rid, draft = completed(resume_env)
    service.save(rid, draft["content"], 0, uid())
    with pytest.raises(Conflict):
        service.save(rid, draft["content"], 0, uid())
    bad = copy.deepcopy(draft["content"])
    bad["blocks"][0]["fact_id"] = "foreign"
    with pytest.raises(InputError):
        service.save(rid, bad, 1, uid())


def test_identical_start_and_concurrent_confirm_are_idempotent(resume_env):
    service = resume_env[2]
    rid, draft = completed(resume_env)
    assert start(resume_env) == rid
    service.save(rid, draft["content"], 0, uid())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.confirm(rid, 1, uid()), range(2)))
    assert results[0] == results[1]


def test_export_word_pdf_same_content_chinese_and_escaped_markup(resume_env):
    service = resume_env[2]
    rid, draft = completed(resume_env)
    draft["content"]["blocks"][-1]["text"] = "Python <script> & RAG\n中文排版测试与英文混合。"
    service.save(rid, draft["content"], 0, uid())
    v = service.version(service.confirm(rid, 1, uid())["id"])
    word = Document(io.BytesIO(docx_bytes(v)))
    expected = [text for _, text in paragraphs(v)]
    assert [p.text for p in word.paragraphs] == expected
    pdf = PdfReader(io.BytesIO(pdf_bytes(v)))
    text = "".join(p.extract_text() for p in pdf.pages)
    assert "离线流程演示" in text and "<script> & RAG" in text
    assert "demo@example.com" in text and len(pdf.pages) <= 2
    for page in pdf.pages:
        fonts = page["/Resources"]["/Font"].get_object()
        assert any("/FontDescriptor" in f.get_object() for f in fonts.values())


def test_model_budget_still_applies_to_corrections(resume_env):
    _, settings, service, _, _ = resume_env
    service.runs.settings = settings.model_copy(update={"max_model_calls": 1})
    rid = start(resume_env, mode="deepseek")
    service.runs.run(rid, Writer(lambda blocks, n: blocks[0].update(fact_id="bad")))
    assert service.runs.detail(rid)["error_code"] == "budget"
    assert service.runs.detail(rid)["model_calls"] == 1


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_web_full_flow_csrf_inline_save_confirm_download_and_get_no_model(resume_env):
    db, settings, service, job_id, _ = resume_env
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        workspace = client.get(f"/v2/jobs/{job_id}/resume")
        assert workspace.status_code == 200 and "选择要写进简历" in workspace.text
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM v2_runs").fetchone()[0] == 0
        rid, draft = completed(resume_env)
        assert "编辑并核对完整简历" in client.get(f"/v2/runs/{rid}").text
        endpoint = f"/v2/resumes/{rid}/edit"
        assert client.post(endpoint, data={}).status_code == 400
        payload = {
            "csrf": client.cookies["job_csrf"],
            "operation_id": uid(),
            "revision": 0,
            "content": json.dumps(draft["content"]),
        }
        headers = {"Accept": "application/json"}
        saved = client.post(endpoint, data=payload, headers=headers)
        assert saved.status_code == 200 and saved.json()["revision"] == 1
        assert (
            client.post(
                endpoint, data=payload, headers={**headers, "Origin": "https://evil.invalid"}
            ).status_code
            == 400
        )
        payload = {"csrf": client.cookies["job_csrf"], "operation_id": uid(), "revision": 1}
        assert (
            client.post(f"/v2/resumes/{rid}/confirm", data=payload, headers=headers).status_code
            == 400
        )
        payload["reviewed"] = "yes"
        confirmed = client.post(f"/v2/resumes/{rid}/confirm", data=payload, headers=headers)
        assert confirmed.status_code == 200
        url = confirmed.json()["url"]
        assert client.get(url).status_code == 200
        for fmt in ("docx", "pdf"):
            response = client.get(url + "/download/" + fmt)
            assert (
                response.status_code == 200
                and "attachment" in response.headers["content-disposition"]
            )
        assert client.get(url + "/download/exe").status_code == 400
        assert service.runs.detail(rid)["model_calls"] == 1


def test_stale_during_generation_is_not_saved_as_success(resume_env):
    db, _, service, _, _ = resume_env

    class ChangesSource(Writer):
        def complete(self, messages, **kwargs):
            response = super().complete(messages, **kwargs)
            with db.connect() as c:
                c.execute("UPDATE facts SET revision=revision+1 WHERE status='confirmed'")
            return response

    rid = start(resume_env, mode="deepseek")
    service.runs.run(rid, ChangesSource())
    assert service.runs.detail(rid)["error_code"] == "stale_evidence"


def test_ambiguous_call_does_not_retry(resume_env):
    service = resume_env[2]
    rid = start(resume_env, mode="deepseek")

    class Timeout:
        def complete(self, *args, **kwargs):
            raise ModelError("timeout")

    service.runs.run(rid, Timeout())
    assert service.runs.detail(rid)["model_calls"] == 1
    assert service.runs.detail(rid)["error_code"] == "timeout"
