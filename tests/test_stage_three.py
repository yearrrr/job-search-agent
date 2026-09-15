import json
import os
import subprocess
import sys

import pytest

from job_search_agent.agent.business_model import BusinessDemoModel
from job_search_agent.agent.contracts import validate_draft
from job_search_agent.agent.model import ModelError, ModelReply, ToolCall
from job_search_agent.agent.retrieval import execute_search
from job_search_agent.agent.workflow import review_token
from job_search_agent.schemas import Conflict
from job_search_agent.services.common import uid
from job_search_agent.services.documents import Documents
from job_search_agent.services.drafts import Drafts
from job_search_agent.services.jobs import Jobs
from job_search_agent.services.profile import Profile
from job_search_agent.services.tasks import Tasks, make_snapshot, task_lock


def start(scenario, model=None):
    db, tasks, job, facts = scenario
    task_id = tasks.create(job, "mock", uid())["id"]
    tasks.run(task_id, model)
    return task_id, tasks.detail(task_id)


def respond(tasks, task_id, action, *, text="", category="", model=None, op=None):
    detail = tasks.detail(task_id)
    token = ""
    if (detail["view"].get("wait") or {}).get("kind") == "fact":
        token = review_token(Documents(tasks.db).detail(detail["view"]["wait"]["document_id"]))
    tasks.submit(
        task_id,
        detail["revision"],
        action,
        text,
        category,
        detail["draft"]["revision"] if detail["draft"] else 0,
        op or uid(),
        token,
    )
    if action != "later":
        tasks.run(task_id, model)
    return tasks.detail(task_id)


def test_actual_graph_three_outcomes_retrieval_and_saved_version(scenario, monkeypatch):
    from job_search_agent.agent import workflow

    count = []
    original = workflow.execute_search

    def spy(*args):
        count.append(args[2])
        return original(*args)

    monkeypatch.setattr(workflow, "execute_search", spy)
    db, tasks, job, facts = scenario
    task_id, first = start(scenario)
    assert first["status"] == "waiting_input", first["error_code"]
    assert {i["status"] for i in first["view"]["items"]} == {"supported", "mismatch", "unknown"}
    assert count == ["search_confirmed_facts"] * 5
    evidence = first["view"]["evidence"]
    assert len(evidence) == 2 and facts[2]["id"] not in evidence
    last_model = [a for a in first["attempts"] if a["kind"] == "model"][-1]
    assert any(
        m["role"] == "tool" and json.loads(m["content"])["facts"]
        for m in last_model["request"]["messages"]
    )
    old_status = Jobs(db).detail(job)["status"]
    paused = respond(tasks, task_id, "later")
    assert paused["deferred"] and paused["model_calls"] == first["model_calls"]
    drafted = respond(tasks, task_id, "skip_all")
    assert drafted["status"] == "waiting_confirmation", drafted["error_code"]
    assert drafted["view"]["wait"]["kind"] == "draft"
    assert "使用 Docker 部署虚构项目" not in drafted["draft"]["content"]
    assert "使用 Python 编写 API 和测试。" in drafted["draft"]["content"]
    assert "待核实：使用过 Docker" in drafted["draft"]["content"]
    edited = Drafts(db).edit(
        task_id, 1, drafted["draft"]["content"] + "\n人工编辑：表达核对完成。", uid()
    )
    assert edited["revision"] == 2
    done = respond(tasks, task_id, "confirm_draft")
    assert done["status"] == "completed", done["error_code"]
    assert done["material"]["version"] == 1
    before_counts = (done["model_calls"], done["tool_calls"])
    again = respond(tasks, task_id, "confirm_draft")
    assert (again["model_calls"], again["tool_calls"]) == before_counts
    assert Jobs(db).detail(job)["status"] == old_status
    assert len(Profile(db).confirmed("demo")) == 2
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 1
        assert (
            c.execute("SELECT count(*) FROM draft_edits WHERE task_id=?", (task_id,)).fetchone()[0]
            == 2
        )
    assert Drafts(db).detail(done["material"]["id"])["metadata"]["user_edited"]


