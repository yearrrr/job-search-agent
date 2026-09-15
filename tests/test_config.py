import pytest

from job_search_agent.config import ConfigurationError, load_settings

FAKE_KEY = "fictional-test-key-DO-NOT-USE"


def test_defaults_and_no_global_env_mutation(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        f"DEEPSEEK_API_KEY={FAKE_KEY}\nJOB_AGENT_PORT=8100\n", encoding="utf-8"
    )
    settings = load_settings(tmp_path, environ={"JOB_AGENT_PORT": "8200"})
    assert settings.mode == "mock"
    assert settings.port == 8200
    assert settings.data_dir == tmp_path / "data"
    assert settings.key_configured
    assert FAKE_KEY not in repr(settings)
    assert "api_key" not in settings.model_dump()
    import os

    assert "DEEPSEEK_API_KEY" not in os.environ


@pytest.mark.parametrize(
    "field,value",
    [
        ("JOB_AGENT_MODE", FAKE_KEY),
        ("JOB_AGENT_PORT", "0"),
        ("MAX_MODEL_CALLS", "0"),
        ("MAX_RETRIES", "100"),
        ("MODEL_TIMEOUT_SECONDS", "nan"),
        ("DEEPSEEK_MODEL", f"https://{FAKE_KEY}"),
    ],
)
def test_invalid_config_never_echoes_values(tmp_path, field, value):
    with pytest.raises(ConfigurationError) as error:
        load_settings(tmp_path, environ={field: value})
    assert FAKE_KEY not in str(error.value)
    assert "配置无效" in str(error.value)


def test_dotenv_does_not_expand_other_secrets(tmp_path):
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=${OTHER_SECRET}\n", encoding="utf-8")
    settings = load_settings(tmp_path, environ={"OTHER_SECRET": FAKE_KEY})
    assert settings.api_key.get_secret_value() == "${OTHER_SECRET}"
