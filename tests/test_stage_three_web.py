import pytest
from fastapi.testclient import TestClient

from job_search_agent.main import create_app
from job_search_agent.services.common import uid
from job_search_agent.services.profile import Profile

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1"])


def post(client, url, data, **kwargs):
    return client.post(
        url,
        data={"csrf": client.cookies["job_csrf"], "corpus": "demo", "operation_id": uid(), **data},
        **kwargs,
    )


def test_web_analysis_edit_confirm_export_and_csrf(settings, scenario):
    db, tasks, job_id, _ = scenario
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        page = client.get(f"/jobs/{job_id}")
        assert "开始分析岗位" in page.text
        started = post(client, f"/jobs/{job_id}/tasks", {"engine": "mock"})
        assert started.status_code == 200
        assert "等待补充" in started.text
        assert "明确不符" in started.text and "有证据支持" in started.text
        task_id = started.url.path.split("/")[-1]
        first = tasks.detail(task_id)
        for _ in range(2):
            assert client.get(f"/tasks/{task_id}").status_code == 200
            assert client.get(f"/tasks/{task_id}/status").status_code == 200
        assert tasks.detail(task_id)["model_calls"] == first["model_calls"]
        assert (
            post(
                client,
                f"/tasks/{task_id}/respond",
                {"revision": first["revision"], "action": "skip_all"},
                headers={"Origin": "https://attacker.example"},
            ).status_code
            == 400
        )
        skipped = post(
            client,
            f"/tasks/{task_id}/respond",
            {"revision": first["revision"], "action": "skip_all"},
            headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
        )
        assert skipped.status_code == 200 and "确认当前内容并保存版本" in skipped.text
        task = tasks.detail(task_id)
        edited_text = task["draft"]["content"] + "\n人工备注：<script>alert('test')</script>"
        values = {
            "revision": task["revision"],
            "draft_revision": task["draft"]["revision"],
            "content": edited_text,
            "action": "confirm",
            "operation_id": uid(),
        }
        confirmed = post(client, f"/tasks/{task_id}/draft", values)
        assert confirmed.status_code == 200 and "已保存材料" in confirmed.text
        assert post(client, f"/tasks/{task_id}/draft", values).status_code == 200
        material = tasks.detail(task_id)["material"]
        saved = client.get(f"/materials/{material['id']}")
        assert saved.status_code == 200 and "&lt;script&gt;" in saved.text
        assert "<script>alert" not in saved.text
        for extension in ("md", "txt"):
            exported = client.get(f"/materials/{material['id']}/export/{extension}")
            assert exported.status_code == 200 and exported.text == edited_text
            assert exported.headers["content-disposition"].startswith("attachment;")
        assert client.get(f"/materials/{material['id']}/export/pdf").status_code == 400
        assert "材料版本 1" in client.get(f"/jobs/{job_id}").text
        assert len(Profile(db).confirmed("demo")) == 2


def test_web_draft_stale_version_and_readonly_get(settings, scenario):
    _, tasks, job, _ = scenario
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.get("/")
        created = post(client, f"/jobs/{job}/tasks", {"engine": "mock"})
        task_id = created.url.path.split("/")[-1]
        task = tasks.detail(task_id)
        post(
            client,
            f"/tasks/{task_id}/respond",
            {"revision": task["revision"], "action": "skip_all"},
        )
        task = tasks.detail(task_id)
        values = {
            "revision": task["revision"],
            "draft_revision": task["draft"]["revision"],
            "content": task["draft"]["content"] + "\n编辑说明",
            "action": "edit",
        }
        assert post(client, f"/tasks/{task_id}/draft", values).status_code == 200
        assert (
            post(client, f"/tasks/{task_id}/draft", {**values, "action": "confirm"}).status_code
            == 409
        )
        assert tasks.detail(task_id)["material"] is None
        assert client.get(f"/tasks/{task_id}/respond").status_code == 405
        assert client.get("/static/task.js").status_code == 200


def test_start_needs_valid_mode_key_and_real_confirmed_snapshot(settings, scenario):
    _, tasks, job, _ = scenario
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.get("/")
        assert post(client, f"/jobs/{job}/tasks", {"engine": "deepseek"}).status_code == 400
        assert post(client, f"/jobs/{job}/tasks", {"engine": "shell"}).status_code == 400
        assert tasks.list_for_job(job) == []