def test_answer_requires_confirmation_then_reanalysis(scenario):
    db, tasks, job, facts = scenario
    task_id, first = start(scenario)
    answer = "使用 Docker 为虚构课程项目构建镜像。"
    review = respond(tasks, task_id, "answer", text=answer, category="project")
    assert review["status"] == "waiting_confirmation", review["error_code"]
    assert review["view"]["wait"]["kind"] == "fact"
    assert len(Profile(db).confirmed("demo")) == 2
    doc = Documents(db).detail(review["view"]["wait"]["document_id"])
    assert all(f["status"] == "pending" for f in doc["facts"])
    ready = respond(tasks, task_id, "confirm_fact")
    assert ready["status"] == "waiting_confirmation", ready["error_code"]
    assert ready["view"]["wait"]["kind"] == "draft"
    assert ready["view"]["round"] == 1
    assert len(Profile(db).confirmed("demo")) == 3
    assert answer in ready["draft"]["content"]
    assert ready["model_calls"] == 5 and ready["tool_calls"] == 10


def test_rejected_answer_and_skip_do_not_reask(scenario):
    db, tasks, *_ = scenario
    task_id, _ = start(scenario)
    review = respond(
        tasks, task_id, "answer", text="使用 Docker 为虚构项目部署。", category="project"
    )
    doc_id = review["view"]["wait"]["document_id"]
    ready = respond(tasks, task_id, "reject_fact")
    assert ready["view"]["wait"]["kind"] == "draft"
    assert ready["view"]["round"] == 0
    assert all(f["status"] == "rejected" for f in Documents(db).detail(doc_id)["facts"])
    assert len(Profile(db).confirmed("demo")) == 2


def test_empty_retrieval_stays_unknown(scenario):
    db, tasks, job, facts = scenario
    docs = Documents(db)
    for f in Profile(db).confirmed("demo"):
        docs.update_fact(f["id"], f["text"], f["category"], "pending", f["revision"], uid())
    task_id, first = start(scenario)
    assert first["status"] == "waiting_input", first["error_code"]
    assert not first["view"]["evidence"]
    assert all(i["status"] == "unknown" for i in first["view"]["items"])
    ready = respond(tasks, task_id, "skip_all")
    assert ready["status"] == "waiting_confirmation"
    assert "当前没有可用于此部分" in ready["draft"]["content"]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("arbitrary_sql", {"query": "select * from facts"}),
        ("search_confirmed_facts", {"query": "Python", "category": "all", "limit": 9}),
        (
            "search_confirmed_facts",
            {"query": "Python", "category": "all", "limit": 2, "corpus": "personal"},
        ),
        ("search_confirmed_facts", {"query": "Python", "category": "all", "limit": True}),
    ],
)
def test_tools_are_scoped_and_bounded(scenario, name, arguments):
    db, _, job, _ = scenario
    with pytest.raises(ModelError):
        execute_search(db, make_snapshot(db, job), name, arguments)


def test_keyword_retrieval_and_stale_fact_exclusion(scenario):
    db, _, job, _ = scenario
    snapshot = make_snapshot(db, job)
    result = execute_search(
        db,
        snapshot,
        "search_confirmed_facts",
        {"query": "Python", "category": "project", "limit": 3},
    )
    assert len(result["facts"]) == 1
    fact = result["facts"][0]
    Documents(db).update_fact(
        fact["id"], fact["text"], fact["category"], "pending", fact["revision"], uid()
    )
    assert not execute_search(
        db, snapshot, "search_confirmed_facts", {"query": "Python", "category": "all", "limit": 3}
    )["facts"]


