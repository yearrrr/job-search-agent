import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from job_search_agent.agent.model import DeepSeekModel, ModelError, ModelReply, Usage
from job_search_agent.config import Settings
from job_search_agent.db import Database
from job_search_agent.main import create_app
from job_search_agent.schemas import Conflict, InputError
from job_search_agent.services.common import now, uid
from job_search_agent.services.demo import SAMPLES
from job_search_agent.services.documents import Documents
from job_search_agent.services.profile import Profile
from job_search_agent.services.profile_parser import ProfileParser
from job_search_agent.services.profile_v2 import MAIN_CATEGORIES, ProfileV2

FAKE_KEY = "fictional-v2-key-never-real"
pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


@pytest.fixture
def v2(tmp_path):
    settings = Settings(data_dir=tmp_path / "v2-data", api_key=SecretStr(FAKE_KEY))
    db = Database(settings.data_dir)
    db.initialize()
    return db, settings, ProfileV2(db)


def imported(profile, *, content=None, corpus="demo"):
    return profile.import_file(
        "虚构测试.txt",
        content or "虚构测试人物\n项目A\n使用Python。\n完成检索和测试。".encode(),
        "resume",
        corpus,
        uid(),
    )["id"]


class SampleModel:
    def __init__(self, categories=("project",)):
        self.calls = 0
        self.categories = categories
        self.messages = []

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        content = messages[-1]["content"]
        if isinstance(content, str):
            source_ids = [s["source_id"] for s in json.loads(content)]
        else:
            source_ids = [
                c["text"].split("：")[1].split("，")[0] for c in content if c["type"] == "text"
            ]
        entries = [
            {
                "category": c,
                "title": "虚构条目-" + c,
                "description": "使用 Python 完成检索和测试。",
                "source_ids": source_ids,
                "period": "2026",
                "technologies": ["Python"],
            }
            for c in self.categories
        ]
        return ModelReply(
            content=json.dumps({"entries": entries}, ensure_ascii=False),
            usage=Usage(prompt_tokens=100, completion_tokens=60, total_tokens=160),
        )


def parsed(v2, categories=("project",)):
    db, settings, profile = v2
    document_id = imported(profile)
    model = SampleModel(categories)
    parser = ProfileParser(db, settings, lambda s: model)
    job_id = parser.start(document_id, uid())["id"]
    parser.run(job_id)
    assert parser.status(job_id)["status"] == "completed", parser.status(job_id)
    return document_id, model, parser, job_id


def item(fact, status="confirmed", **changes):
    return {
        "id": fact["id"],
        "revision": fact["revision"],
        "status": status,
        "entry": {
            "title": fact["title"],
            "category": fact["category"],
            **fact["metadata"],
            **changes,
        },
    }


def test_text_import_never_uses_local_candidates(v2, monkeypatch):
    db, settings, profile = v2
    monkeypatch.setattr(
        "job_search_agent.services.documents.local_candidates",
        lambda _: pytest.fail("V2 cannot call heading heuristics"),
    )
    document_id = imported(profile)
    assert profile.detail(document_id)["facts"] == []
    assert len(profile.detail(document_id)["snippets"]) == 4
    parser = ProfileParser(db, settings, lambda s: SampleModel())
    job_id = parser.start(document_id, uid())["id"]
    parser.run(job_id)
    facts = profile.detail(document_id)["facts"]
    assert len(facts) == 1 and len(facts[0]["sources"]) == 4
    assert facts[0]["status"] == "pending" and facts[0]["method"] == "llm_v2"


