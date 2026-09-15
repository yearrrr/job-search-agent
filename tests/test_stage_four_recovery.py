import os
import subprocess
import sys

import pytest
from test_stage_three import respond, start

from job_search_agent.agent.workflow import review_token
from job_search_agent.services.common import uid
from job_search_agent.services.documents import Documents
from job_search_agent.services.profile import Profile
from job_search_agent.services.tasks import task_lock

CHILD = """
import os, sys, pytest_socket
from pathlib import Path
pytest_socket.disable_socket()
from job_search_agent.config import Settings
from job_search_agent.db import Database
from job_search_agent.services.tasks import Tasks
from job_search_agent.services.drafts import Drafts
from job_search_agent.agent.business_model import BusinessDemoModel
from job_search_agent.agent.model import ModelError
from job_search_agent.agent import workflow
db = Database(Path(sys.argv[1]))
db.initialize()
tasks = Tasks(db, Settings(data_dir=db.data_dir, max_retries=0))
task_id, mode = sys.argv[2:4]
model = BusinessDemoModel()
if mode == "started":
    class ExitDuringRequest:
        def complete(self, *args, **kwargs):
            os._exit(79)
    model = ExitDuringRequest()
elif mode == "recorded":
    original = Tasks.finish_attempt
    def exit_after_result(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(79)
    Tasks.finish_attempt = exit_after_result
elif mode == "retry":
    class Limited:
        def complete(self, *args, **kwargs):
            raise ModelError("rate_limit")
    model = Limited()
    original = Tasks.wait_retry
    def exit_before_retry(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(79)
    Tasks.wait_retry = exit_before_retry
elif mode == "material":
    original = Drafts.confirm
    def exit_after_material(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(79)
    Drafts.confirm = exit_after_material
elif mode == "fact":
    original = workflow.commit_answer
    def exit_after_fact(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(79)
    workflow.commit_answer = exit_after_fact
tasks.run(task_id, model)
"""


def child(db, task_id, mode, expected=0):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", CHILD, str(db.data_dir), task_id, mode],
        cwd=db.data_dir.parent,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    "point,final_status,models",
    [
        ("started", "failed", 1),
        ("recorded", "waiting_input", 2),
        ("retry", "waiting_input", 3),
    ],
)
def test_hard_process_exit_at_model_boundaries(scenario, point, final_status, models):
    db, tasks, job, _ = scenario
    task_id = tasks.create(job, "mock", uid())["id"]
    child(db, task_id, point, 79)
    interrupted = tasks.detail(task_id)
    assert interrupted["status"] == "processing" and interrupted["model_calls"] == 1
    assert (
        interrupted["attempts"][0]["status"]
        == {
            "started": "started",
            "recorded": "ok",
            "retry": "failed",
        }[point]
    )
    child(db, task_id, "resume")
    after = tasks.detail(task_id)
    assert after["status"] == final_status, after["error_code"]
    assert after["model_calls"] == models
    if point == "started":
        assert after["error_code"] == "uncertain_attempt" and after["tokens"] is None
    else:
        assert after["tool_calls"] == 5
    child(db, task_id, "resume")
    assert tasks.detail(task_id)["model_calls"] == models


def test_separate_worker_cannot_run_while_os_lock_held(scenario):
    db, tasks, job, _ = scenario
    task_id = tasks.create(job, "mock", uid())["id"]
    with task_lock(db.data_dir, task_id) as acquired:
        assert acquired
        child(db, task_id, "resume")
        assert tasks.detail(task_id)["status"] == "queued"
        assert tasks.detail(task_id)["model_calls"] == 0
    child(db, task_id, "resume")
    assert tasks.detail(task_id)["status"] == "waiting_input"
    assert (db.data_dir / "task-locks" / f"{task_id}.lock").stat().st_size == 1


def test_hard_exit_after_material_commit_reconciles_same_version(scenario):
    db, tasks, _, _ = scenario
    task_id, _ = start(scenario)
    detail = respond(tasks, task_id, "skip_all")
    tasks.submit(
        task_id, detail["revision"], "confirm_draft", "", "", detail["draft"]["revision"], uid()
    )
    child(db, task_id, "material", 79)
    before = tasks.detail(task_id)
    assert before["status"] == "processing" and before["material"]
    child(db, task_id, "resume")
    after = tasks.detail(task_id)
    assert after["status"] == "completed", after["error_code"]
    assert after["material"] == before["material"]
    assert after["model_calls"] == before["model_calls"]
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 1


def test_hard_exit_after_fact_commit_does_not_confirm_twice(scenario):
    db, tasks, _, _ = scenario
    task_id, _ = start(scenario)
    detail = respond(
        tasks, task_id, "answer", text="使用 Docker 部署虚构课程项目。", category="project"
    )
    document = Documents(db).detail(detail["view"]["wait"]["document_id"])
    tasks.submit(
        task_id, detail["revision"], "confirm_fact", "", "", 0, uid(), review_token(document)
    )
    child(db, task_id, "fact", 79)
    assert len(Profile(db).confirmed("demo")) == 3
    child(db, task_id, "resume")
    after = tasks.detail(task_id)
    assert after["status"] == "waiting_confirmation", after["error_code"]
    assert after["view"]["wait"]["kind"] == "draft"
    assert len(Profile(db).confirmed("demo")) == 3
    with db.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM fact_history WHERE fact_id=? AND status='confirmed'",
                (document["facts"][0]["id"],),
            ).fetchone()[0]
            == 1
        )