@pytest.mark.parametrize(
    "change",
    ["fake_reference", "pending_reference", "missing_requirement", "new_number", "new_experience"],
)
def test_reject_invalid_model_claims(scenario, change):
    pending_id = scenario[3][2]["id"]

    class BadModel(BusinessDemoModel):
        def complete(self, messages, **kwargs):
            reply = super().complete(messages, **kwargs)
            if reply.tool_calls:
                return reply
            data = json.loads(reply.content)
            if "items" in data and change in ("fake_reference", "pending_reference"):
                data["items"][0]["fact_ids"] = [
                    "nonexistent" if change == "fake_reference" else pending_id
                ]
            elif "items" in data and change == "missing_requirement":
                data["items"].pop()
            elif "sections" in data and change == "new_number":
                data["sections"][0]["rationale"] = "提升效率 999%。"
            elif "sections" in data and change == "new_experience":
                data["sections"][0]["text"] = "拥有世界知名企业实习经历，精通 Kubernetes。"
            return ModelReply(content=json.dumps(data, ensure_ascii=False))

    model = BadModel()
    task_id, result = start(scenario, model)
    if result["status"] == "waiting_input":
        result = respond(scenario[1], task_id, "skip_all", model=model)
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_output"
    assert result["material"] is None


def test_budget_and_unknown_tool_attempt_persist(scenario, settings):
    db, _, job, facts = scenario
    limited = Tasks(db, settings.model_copy(update={"max_tool_calls": 1}))
    task_id = limited.create(job, "mock", uid())["id"]
    limited.run(task_id)
    result = Tasks(db, settings).detail(task_id)
    assert result["status"] == "failed" and result["error_code"] == "budget"
    assert result["tool_calls"] == 1
    Tasks(db, settings).run(task_id)
    assert Tasks(db, settings).detail(task_id)["tool_calls"] == 1

    class BadTool(BusinessDemoModel):
        def complete(self, *args, **kwargs):
            return ModelReply(tool_calls=[ToolCall(id="bad", name="run_shell", arguments={})])

    task_id, result = start(scenario, BadTool())
    assert result["error_code"] == "tool_rejected" and result["tool_calls"] == 1
    assert result["attempts"][-1]["status"] == "failed"


def test_stale_evidence_blocks_confirmation(scenario):
    db, tasks, *_ = scenario
    task_id, _ = start(scenario)
    respond(tasks, task_id, "skip_all")
    f = Profile(db).confirmed("demo")[0]
    Documents(db).update_fact(f["id"], f["text"], f["category"], "pending", f["revision"], uid())
    result = respond(tasks, task_id, "confirm_draft")
    assert result["error_code"] == "stale_evidence" and not result["material"]


def test_operation_replay_and_concurrent_lock(scenario):
    db, tasks, job, _ = scenario
    op = uid()
    first = tasks.create(job, "mock", op)
    assert tasks.create(job, "mock", op) == first
    task_id = first["id"]
    with task_lock(db.data_dir, task_id) as acquired:
        assert acquired
        tasks.run(task_id)
        assert tasks.detail(task_id)["model_calls"] == 0
    tasks.run(task_id)
    detail = tasks.detail(task_id)
    command_id = uid()
    args = (task_id, detail["revision"], "skip_all", "", "", 0, command_id)
    tasks.submit(*args)
    tasks.run(task_id)
    tasks.submit(*args)
    tasks.run(task_id)
    assert tasks.detail(task_id)["view"]["wait"]["kind"] == "draft"
    with pytest.raises(Conflict):
        tasks.submit(task_id, detail["revision"], "skip", "", "", 0, uid())


def test_waits_resume_in_new_processes(scenario, settings, tmp_path):
    db, tasks, job, _ = scenario
    task_id, first = start(scenario)
    code = """
import json, sys, pytest_socket
from pathlib import Path
pytest_socket.disable_socket()
from job_search_agent.config import Settings
from job_search_agent.db import Database
from job_search_agent.services.tasks import Tasks
from job_search_agent.services.common import uid
db=Database(Path(sys.argv[1])); db.initialize()
tasks=Tasks(db,Settings(data_dir=db.data_dir))
t=tasks.detail(sys.argv[2]); action=sys.argv[3]
tasks.submit(t['id'],t['revision'],action,'','',t['draft']['revision'] if t['draft'] else 0,uid())
tasks.run(t['id'])
t=tasks.detail(t['id'])
print(json.dumps({k:t[k] for k in ['status','error_code','model_calls','tool_calls']}))
"""

    def invoke(action):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code, str(db.data_dir), task_id, action],
            cwd=tmp_path,
            env={**os.environ, "PYTHONUTF8": "1"},
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    assert first["status"] == "waiting_input"
    assert invoke("skip_all")["status"] == "waiting_confirmation"
    done = invoke("confirm_draft")
    assert done["status"] == "completed"
    assert invoke("confirm_draft") == done


