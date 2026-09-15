import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import SecretStr
from test_stage_three import respond, start

from job_search_agent.agent.business_model import BusinessDemoModel
from job_search_agent.agent.model import DeepSeekModel, ModelError, ModelReply, ToolCall, Usage
from job_search_agent.agent.privacy import mask_contacts
from job_search_agent.services.common import uid
from job_search_agent.services.documents import Documents
from job_search_agent.services.drafts import Drafts
from job_search_agent.services.profile import Profile
from job_search_agent.services.tasks import Tasks

KEY = "stage-four-fictional-key-NEVER-REAL"


def configuration(settings, **overrides):
    return settings.model_copy(
        update={"mode": "deepseek", "api_key": SecretStr(KEY), "max_retries": 2, **overrides}
    )


def provider_reply(reply):
    message = {"role": "assistant", "content": reply.content}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in reply.tool_calls
        ]
    return {
        "choices": [
            {"finish_reason": "tool_calls" if reply.tool_calls else "stop", "message": message}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def assert_secret_absent(db, detail):
    assert KEY not in json.dumps(detail, ensure_ascii=False)
    for name in ("app.sqlite3", "workflow.sqlite3"):
        assert KEY.encode() not in (db.data_dir / name).read_bytes()


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (400, "invalid_request", 1),
        (401, "authentication", 1),
        (402, "balance", 1),
        (422, "invalid_request", 1),
        (429, "rate_limit", 3),
        (500, "unavailable", 3),
        (503, "unavailable", 3),
        (302, "http_error", 1),
        (418, "http_error", 1),
    ],
)
def test_real_workflow_http_failure_matrix(settings, scenario, caplog, status, code, expected):
    db, _, job, _ = scenario
    configured = configuration(settings)
    tasks, seen = Tasks(db, configured), []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(status, text=KEY, headers={"location": "https://attacker.example"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel(configured, client)
        task_id = tasks.create(job, "deepseek", uid())["id"]
        tasks.run(task_id, model)
        detail = tasks.detail(task_id)
        assert (detail["status"], detail["error_code"]) == ("failed", code)
        assert len(seen) == detail["model_calls"] == expected
        assert detail["tool_calls"] == 0
        assert detail["tokens"] is None and detail["unknown_usage_calls"] == expected
        assert detail["limits"]["max_retries"] == 2
        assert all(str(url) == DeepSeekModel.ENDPOINT for url in seen)
        assert_secret_absent(db, detail)
        assert KEY not in caplog.text
        tasks.run(task_id, model)
        assert len(seen) == expected


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ConnectError, "network"),
        (httpx.ReadError, "network"),
    ],
)
def test_ambiguous_transport_does_not_retry(settings, scenario, failure, code):
    db, _, job, _ = scenario
    configured = configuration(settings, max_retries=3)
    tasks, seen = Tasks(db, configured), []

    def handler(request):
        seen.append(1)
        raise failure(KEY, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        task_id = tasks.create(job, "deepseek", uid())["id"]
        tasks.run(task_id, DeepSeekModel(configured, client))
    detail = tasks.detail(task_id)
    assert detail["error_code"] == code
    assert len(seen) == detail["model_calls"] == 1
    assert_secret_absent(db, detail)


def test_transient_retry_then_real_tools_and_partial_usage(settings, scenario):
    db, _, job, _ = scenario
    configured = configuration(settings)
    tasks, seen = Tasks(db, configured), []

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429)
        body = json.loads(request.content)
        reply = BusinessDemoModel().complete(body["messages"])
        return httpx.Response(200, json=provider_reply(reply))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel(configured, client)
        task_id = tasks.create(job, "deepseek", uid())["id"]
        tasks.run(task_id, model)
        detail = tasks.detail(task_id)
        assert detail["status"] == "waiting_input", detail["error_code"]
        assert (detail["model_calls"], detail["tool_calls"]) == (3, 5)
        detail = respond(tasks, task_id, "skip_all", model=model)
    assert detail["status"] == "waiting_confirmation", detail["error_code"]
    assert detail["view"]["wait"]["kind"] == "draft"
    assert len(seen) == detail["model_calls"] == 4
    assert detail["tokens"] is None
    assert detail["known_tokens"] == 36 and detail["unknown_usage_calls"] == 1
    retries = [a for a in detail["attempts"] if ":retry:" in a["step_key"]]
    assert [a["step_key"] for a in retries] == ["model:0:retry:1"]