def test_six_categories_basic_and_repeat_preserve_confirmed(v2):
    db, settings, profile = v2
    document_id, model, parser, job_id = parsed(v2, MAIN_CATEGORIES + ("basic",))
    facts = profile.detail(document_id)["facts"]
    assert len(facts) == 7
    profile.update_entries(document_id, [item(facts[0])], uid())
    before = profile.detail(document_id)["facts"]
    parser.run(job_id)
    assert model.calls == 1
    second = parser.start(document_id, uid())["id"]
    parser.run(second)
    assert model.calls == 2 and parser.status(second)["result"]["added"] == 0
    assert before == profile.detail(document_id)["facts"]
    assert Profile(db).confirmed("personal") == []
    assert len(Profile(db).confirmed("demo")) == 1
    assert FAKE_KEY.encode() not in db.path.read_bytes()


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_image_and_pdf_are_sent_as_pages_and_logs_omit_base64(v2, suffix):
    db, settings, profile = v2
    if suffix == ".pdf":
        content = (SAMPLES / "fictional-resume.pdf").read_bytes()
    else:
        image = io.BytesIO()
        Image.new("RGB", (80, 80), "white").save(image, "PNG")
        content = image.getvalue()
    doc = profile.import_file("虚构材料" + suffix, content, "resume", "demo", uid())["id"]
    model = SampleModel()
    parser = ProfileParser(db, settings, lambda s: model)
    task = parser.start(doc, uid())["id"]
    parser.run(task)
    assert parser.status(task)["status"] == "completed", parser.status(task)
    payload = model.messages[-1]["content"]
    assert any(c["type"] == "image_url" for c in payload)
    assert profile.detail(doc)["pages"] and profile.detail(doc)["facts"]
    with db.connect() as c:
        request = c.execute("SELECT request_json FROM profile_parse_attempts").fetchone()[0]
    assert "image_sha256" in request and "data:image/jpeg;base64" not in request
    assert Documents(db).original_path(doc, suffix).read_bytes() == content
    assert profile.page_path(doc, 1).is_file()


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_invalid_visual_stops_before_model(v2, suffix):
    db, settings, profile = v2
    doc = profile.import_file("bad" + suffix, b"broken", "resume", "demo", uid())["id"]
    model = SampleModel()
    parser = ProfileParser(db, settings, lambda s: model)
    task = parser.start(doc, uid())["id"]
    parser.run(task)
    assert parser.status(task)["status"] == "failed" and model.calls == 0
    assert profile.detail(doc)["facts"] == []


def test_bad_source_and_truncated_content_keep_usage_without_partial_facts(v2):
    db, settings, profile = v2
    document_id = imported(profile)

    class BadModel(SampleModel):
        def complete(self, messages, **kwargs):
            reply = super().complete(messages, **kwargs)
            body = json.loads(reply.content)
            body["entries"][0]["source_ids"] = [uid()]
            reply.content = json.dumps(body)
            return reply

    parser = ProfileParser(db, settings, lambda s: BadModel())
    task = parser.start(document_id, uid())["id"]
    parser.run(task)
    status = parser.status(task)
    assert status["status"] == "failed" and status["known_tokens"] == 160
    assert status["attempts"][0]["error_code"] == "invalid_output"
    assert not profile.detail(document_id)["facts"]


@pytest.mark.parametrize(
    "code,count", [("rate_limit", 2), ("unavailable", 2), ("timeout", 1), ("authentication", 1)]
)
def test_retries_are_finite_and_recorded(v2, monkeypatch, code, count):
    db, settings, profile = v2
    monkeypatch.setattr("job_search_agent.services.profile_parser.time.sleep", lambda _: None)

    class Failure:
        def complete(self, *args, **kwargs):
            raise ModelError(code)

    parser = ProfileParser(db, settings, lambda s: Failure())
    task = parser.start(imported(profile), uid())["id"]
    parser.run(task)
    state = parser.status(task)
    assert state["status"] == "failed" and len(state["attempts"]) == count
    assert state["unknown_usage"] == count
    parser.run(task)
    assert len(parser.status(task)["attempts"]) == count


