import pytest
from fastapi.testclient import TestClient
from test_stage_three import respond, start
from test_stage_three_web import post

from job_search_agent.main import create_app
from job_search_agent.services.common import uid
from job_search_agent.services.tasks import Tasks

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def test_edit_and_confirmation_roll_back_together(settings, scenario, monkeypatch):
    db, tasks, _, _ = scenario
    task_id, _ = start(scenario)
    before = respond(tasks, task_id, "skip_all")
    original = Tasks.submit
    operation = uid()
    values = {
        "revision": before["revision"],
        "draft_revision": before["draft"]["revision"],
        "content": before["draft"]["content"] + "\n虚构原子提交测试",
        "action": "confirm",
        "operation_id": operation,
    }

    def fail_after_edit(*args, **kwargs):
        raise OSError("injected storage failure")

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.get("/")
        monkeypatch.setattr(Tasks, "submit", fail_after_edit)
        failed = post(client, f"/tasks/{task_id}/draft", values)
        assert failed.status_code == 503
        assert "injected" not in failed.text
        after = tasks.detail(task_id)
        assert after["draft"] == before["draft"]
        assert after["status"] == "waiting_confirmation" and after["material"] is None
        monkeypatch.setattr(Tasks, "submit", original)
        saved = post(client, f"/tasks/{task_id}/draft", values)
        assert saved.status_code == 200
        assert tasks.detail(task_id)["status"] == "completed"
        assert post(client, f"/tasks/{task_id}/draft", values).status_code == 200
        assert tasks.detail(task_id)["draft"]["revision"] == before["draft"]["revision"] + 1
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 1


def test_completed_task_stale_action_and_status_is_readonly(settings, scenario):
    _, tasks, _, _ = scenario
    task_id, _ = start(scenario)
    respond(tasks, task_id, "skip_all")
    before = respond(tasks, task_id, "confirm_draft")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/tasks/{task_id}")
        assert "第二版" in page.text
        assert "超时" in page.text
        response = post(
            client, f"/tasks/{task_id}/respond", {"action": "skip", "revision": before["revision"]}
        )
        assert response.status_code == 409
        for _ in range(3):
            status = client.get(f"/tasks/{task_id}/status")
            assert status.status_code == 200
            assert set(status.json()) == {
                "status",
                "revision",
                "error_code",
                "model_calls",
                "tool_calls",
            }
        assert tasks.detail(task_id) == before


def test_storage_error_page_stops_auto_reload(settings, scenario):
    db, tasks, job, _ = scenario
    task_id = tasks.create(job, "mock", uid())["id"]
    with db.connect() as c:
        c.execute(
            "UPDATE agent_tasks SET status='processing',error_code='storage' WHERE id=?", (task_id,)
        )
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/tasks/{task_id}")
        assert page.status_code == 200
        assert "继续未完成任务" in page.text
        assert '<script src="/static/task.js"' not in page.text