def test_retry_obeys_total_budget_and_legacy_policy(settings, scenario):
    db, _, job, _ = scenario
    tasks = Tasks(db, configuration(settings, max_model_calls=2, max_retries=3))

    class Limited:
        def complete(self, *args, **kwargs):
            raise ModelError("rate_limit")

    task_id = tasks.create(job, "deepseek", uid())["id"]
    tasks.run(task_id, Limited())
    detail = tasks.detail(task_id)
    assert detail["error_code"] == "budget" and detail["model_calls"] == 2
    old_id = tasks.create(job, "deepseek", uid())["id"]
    with db.connect() as connection:
        limits = tasks.detail(old_id)["limits"]
        del limits["max_retries"]
        connection.execute(
            "UPDATE agent_tasks SET limits_json=? WHERE id=?", (json.dumps(limits), old_id)
        )
    tasks.run(old_id, Limited())
    old = tasks.detail(old_id)
    assert old["error_code"] == "rate_limit" and old["model_calls"] == 1


@pytest.mark.parametrize(
    "mutation,known",
    [
        ("truncated", 12),
        ("bad_json", None),
        ("bad_arguments", 12),
        ("secret_echo", 12),
        ("negative_usage", None),
    ],
)
def test_invalid_provider_output_is_stopped_with_known_usage(settings, scenario, mutation, known):
    db, _, job, _ = scenario
    configured = configuration(settings)
    tasks = Tasks(db, configured)
    payload = provider_reply(ModelReply(content="OK"))
    if mutation == "truncated":
        payload["choices"][0]["finish_reason"] = "length"
    if mutation == "bad_arguments":
        payload["choices"][0] = {
            "finish_reason": "tool_calls",
            "message": {
                "tool_calls": [
                    {
                        "id": "bad",
                        "type": "function",
                        "function": {"name": "search_confirmed_facts", "arguments": "{"},
                    }
                ]
            },
        }
    if mutation == "secret_echo":
        payload["choices"][0]["message"]["content"] = KEY
    if mutation == "negative_usage":
        payload["usage"]["total_tokens"] = -1

    def handler(request):
        return (
            httpx.Response(200, content=b"{")
            if mutation == "bad_json"
            else httpx.Response(200, json=payload)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        task_id = tasks.create(job, "deepseek", uid())["id"]
        tasks.run(task_id, DeepSeekModel(configured, client))
    detail = tasks.detail(task_id)
    assert detail["error_code"] == "invalid_output"
    assert detail["model_calls"] == 1 and detail["tool_calls"] == 0
    assert detail["attempts"][0]["total_tokens"] == known
    assert_secret_absent(db, detail)


def test_stream_size_and_elapsed_limits_stop_reading(settings):
    chunks = []

    class Large(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                chunks.append(1)
                yield b"x" * 65536

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(0.02)
            yield b"{}"

    for stream, timeout, code in ((Large(), 5, "invalid_output"), (Slow(), 0.005, "timeout")):
        config = configuration(settings, timeout_seconds=timeout)
        with httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
        ) as client:
            with pytest.raises(ModelError) as caught:
                DeepSeekModel(config, client).complete([{"role": "user", "content": "虚构"}])
        assert caught.value.code == code
    assert len(chunks) == 5


def test_concurrent_create_run_and_confirm_are_unique(scenario):
    db, tasks, job, _ = scenario
    operation = uid()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: tasks.create(job, "mock", operation)["id"], range(4)))
        assert len(set(ids)) == 1
        list(pool.map(lambda _: tasks.run(ids[0]), range(4)))
    detail = tasks.detail(ids[0])
    assert detail["status"] == "waiting_input"
    assert (detail["model_calls"], detail["tool_calls"]) == (2, 5)
    detail = respond(tasks, ids[0], "skip_all")
    with ThreadPoolExecutor(max_workers=4) as pool:
        material_ids = list(
            pool.map(
                lambda _: Drafts(db).confirm(
                    ids[0],
                    detail["draft"]["revision"],
                    detail["view"]["snapshot"],
                    {"sections": [], "evidence": {}},
                ),
                range(4),
            )
        )
    assert len(set(material_ids)) == 1
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 1