def test_requirement_reference_is_not_a_fabricated_metric():
    evidence = {"fact": {"text": "使用 Python 构建接口。"}}
    content = {
        "sections": [
            {"kind": kind, "fact_ids": ["fact"], "rationale": "优先展示与 r1 对应的项目。"}
            for kind in ("简历段落", "项目介绍", "自我介绍")
        ]
    }
    assert validate_draft(json.dumps(content), evidence, {"r1"})
    content["sections"][0]["rationale"] = "满足 r99，效率提升 999%。"
    with pytest.raises(ModelError):
        validate_draft(json.dumps(content), evidence, {"r1"})


def test_material_commit_before_checkpoint_failure_reconciles_once(scenario, monkeypatch):
    db, tasks, *_ = scenario
    task_id, _ = start(scenario)
    respond(tasks, task_id, "skip_all")
    original = Drafts.confirm

    def crash_after_save(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("injected after material commit")

    monkeypatch.setattr(Drafts, "confirm", crash_after_save)
    interrupted = respond(tasks, task_id, "confirm_draft")
    assert interrupted["status"] == "processing" and interrupted["error_code"] == "storage"
    assert interrupted["material"]
    monkeypatch.setattr(Drafts, "confirm", original)
    tasks.run(task_id)
    resumed = tasks.detail(task_id)
    assert resumed["status"] == "completed", resumed["error_code"]
    assert resumed["material"] == interrupted["material"]
    assert resumed["model_calls"] == interrupted["model_calls"]
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 1


def test_answer_confirmation_commit_before_checkpoint_failure_replays(scenario, monkeypatch):
    from job_search_agent.agent import workflow

    db, tasks, *_ = scenario
    task_id, _ = start(scenario)
    respond(tasks, task_id, "answer", text="使用 Docker 部署虚构课程项目。", category="project")
    original = workflow.commit_answer

    def crash_after_fact(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected after confirmed fact")

    monkeypatch.setattr(workflow, "commit_answer", crash_after_fact)
    interrupted = respond(tasks, task_id, "confirm_fact")
    assert interrupted["status"] == "processing"
    assert len(Profile(db).confirmed("demo")) == 3
    monkeypatch.setattr(workflow, "commit_answer", original)
    tasks.run(task_id)
    resumed = tasks.detail(task_id)
    assert resumed["status"] == "waiting_confirmation", resumed["error_code"]
    assert resumed["view"]["wait"]["kind"] == "draft"
    assert len(Profile(db).confirmed("demo")) == 3


def test_started_remote_attempt_is_not_repeated(scenario):
    from job_search_agent.services.tasks import TaskStopped

    db, tasks, job, _ = scenario
    task_id = tasks.create(job, "mock", uid())["id"]
    with db.connect() as c:
        c.execute(
            """INSERT INTO agent_attempts(task_id,step_key,kind,status,request_json,created_at)
                     VALUES (?, 'model:0', 'model', 'started', '{}', '2026-09-14')""",
            (task_id,),
        )

    def forbidden():
        pytest.fail("未落盘请求不能自动重发")

    with pytest.raises(TaskStopped, match="上次请求"):
        tasks.attempt(task_id, "model:0", "model", {}, forbidden)
    assert tasks.detail(task_id)["model_calls"] == 1


def test_key_is_absent_from_business_database_and_checkpoints(scenario, settings):
    from pydantic import SecretStr

    db, _, job, _ = scenario
    fake = "fictional-stage-three-secret-do-not-use"
    tasks = Tasks(db, settings.model_copy(update={"api_key": SecretStr(fake)}))
    task_id = tasks.create(job, "mock", uid())["id"]
    tasks.run(task_id)
    respond(tasks, task_id, "skip_all")
    assert fake not in json.dumps(tasks.detail(task_id))
    for filename in ("app.sqlite3", "workflow.sqlite3"):
        assert fake.encode() not in (db.data_dir / filename).read_bytes()