def test_cached_response_survives_failed_business_commit(v2, monkeypatch):
    db, settings, profile = v2
    model = SampleModel()
    parser = ProfileParser(db, settings, lambda s: model)
    doc = imported(profile)
    task = parser.start(doc, uid())["id"]
    original = parser._apply
    monkeypatch.setattr(
        parser, "_apply", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError())
    )
    parser.run(task)
    assert parser.status(task)["status"] == "failed" and model.calls == 1
    monkeypatch.setattr(parser, "_apply", original)
    assert parser.start(doc, uid())["id"] == task
    parser.run(task)
    assert parser.status(task)["status"] == "completed" and model.calls == 1
    assert len(profile.detail(doc)["facts"]) == 1


def test_unknown_request_is_not_reissued(v2):
    db, settings, profile = v2
    model = SampleModel()
    parser = ProfileParser(db, settings, lambda s: model)
    task = parser.start(imported(profile), uid())["id"]
    with db.connect() as c:
        c.execute(
            """INSERT INTO profile_parse_attempts(job_id,attempt,status,request_json,created_at)
                     VALUES (?,1,'started','{}',?)""",
            (task, now()),
        )
    parser.run(task)
    assert parser.status(task)["status"] == "failed" and model.calls == 0
    assert "是否计费未知" in parser.status(task)["message"]


def test_concurrent_start_and_run_only_calls_once(v2):
    db, settings, profile = v2
    doc, op = imported(profile), uid()
    model = SampleModel()
    parser = ProfileParser(db, settings, lambda s: model)
    with ThreadPoolExecutor(2) as executor:
        jobs = list(executor.map(lambda _: parser.start(doc, op)["id"], range(2)))
        list(executor.map(parser.run, jobs))
    assert len(set(jobs)) == 1 and model.calls == 1
    assert len(profile.detail(doc)["facts"]) == 1


def test_batch_saves_visible_edits_once_and_rolls_back_on_stale(v2):
    db, settings, profile = v2
    doc, _, _, _ = parsed(v2, ("project", "skill"))
    facts = profile.detail(doc)["facts"]
    items = [item(f, description="用户核对后的完整说明") for f in facts]
    operation = uid()
    result = profile.update_entries(doc, items, operation)
    assert profile.update_entries(doc, items, operation) == result
    after = profile.detail(doc)["facts"]
    assert all(f["revision"] == 2 for f in after)
    profile.update_entries(doc, [item(f) for f in after], uid())
    assert after == profile.detail(doc)["facts"]
    stale = [item(f, status="rejected") for f in after]
    stale[-1]["revision"] = 1
    with pytest.raises(Conflict):
        profile.update_entries(doc, stale, uid())
    assert after == profile.detail(doc)["facts"]
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM profile_entry_history").fetchone()[0] == 4


def test_cross_document_and_notes_cannot_be_confirmed(v2):
    db, settings, profile = v2
    doc, _, _, _ = parsed(v2)
    f = profile.detail(doc)["facts"][0]
    other = imported(profile, corpus="personal")
    with pytest.raises(InputError):
        profile.update_entries(other, [item(f)], uid())
    notes = Documents(db).import_file("notes.txt", b"Python notes", "notes", "demo", uid())["id"]
    with pytest.raises(InputError):
        ProfileParser(db, settings).start(notes, uid())


def test_manual_project_preserves_original_and_history(v2):
    db, settings, profile = v2
    values = {
        "category": "project",
        "title": "虚构额外项目",
        "description": "Python 检索工具",
        "organization": "虚构实验室",
        "technologies": ["Python"],
    }
    key = uid()
    one = profile.add_manual("personal", values, key)
    assert profile.add_manual("personal", values, key)["id"] == one["id"]
    facts = profile.detail(one["id"])["facts"]
    assert len(facts) == 1 and facts[0]["status"] == "pending"
    assert not Profile(db).confirmed("personal")
    profile.update_entries(one["id"], [item(facts[0])], uid())
    assert len(Profile(db).confirmed("personal")) == 1