def test_excessive_tool_calls_stop_at_budget(settings, scenario):
    db, _, job, facts = scenario
    tasks = Tasks(db, settings.model_copy(update={"max_tool_calls": 2}))

    class Flood:
        def complete(self, *args, **kwargs):
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id=str(i),
                        name="search_confirmed_facts",
                        arguments={"category": "project", "query": "Python", "limit": 8},
                    )
                    for i in range(20)
                ]
            )

    task_id, detail = start((db, tasks, job, facts), Flood())
    assert detail["error_code"] == "budget"
    assert (detail["model_calls"], detail["tool_calls"]) == (1, 2)


def test_lost_checkpoint_does_not_start_paid_work_again(scenario):
    db, tasks, _, _ = scenario
    task_id, detail = start(scenario)
    tasks.submit(task_id, detail["revision"], "skip_all", "", "", 0, uid())
    (db.data_dir / "workflow.sqlite3").rename(db.data_dir / "workflow.retained-for-test.sqlite3")
    tasks.run(task_id)
    stopped = tasks.detail(task_id)
    assert stopped["error_code"] == "checkpoint_missing"
    assert stopped["model_calls"] == detail["model_calls"]


def test_fictional_prefix_answer_still_creates_pending_fact(scenario):
    db, tasks, _, _ = scenario
    task_id, _ = start(scenario)
    text = "虚构测试：使用 Docker 部署练习服务。"
    detail = respond(tasks, task_id, "answer", text=text, category="project")
    assert detail["view"]["wait"]["kind"] == "fact"
    document = Documents(db).detail(detail["view"]["wait"]["document_id"])
    assert [(f["text"], f["status"]) for f in document["facts"]] == [(text, "pending")]
    assert len(Profile(db).confirmed("demo")) == 2


def test_substitute_model_cannot_write_key_to_checkpoint(settings, scenario):
    db, _, job, facts = scenario
    tasks = Tasks(db, configuration(settings))

    class Echo:
        def complete(self, *args, **kwargs):
            return ModelReply(content=KEY, usage=Usage(total_tokens=7))

    _, detail = start((db, tasks, job, facts), Echo())
    assert detail["error_code"] == "invalid_output" and detail["known_tokens"] == 7
    assert_secret_absent(db, detail)


def test_contacts_hidden_at_sending_boundary_and_originals_unchanged(scenario):
    db, tasks, _, _ = scenario
    docs = Documents(db)
    text = "使用 Python 编写服务；邮箱 learner@example.test，手机号13812345678。"
    doc_id = docs.import_file("contacts.txt", text.encode(), "project", "demo", uid())["id"]
    f = docs.detail(doc_id)["facts"][0]
    docs.update_fact(f["id"], f["text"], f["category"], "confirmed", f["revision"], uid())
    sent = []

    class Observer(BusinessDemoModel):
        def complete(self, messages, **kwargs):
            sent.append(json.dumps(messages, ensure_ascii=False))
            return super().complete(messages, **kwargs)

    _, detail = start(scenario, Observer())
    assert detail["status"] == "waiting_input", detail["error_code"]
    wire = "\n".join(sent)
    assert "learner@example.test" not in wire and "13812345678" not in wire
    assert "[邮箱已隐藏]" in wire and "[手机号已隐藏]" in wire
    assert docs.detail(doc_id)["facts"][0]["text"] == text
    assert (
        mask_contacts("2027.06 88c07a7c-af53-4b19-8de9-a4f1a83dc0ae")
        == "2027.06 88c07a7c-af53-4b19-8de9-a4f1a83dc0ae"
    )
