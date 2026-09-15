import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from job_search_agent.agent.experience_tools import ALL_CATEGORIES, execute, fresh
from job_search_agent.agent.model import ModelError, ModelReply, ToolCall, Usage
from job_search_agent.agent.preparation import PreparationMock, validate_result
from job_search_agent.config import Settings
from job_search_agent.db import Database
from job_search_agent.main import create_app
from job_search_agent.schemas import FIELD_LABELS, InputError
from job_search_agent.services.common import uid
from job_search_agent.services.jd_v2 import JD
from job_search_agent.services.runs_v2 import Runs
from job_search_agent.services.tasks import TaskStopped, make_snapshot


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "v2", api_key=SecretStr("synthetic-test-key"))
    db = Database(settings.data_dir)
    db.initialize()
    return db, settings, Runs(db, settings)


def payload(source, **changes):
    out = {
        "company": "虚构星河科技",
        "title": "AI 应用工程师",
        "job_code": "",
        "batch": "",
        "source_url": "",
        "raw_jd": "虚构星河科技\nAI 应用工程师\n任职要求：熟悉 Python",
        "source_ids": [source],
        "notes": "来源链接及招聘批次未提供。",
        "fields": {
            k: {
                "text": "熟悉 Python" if k == "requirements" else "",
                "source_ids": [source] if k == "requirements" else [],
            }
            for k in FIELD_LABELS
        },
    }
    out.update(changes)
    return out