def test_legacy_parse_retains_old_rows_but_collapses_old_pending_cards(v2):
    db, settings, profile = v2
    doc = Documents(db).import_file(
        "legacy.txt", "项目经历\nPython 检索项目。".encode(), "resume", "demo", uid()
    )["id"]
    old = Documents(db).detail(doc)["facts"]
    parser = ProfileParser(db, settings, lambda s: SampleModel())
    task = parser.start(doc, uid())["id"]
    parser.run(task)
    assert parser.status(task)["status"] == "completed"
    fresh = profile.detail(doc)
    assert fresh["has_llm_entries"] and fresh["legacy_pending_count"] == len(old)
    for f in old:
        current = next(x for x in fresh["facts"] if x["id"] == f["id"])
        assert current["hidden_legacy"] and current["history"] == f["history"]
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/v2/documents/{doc}")
        assert 'data-legacy="yes" hidden' in page.text
        assert "收起旧版规则候选" in page.text


def test_new_budget_allows_image_body_without_counting_base64_as_text(v2):
    db, settings, profile = v2
    assert (
        settings.max_model_calls,
        settings.max_tool_calls,
        settings.max_input_chars,
        settings.max_output_tokens,
    ) == (40, 60, 100000, 102400)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel(settings.model_copy(update={"mode": "deepseek"}), client)
        model.complete(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + "A" * 200000},
                        }
                    ],
                }
            ]
        )
        assert captured[0]["max_tokens"] == 102400
        with pytest.raises(ModelError):
            model.complete([{"role": "user", "content": "x" * 100000}])
        with pytest.raises(ModelError):
            model.complete([{"role": "user", "content": "data:image/png;base64," + "x" * 100000}])
    assert len(captured) == 1


def test_v2_output_response_size_scales_with_requested_limit(v2):
    _, settings, _ = v2
    content = "字" * 100000
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
            )
        )
    ) as client:
        configured = settings.model_copy(update={"mode": "deepseek"})
        assert (
            DeepSeekModel(configured, client)
            .complete([{"role": "user", "content": "虚构输出容量检查"}])
            .content
            == content
        )
        with pytest.raises(ModelError):
            DeepSeekModel(configured, client).complete(
                [{"role": "user", "content": "虚构输出容量检查"}], max_tokens=2048
            )


def test_web_upload_batch_json_conflicts_and_escaping(v2):
    db, settings, profile = v2
    app = create_app(settings)
    model = SampleModel(("project", "skill"))
    app.state.profile_parser.model_factory = lambda s: model
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.get("/documents")

        def submit(path, values=None, **kwargs):
            return client.post(
                path,
                data={
                    "csrf": client.cookies["job_csrf"],
                    "operation_id": uid(),
                    "corpus": "demo",
                    **(values or {}),
                },
                **kwargs,
            )

        result = submit(
            "/v2/documents/paste",
            {"kind": "resume", "name": "虚构测试", "text": "虚构项目\nPython"},
        )
        assert result.status_code == 200 and "保存并确认" in result.text
        assert "下一步：导入岗位" in result.text
        doc = result.url.path.split("/")[-1]
        facts = profile.detail(doc)["facts"]
        items = [item(f, description="<script>alert(1)</script>") for f in facts]
        url = f"/v2/documents/{doc}/review"
        headers = {"Accept": "application/json"}
        saved = submit(url, {"items": json.dumps(items)}, headers=headers)
        assert saved.status_code == 200 and saved.json()["counts"]["confirmed"] == 2
        stale = submit(url, {"items": json.dumps(items)}, headers=headers)
        assert stale.status_code == 409 and "message" in stale.json()
        page = client.get(f"/v2/documents/{doc}")
        assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
        blocked = submit(
            url, {"items": json.dumps(items)}, headers={**headers, "Origin": "https://evil.example"}
        )
        assert blocked.status_code == 400
        assert FAKE_KEY not in page.text
        assert "2 条" in client.get("/profile?corpus=demo").text
        assert "尚未确认任何事实" in client.get("/profile?corpus=personal").text
