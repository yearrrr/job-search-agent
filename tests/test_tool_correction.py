"""工具参数纠错、旧失败任务恢复与预算边界回归；不访问外部模型。"""

import json

import pytest
from fastapi.testclient import TestClient

from job_search_agent.agent import preparation
from job_search_agent.agent.experience_tools import ALL_CATEGORIES, rejection_feedback
from job_search_agent.agent.model import ModelError, ModelReply, ToolCall, Usage
from job_search_agent.agent.preparation import PreparationMock
from job_search_agent.main import create_app
from job_search_agent.schemas import InputError
from job_search_agent.services.common import uid
from job_search_agent.services.documents import Documents
from job_search_agent.services.runs_v2 import Runs
from job_search_agent.services.tasks import make_snapshot


def setup(scenario, **limits):
    db, tasks, job_id, _ = scenario
    settings = tasks.settings.model_copy(update={"max_input_chars": 100000, **limits})
    runs = Runs(db, settings)
    run_id = runs.create("preparation", "demo", job_id, make_snapshot(db, job_id), "mock", uid())[
        "id"
    ]
    return db, runs, runs.detail(run_id)


def reply_call(identifier, name, arguments):
    return ModelReply(
        tool_calls=[ToolCall(id=identifier, name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
    )


class CorrectsIDs:
    def __init__(self, snapshot):
        self.snapshot, self.calls, self.feedback, self.histories = snapshot, 0, None, []
        self.selected = snapshot["facts"][0]["id"]

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.histories.append(messages)
        if self.calls == 1:
            return reply_call(
                "search",
                "search_experiences",
                {"query": "", "categories": ALL_CATEGORIES, "limit": 12},
            )
        if self.calls == 2:
            return reply_call(
                "bad",
                "get_experience_details",
                {"experience_ids": [self.selected, "invented-one", "invented-two"]},
            )
        if self.calls == 3:
            self.feedback = json.loads(messages[-1]["content"])["error"]
            assert self.feedback["invalid_ids"] == ["invented-one", "invented-two"]
            assert self.selected in {f["id"] for f in self.feedback["available_experiences"]}
            return reply_call(
                "fixed", "get_experience_details", {"experience_ids": [self.selected]}
            )
        if self.calls == 4:
            return reply_call("proof", "get_evidence", {"fact_ids": [self.selected]})
        if self.calls == 5:
            return PreparationMock(self.snapshot).complete([{"role": "tool", "content": "{}"}])
        raise AssertionError("unexpected extra model request")


def force_legacy_stop(monkeypatch):
    def stop(*args, **kwargs):
        raise ModelError("tool_rejected")

    monkeypatch.setattr(preparation, "rejection_feedback", stop)


def test_wrong_ids_get_feedback_then_finish_without_reading_partial_invalid_batch(scenario):
    db, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    runs.run(run["id"], model)
    final = runs.detail(run["id"])
    assert final["status"] == "completed"
    assert final["tool_errors"] == 1
    assert final["result"]["evidence_ids"] == [model.selected]
    assert model.calls == 5 and final["tool_calls"] == 4
    with db.connect() as c:
        failed = c.execute(
            "SELECT * FROM v2_run_attempts WHERE run_id=? AND step='tool:2'", (run["id"],)
        ).fetchone()
        assert failed["status"] == "failed" and failed["result_json"] is None
    # 错误提示不伪装为工具事实；编号存在不等于读取成功。
    assert "facts" not in json.loads(model.histories[2][-1]["content"])
    assert "invented-one" not in final["result"]["evidence_ids"]


def test_old_failed_run_resumes_from_cached_replies_with_original_budget(scenario, monkeypatch):
    _, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    with monkeypatch.context() as patch:
        force_legacy_stop(patch)
        runs.run(run["id"], model)
    before = runs.detail(run["id"])
    assert before["status"] == "failed" and before["error_code"] == "tool_rejected"
    assert before["model_calls"] == 2 and before["tool_calls"] == 2
    assert runs.resume(run["id"])["status"] == "queued"
    runs.run(run["id"], model)
    after = runs.detail(run["id"])
    assert after["status"] == "completed" and model.calls == 5
    assert after["limits"] == before["limits"]
    assert after["attempts"][: len(before["attempts"])] == before["attempts"]
    assert runs.resume(run["id"])["status"] == "completed"
    runs.run(run["id"], model)
    assert model.calls == 5


def test_recovery_cannot_reset_model_budget(scenario, monkeypatch):
    _, runs, run = setup(scenario, max_model_calls=3)
    model = CorrectsIDs(run["snapshot"])
    with monkeypatch.context() as patch:
        force_legacy_stop(patch)
        runs.run(run["id"], model)
    runs.resume(run["id"])
    runs.run(run["id"], model)
    stopped = runs.detail(run["id"])
    assert stopped["error_code"] == "budget" and stopped["model_calls"] == 3
    with pytest.raises(InputError):
        runs.resume(run["id"])


class BadCalls:
    def __init__(self, name, arguments, batch=1):
        self.name, self.arguments, self.calls, self.batch = name, arguments, 0, batch

    def complete(self, messages, **kwargs):
        self.calls += 1
        return ModelReply(
            tool_calls=[
                ToolCall(id=f"bad-{self.calls}-{i}", name=self.name, arguments=self.arguments)
                for i in range(self.batch)
            ]
        )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_experience_details", {"experience_ids": ["made-up"]}),
        ("get_experience_details", {"experience_ids": "wrong-type"}),
        ("get_evidence", {"fact_ids": []}),
        ("search_experiences", {"query": "", "categories": ["private"], "limit": 12}),
        ("read_arbitrary_file", {"path": "unavailable"}),
    ],
)
def test_repeated_invalid_requests_are_counted_and_bounded(scenario, name, arguments):
    _, runs, run = setup(scenario)
    model = BadCalls(name, arguments)
    runs.run(run["id"], model)
    final = runs.detail(run["id"])
    assert final["error_code"] == "tool_correction_limit"
    assert final["tool_errors"] == final["tool_calls"] == 4
    assert model.calls == 4 and final["cache_hits"] == 0
    assert not final["can_recover_tools"]
    with pytest.raises(InputError):
        runs.resume(run["id"])