class Fixed:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return ModelReply(
            content=json.dumps(self.result, ensure_ascii=False),
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


def import_text(db, settings, runs):
    jd = JD(db, settings, runs)
    result = jd.import_source("demo", "虚构星河科技 AI 应用工程师 任职要求：熟悉 Python", [], uid())
    run = runs.detail(result["id"])
    return jd, run


def test_jd_llm_review_preserves_source_and_deduplicates(env):
    db, settings, runs = env
    jd, run = import_text(db, settings, runs)
    model = Fixed(payload(run["target_id"]))
    runs.run(run["id"], model)
    done = runs.detail(run["id"])
    assert done["status"] == "completed" and done["known_tokens"] == 30
    assert done["result"]["source_url"] == done["result"]["batch"] == ""
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    again = jd.import_source("demo", "虚构星河科技 AI 应用工程师 任职要求：熟悉 Python", [], uid())
    assert again["id"] == run["id"]
    runs.run(again["id"], model)
    assert model.calls == 1
    values = {**done["result"], **{k: done["result"]["fields"][k]["text"] for k in FIELD_LABELS}}
    op = uid()
    saved = jd.confirm(run["id"], values, op)
    assert jd.confirm(run["id"], values, op) == saved
    from job_search_agent.services.jobs import Jobs

    job = Jobs(db).detail(saved["id"])
    assert job["fields"]["requirements"]["method"] == "llm"
    assert job["jd_run_id"] == run["id"] and job["status"] == "saved"
    with pytest.raises(InputError):
        jd.confirm(run["id"], {**values, "company": "不能覆盖"}, uid())


@pytest.mark.parametrize(
    "change",
    [
        {"source_ids": ["foreign"]},
        {"source_url": "javascript:alert(1)"},
        {"source_ids": []},
        {"extra": "no"},
        {"raw_jd": ""},
    ],
)
def test_bad_jd_result_cannot_create_job(env, change):
    db, settings, runs = env
    _, run = import_text(db, settings, runs)
    runs.run(run["id"], Fixed(payload(run["target_id"], **change)))
    assert runs.detail(run["id"])["error_code"] == "invalid_output"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_multiple_images_are_visual_inputs_and_logs_omit_base64(env):
    db, settings, runs = env
    image = io.BytesIO()
    Image.new("RGB", (30, 30), "white").save(image, "PNG")
    jd = JD(db, settings, runs)
    result = jd.import_source(
        "personal", "", [("one.png", image.getvalue()), ("two.png", image.getvalue())], uid()
    )
    run = runs.detail(result["id"])
    messages, sources = jd.messages(run["target_id"])
    assert len(sources) == 2
    assert sum(b["type"] == "image_url" for b in messages[1]["content"]) == 2
    output = payload(next(iter(sources)))
    runs.run(run["id"], Fixed(output))
    done = runs.detail(run["id"])
    assert done["status"] == "completed"
    assert "base64," not in json.dumps(done["attempts"])
    assert len(jd.source(run["target_id"])["files"]) == 2


@pytest.mark.parametrize(
    "files",
    [
        [("bad.png", b"not image")],
        [("bad.svg", b"<svg>")],
        [("big.png", b"x" * (5 * 1024 * 1024 + 1))],
        [("a.png", b"x")] * 7,
    ],
)
def test_invalid_images_never_call_model(env, files):
    db, settings, runs = env
    with pytest.raises(InputError):
        JD(db, settings, runs).import_source("demo", "", files, uid())
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_run_attempts").fetchone()[0] == 0


def test_successful_paid_reply_survives_final_storage_failure(env, monkeypatch):
    db, settings, runs = env
    _, run = import_text(db, settings, runs)
    model = Fixed(payload(run["target_id"]))
    actual = runs._status

    def fail_once(identifier, status, *args, **kwargs):
        if status == "completed":
            raise OSError("simulated storage interruption")
        return actual(identifier, status, *args, **kwargs)

    monkeypatch.setattr(runs, "_status", fail_once)
    runs.run(run["id"], model)
    assert runs.detail(run["id"])["error_code"] == "storage"
    monkeypatch.setattr(runs, "_status", actual)
    runs.run(run["id"], model)
    assert runs.detail(run["id"])["status"] == "completed"
    assert model.calls == 1


def test_unknown_attempt_is_not_reissued(env):
    db, settings, runs = env
    jd, run = import_text(db, settings, runs)
    messages, _ = jd.messages(run["target_id"])

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runs.attempt(
            run,
            "model:1:0",
            "model",
            {"messages": messages, "tools": None, "max_tokens": settings.max_output_tokens},
            interrupted,
        )
    model = Fixed(payload(run["target_id"]))
    runs.run(run["id"], model)
    assert runs.detail(run["id"])["error_code"] == "uncertain_attempt"
    assert model.calls == 0


def prep_run(scenario, mode="mock", **limits):
    db, old_tasks, job, _ = scenario
    settings = old_tasks.settings.model_copy(update={**limits, "max_input_chars": 100000})
    runs = Runs(db, settings)
    run_id = runs.create("preparation", "demo", job, make_snapshot(db, job), mode, uid())["id"]
    return db, runs, runs.detail(run_id)


def test_prep_uses_three_tools_and_is_read_only(scenario):
    db, runs, run = prep_run(scenario)
    with db.connect() as c:
        before = c.execute("SELECT count(*) FROM facts").fetchone()[0]
    runs.run(run["id"])
    done = runs.detail(run["id"])
    assert done["status"] == "completed"
    assert done["tool_calls"] == 3 and done["model_calls"] == 4
    names = [a["request"]["name"] for a in done["attempts"] if a["kind"] == "tool"]
    assert names == ["search_experiences", "get_experience_details", "get_evidence"]
    assert all(i["status"] == "unknown" for i in done["result"]["items"])
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM facts").fetchone()[0] == before


def test_tools_reject_foreign_pending_and_unseen_ids(scenario):
    db, runs, run = prep_run(scenario)
    snapshot = run["snapshot"]
    ids = {f["id"] for f in snapshot["facts"]}
    result = execute(
        db,
        snapshot,
        "search_experiences",
        {"query": "", "categories": ALL_CATEGORIES, "limit": 12},
        set(),
        set(),
    )
    assert {f["id"] for f in result["experiences"]} == ids
    with pytest.raises(ModelError):
        execute(db, snapshot, "get_experience_details", {"experience_ids": list(ids)}, set(), set())
    with pytest.raises(ModelError):
        execute(db, snapshot, "get_evidence", {"fact_ids": ["foreign"]}, ids, ids)
    assert execute(
        db, snapshot, "get_experience_details", {"experience_ids": list(ids)}, ids, set()
    )["facts"]


def test_newly_confirmed_fact_invalidates_snapshot(scenario):
    db, runs, run = prep_run(scenario)
    from job_search_agent.services.documents import Documents

    docs = Documents(db)
    doc = docs.import_file("extra.txt", "新增已确认项目".encode(), "project", "demo", uid())
    f = docs.detail(doc["id"])["facts"][0]
    docs.update_fact(f["id"], f["text"], f["category"], "confirmed", f["revision"], uid())
    with pytest.raises(TaskStopped):
        fresh(db, run["snapshot"])
    runs.run(run["id"])
    assert runs.detail(run["id"])["error_code"] == "stale_evidence"
    assert runs.detail(run["id"])["model_calls"] == 0


class Repeats:
    def __init__(self):
        self.calls = 0
        self.messages = []

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        return ModelReply(
            tool_calls=[
                ToolCall(
                    id=f"q{self.calls}",
                    name="search_experiences",
                    arguments={"query": " Python  ", "categories": ["project"], "limit": 8},
                )
            ]
        )


def test_duplicate_retrieval_is_cached_compressed_and_bounded(scenario):
    _, runs, run = prep_run(scenario)
    model = Repeats()
    runs.run(run["id"], model)
    done = runs.detail(run["id"])
    assert done["error_code"] == "repeated_search"
    assert done["tool_calls"] == 3 and done["cache_hits"] == 2
    assert "cache_ref" in model.messages[-1]["content"]
    assert "experiences" not in model.messages[-1]["content"]


def test_resume_preserves_model_budget(scenario):
    _, runs, run = prep_run(scenario, max_model_calls=1)
    runs.run(run["id"])
    done = runs.detail(run["id"])
    assert done["error_code"] == "budget" and done["model_calls"] == 1
    runs.run(run["id"])
    assert runs.detail(run["id"])["model_calls"] == 1


def test_analysis_cannot_cite_unread_evidence_or_turn_unknown_into_deficit(scenario):
    _, _, run = prep_run(scenario)
    output = json.loads(
        PreparationMock(run["snapshot"]).complete([{"role": "tool", "content": "{}"}]).content
    )
    first = output["items"][0]
    first.update(status="supported", fact_ids=["foreign"], question="")
    with pytest.raises(ModelError):
        validate_result(json.dumps(output), run["snapshot"]["requirements"], set())
    first.update(status="unknown", fact_ids=[], question="")
    with pytest.raises(ModelError):
        validate_result(json.dumps(output), run["snapshot"]["requirements"], set())


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_status_update_is_inline_and_does_not_start_analysis(scenario):
    db, tasks, job_id, _ = scenario
    with TestClient(create_app(tasks.settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200 and 'id="preparation-choice"' in page.text
        token = client.cookies["job_csrf"]
        response = client.post(
            f"/jobs/{job_id}/status",
            data={"csrf": token, "revision": 1, "status": "preparing", "operation_id": uid()},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["ask_preparation"] is True
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM v2_runs").fetchone()[0] == 0
        assert client.get(f"/jobs/{job_id}").status_code == 200
        started = client.post(
            f"/v2/jobs/{job_id}/prepare",
            data={"csrf": token, "engine": "mock", "operation_id": uid()},
            headers={"Accept": "application/json"},
        )
        assert started.status_code == 200
        result = client.get(started.json()["url"])
        assert result.status_code == 200 and "练习完成标准" in result.text
        assert "离线演示" in result.text


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_jd_page_confirm_and_csrf(env):
    db, settings, runs = env
    _, run = import_text(db, settings, runs)
    runs.run(run["id"], Fixed(payload(run["target_id"])))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/v2/runs/{run['id']}")
        assert page.status_code == 200 and "核对 LLM 提取结果" in page.text
        assert "synthetic-test-key" not in page.text
        assert client.get("/jobs/new?corpus=demo").status_code == 200
        result = runs.detail(run["id"])["result"]
        values = {
            **result,
            **{k: result["fields"][k]["text"] for k in FIELD_LABELS},
            "operation_id": uid(),
        }
        path = f"/v2/runs/{run['id']}/confirm-jd"
        assert (
            client.post(path, data=values, headers={"Accept": "application/json"}).status_code
            == 400
        )
        values["csrf"] = client.cookies["job_csrf"]
        result = client.post(path, data=values, headers={"Accept": "application/json"})
        assert result.status_code == 200
        assert client.get(result.json()["url"]).status_code == 200
        page = client.get(f"/v2/runs/{run['id']}")
        assert "查看原始输入" in page.text


def test_single_fenced_json_preamble_is_accepted_but_ambiguous_or_truncated_output_is_not():
    from job_search_agent.agent.json_output import json_content

    fence = chr(96) * 3
    obj = '{"ok": true}'
    assert json_content("Now the result.\n\n" + fence + "json\n" + obj + "\n" + fence) == obj
    assert json_content(fence + "json\n" + obj + "\n" + fence) == obj
    for bad in [
        fence + "json\n" + obj,
        fence + "json\n" + obj + "\n" + fence + "\n" + fence + "json\n{}\n" + fence,
        "{}\n" + fence + "json\n" + obj + "\n" + fence,
    ]:
        with pytest.raises(ValueError):
            json.loads(json_content(bad))


def test_optional_condition_may_not_be_marked_as_hard_mismatch(scenario):
    _, _, run = prep_run(scenario)
    output = json.loads(
        PreparationMock(run["snapshot"]).complete([{"role": "tool", "content": "{}"}]).content
    )
    requirement = next(r for r in run["snapshot"]["requirements"] if r["kind"] == "加分项")
    item = next(i for i in output["items"] if i["requirement_id"] == requirement["id"])
    fact_id = run["snapshot"]["facts"][0]["id"]
    item.update(status="mismatch", fact_ids=[fact_id], question="")
    with pytest.raises(ModelError):
        validate_result(json.dumps(output), run["snapshot"]["requirements"], {fact_id})


def test_browser_line_endings_do_not_mark_untouched_jd_as_user_edit(env):
    db, settings, runs = env
    jd, run = import_text(db, settings, runs)
    data = payload(run["target_id"])
    data["fields"]["requirements"]["text"] = "熟悉 Python\n使用 Git"
    runs.run(run["id"], Fixed(data))
    values = {**data, **{k: data["fields"][k]["text"].replace("\n", "\r\n") for k in FIELD_LABELS}}
    saved = jd.confirm(run["id"], values, uid())
    from job_search_agent.services.jobs import Jobs

    assert Jobs(db).detail(saved["id"])["fields"]["requirements"]["method"] == "llm"


def test_transient_retries_are_counted_and_budget_cannot_be_bypassed(env):
    db, settings, _ = env
    runs = Runs(db, settings.model_copy(update={"max_model_calls": 1, "max_retries": 3}))
    _, run = import_text(db, settings, runs)
    model = Fixed(ModelError("rate_limit"))
    runs.run(run["id"], model)
    done = runs.detail(run["id"])
    assert model.calls == done["model_calls"] == 1
    assert done["error_code"] == "budget"


def test_prep_replay_recovers_after_storage_without_new_model_or_tool_attempts(
    scenario, monkeypatch
):
    _, runs, run = prep_run(scenario)
    actual = runs._status

    def fail_once(identifier, status, *args, **kwargs):
        if status == "completed":
            raise OSError("simulated storage interruption")
        return actual(identifier, status, *args, **kwargs)

    monkeypatch.setattr(runs, "_status", fail_once)
    runs.run(run["id"])
    before = runs.detail(run["id"])
    assert before["error_code"] == "storage"
    monkeypatch.setattr(runs, "_status", actual)
    model = Fixed(AssertionError("must reuse paid result"))
    runs.run(run["id"], model)
    after = runs.detail(run["id"])
    assert after["status"] == "completed"
    assert model.calls == 0
    assert after["model_calls"] == before["model_calls"]
    assert after["tool_calls"] == before["tool_calls"]


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_status_response_replay_repeats_choice_without_duplicate_state_write(scenario):
    db, tasks, job_id, _ = scenario
    with TestClient(create_app(tasks.settings), base_url="http://127.0.0.1") as client:
        client.get(f"/jobs/{job_id}")
        values = {
            "csrf": client.cookies["job_csrf"],
            "revision": 1,
            "status": "preparing",
            "operation_id": uid(),
        }
        first = client.post(
            f"/jobs/{job_id}/status", data=values, headers={"Accept": "application/json"}
        )
        again = client.post(
            f"/jobs/{job_id}/status", data=values, headers={"Accept": "application/json"}
        )
        assert first.json() == again.json()
        assert again.json()["ask_preparation"]
        with db.connect() as c:
            assert (
                c.execute("SELECT count(*) FROM job_history WHERE job_id=?", (job_id,)).fetchone()[
                    0
                ]
                == 2
            )
