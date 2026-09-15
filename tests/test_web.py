import socket

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pytest_socket import SocketConnectBlockedError

from job_search_agent.main import create_app


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_home_health_and_static_do_not_expose_configuration(settings):
    # Windows asyncio 内部 socketpair 使用回环连接；仍禁止一切外部地址。
    with socket.socket() as guard_check:
        with (
            pytest.warns(UserWarning, match="A test tried"),
            pytest.raises(SocketConnectBlockedError),
        ):
            guard_check.connect(("203.0.113.1", 443))
    key = "fictional-test-key-DO-NOT-USE"
    configured = settings.model_copy(update={"api_key": SecretStr(key)})
    with TestClient(create_app(configured), base_url="http://127.0.0.1") as client:
        home = client.get("/")
        health = client.get("/health")
        css = client.get("/static/style.css")
        assert home.status_code == health.status_code == css.status_code == 200
        assert "加载虚构测试样例" in home.text
        assert "离线模拟" in home.text
        assert health.json()["schema_version"] == 7
        for response in [home, health, css]:
            assert key not in response.text
            assert str(settings.data_dir) not in response.text
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["Referrer-Policy"] == "same-origin"
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/").status_code == 405