def test_invalid_requests_still_obey_smaller_tool_budget(scenario):
    _, runs, run = setup(scenario, max_tool_calls=1)
    runs.run(run["id"], BadCalls("get_evidence", {"fact_ids": ["bad"]}))
    final = runs.detail(run["id"])
    assert final["error_code"] == "budget" and final["tool_calls"] == 1


def test_one_model_batch_cannot_bypass_total_correction_limit(scenario):
    _, runs, run = setup(scenario)
    model = BadCalls("get_experience_details", {"experience_ids": ["invalid"]}, batch=6)
    runs.run(run["id"], model)
    final = runs.detail(run["id"])
    assert final["model_calls"] == 1 and final["tool_errors"] == 4
    assert final["error_code"] == "tool_correction_limit"


def test_feedback_never_reveals_other_corpus_or_unconfirmed_facts(scenario):
    db, runs, run = setup(scenario)
    with db.connect() as c:
        foreign = c.execute("""SELECT f.id FROM facts f JOIN documents d ON d.id=f.document_id
            WHERE d.corpus='personal' LIMIT 1""").fetchone()[0]
    known = {f["id"] for f in run["snapshot"]["facts"]}
    selected = next(iter(known))
    out = rejection_feedback(
        run["snapshot"],
        "get_experience_details",
        {"experience_ids": [foreign]},
        {selected},
        set(),
        1,
    )["error"]
    assert {f["id"] for f in out["available_experiences"]} == {selected}
    assert foreign not in {f["id"] for f in out["available_experiences"]}
    # 未读详情时不开放来源，只提示先完成必要步骤。
    out = rejection_feedback(
        run["snapshot"], "get_evidence", {"fact_ids": [selected]}, {selected}, set(), 1
    )["error"]
    assert out["available_experiences"] == []
    assert out["read_details_first"] == [selected]


def test_stale_profile_cannot_be_resumed_or_receive_repair_suggestions(scenario, monkeypatch):
    db, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    with monkeypatch.context() as patch:
        force_legacy_stop(patch)
        runs.run(run["id"], model)
    fact = run["snapshot"]["facts"][0]
    Documents(db).update_fact(
        fact["id"], fact["text"], fact["category"], "rejected", fact["revision"], uid()
    )
    with pytest.raises(InputError):
        runs.resume(run["id"])
    assert model.calls == 2
    assert runs.detail(run["id"])["error_code"] == "tool_rejected"


def test_storage_replay_after_correction_does_not_repeat_requests(scenario, monkeypatch):
    _, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    actual = runs._status

    def fail_completed(run_id, status, *args, **kwargs):
        if status == "completed":
            raise OSError("simulated storage error")
        return actual(run_id, status, *args, **kwargs)

    monkeypatch.setattr(runs, "_status", fail_completed)
    runs.run(run["id"], model)
    before = runs.detail(run["id"])
    assert before["error_code"] == "storage"
    monkeypatch.setattr(runs, "_status", actual)
    runs.resume(run["id"])
    runs.run(run["id"], model)
    assert runs.detail(run["id"])["status"] == "completed" and model.calls == 5
    assert runs.detail(run["id"])["attempts"] == before["attempts"]


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_resume_button_is_explicit_csrf_protected_and_get_does_not_retry(scenario, monkeypatch):
    db, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    with monkeypatch.context() as patch:
        force_legacy_stop(patch)
        runs.run(run["id"], model)
    app = create_app(runs.settings)
    app.state.v2_runs.model_factory = lambda settings: model
    # 显式注入脚本模型以覆盖 mock 默认分支，同时保持页面操作经过真实业务入口。
    original_run = app.state.v2_runs.run
    app.state.v2_runs.run = lambda run_id: original_run(run_id, model)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get(f"/v2/runs/{run['id']}")
        assert "纠正参数并继续本次分析" in page.text and model.calls == 2
        path = f"/v2/runs/{run['id']}/continue"
        assert client.post(path, headers={"Accept": "application/json"}).status_code == 400
        result = client.post(
            path, data={"csrf": client.cookies["job_csrf"]}, headers={"Accept": "application/json"}
        )
        assert result.status_code == 200
        assert client.get(f"/v2/runs/{run['id']}/status").json()["status"] == "completed"
        assert model.calls == 5
        client.post(
            path, data={"csrf": client.cookies["job_csrf"]}, headers={"Accept": "application/json"}
        )
        assert model.calls == 5


def test_uncertain_tool_attempt_is_never_converted_into_a_correctable_rejection(
    scenario, monkeypatch
):
    db, runs, run = setup(scenario)
    model = CorrectsIDs(run["snapshot"])
    with monkeypatch.context() as patch:
        force_legacy_stop(patch)
        runs.run(run["id"], model)
    with db.connect() as c:
        c.execute(
            "UPDATE v2_run_attempts SET status='started',error_code=NULL WHERE run_id=? AND step='tool:2'",
            (run["id"],),
        )
    runs.resume(run["id"])
    runs.run(run["id"], model)
    assert runs.detail(run["id"])["error_code"] == "uncertain_attempt"
    assert model.calls == 2
