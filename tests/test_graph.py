import json
import os
import subprocess
import sys

import pytest
from langgraph.types import Command

from job_search_agent.agent.graph import run_smoke, smoke_graph
from job_search_agent.agent.model import DemoModel, ModelError, ModelReply, ToolCall
from job_search_agent.agent.tools import execute_demo_tool
from job_search_agent.db import Database


def test_real_graph_tool_loop_pauses_and_resumes_without_regenerating(settings, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    first = run_smoke(settings, "demo", action="start")
    assert first["status"] == "waiting_confirmation"
    assert (first["model_requests"], first["tool_requests"]) == (2, 1)
    assert run_smoke(settings, "demo", action="start") == first
    with smoke_graph(settings) as graph:
        state = graph.get_state({"configurable": {"thread_id": "demo"}}).values
        tool_message = next(m for m in state["messages"] if m["role"] == "tool")
        assert json.loads(tool_message["content"])[0]["source_id"] == "fictional-profile:1"
    result = run_smoke(settings, "demo", action="resume")
    assert result["status"] == "completed"
    assert result["model_requests"] == 2
    assert run_smoke(settings, "demo", action="resume") == result
    assert run_smoke(settings, "demo", action="start") == result
    db = Database(settings.data_dir)
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM smoke_results").fetchone()[0] == 1


def test_reject_does_not_save(settings):
    run_smoke(settings, "reject", action="start")
    result = run_smoke(settings, "reject", action="resume", approved=False)
    assert result["status"] == "cancelled"
    with Database(settings.data_dir).connect() as connection:
        assert connection.execute("SELECT count(*) FROM smoke_results").fetchone()[0] == 0


def test_confirmation_payload_must_be_boolean(settings):
    run_smoke(settings, "invalid-confirm", action="start")
    with smoke_graph(settings) as graph:
        result = graph.invoke(
            Command(resume="yes"), {"configurable": {"thread_id": "invalid-confirm"}}
        )
    assert result["status"] == "failed"


def test_model_budget_stops_and_survives_reopen(settings):
    limited = settings.model_copy(update={"max_model_calls": 1})
    result = run_smoke(limited, "budget", action="start")
    assert result["status"] == "failed" and result["error_code"] == "budget"
    assert result["model_requests"] == 1
    assert run_smoke(settings, "budget", action="resume") == result


@pytest.mark.parametrize(
    "name,args",
    [
        ("run_shell", {"query": "whoami"}),
        ("search_demo_evidence", {"query": "Python", "path": "../private"}),
        ("search_demo_evidence", {"query": " "}),
    ],
)
def test_tool_allowlist_and_parameters(name, args):
    with pytest.raises(ModelError) as error:
        execute_demo_tool(ToolCall(id="bad", name=name, arguments=args))
    assert error.value.code == "tool_rejected"


def test_graph_records_rejected_tool_attempt(settings):
    class BadModel(DemoModel):
        def complete(self, *args, **kwargs):
            return ModelReply(tool_calls=[ToolCall(id="bad", name="arbitrary_sql", arguments={})])

    with smoke_graph(settings, BadModel()) as graph:
        result = graph.invoke(
            {"task_id": "bad", "messages": [], "model_requests": 0, "tool_requests": 0},
            {"configurable": {"thread_id": "bad"}},
        )
    assert result["status"] == "failed" and result["tool_requests"] == 1


def test_resume_in_separate_processes(settings, tmp_path):
    # 子进程也显式阻断 socket；仅执行本地 SQLite 和虚构模型。
    prefix = "import pytest_socket; pytest_socket.disable_socket(); from job_search_agent.cli import main; raise SystemExit(main())"
    env = {
        **os.environ,
        "JOB_AGENT_DATA_DIR": str(settings.data_dir),
        "PYTHONUTF8": "1",
    }

    def invoke(action):
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                prefix,
                "smoke",
                action,
                "--task-id",
                "process-demo",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert output.returncode == 0, output.stderr
        return json.loads(output.stdout)

    assert invoke("start")["status"] == "waiting_confirmation"
    assert invoke("status")["status"] == "waiting_confirmation"
    assert invoke("resume")["status"] == "completed"
    assert invoke("resume")["model_requests"] == 2
