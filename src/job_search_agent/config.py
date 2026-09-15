"""只读取明确列出的配置，不向页面传整个 Settings。"""

import os
from pathlib import Path
from typing import Literal, Mapping

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class ConfigurationError(Exception):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    mode: Literal["mock", "deepseek"] = "mock"
    data_dir: Path = Field(default=Path("data"), repr=False)
    port: int = Field(default=8000, ge=1024, le=65535)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False, exclude=True)
    model: str = Field(default="deepseek-flash", pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")
    timeout_seconds: float = Field(default=60, gt=0, le=120)
    max_model_calls: int = Field(default=40, ge=1, le=100)
    max_tool_calls: int = Field(default=60, ge=1, le=100)
    max_retries: int = Field(default=1, ge=0, le=3)
    max_input_chars: int = Field(default=100000, ge=100, le=100000)
    max_output_tokens: int = Field(default=102400, ge=32, le=102400)

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.get_secret_value().strip())


ENV_FIELDS = {
    "JOB_AGENT_MODE": "mode",
    "JOB_AGENT_DATA_DIR": "data_dir",
    "JOB_AGENT_PORT": "port",
    "DEEPSEEK_API_KEY": "api_key",
    "DEEPSEEK_MODEL": "model",
    "MODEL_TIMEOUT_SECONDS": "timeout_seconds",
    "MAX_MODEL_CALLS": "max_model_calls",
    "MAX_TOOL_CALLS": "max_tool_calls",
    "MAX_RETRIES": "max_retries",
    "MAX_INPUT_CHARS": "max_input_chars",
    "MAX_OUTPUT_TOKENS": "max_output_tokens",
}


def load_settings(
    project_dir: Path | None = None, environ: Mapping[str, str] | None = None
) -> Settings:
    root = (project_dir or Path.cwd()).resolve()
    env_path = root / ".env"
    try:
        # 不插值其它环境变量，不向 os.environ 注入本地配置。
        values = (
            dotenv_values(env_path, encoding="utf-8-sig", interpolate=False)
            if env_path.is_file()
            else {}
        )
        values.update(os.environ if environ is None else environ)
        fields = {
            field: values[name]
            for name, field in ENV_FIELDS.items()
            if values.get(name) is not None
        }
        fields["data_dir"] = (root / Path(fields.get("data_dir", "data"))).resolve()
        return Settings.model_validate(fields)
    except (ValidationError, OSError, ValueError):
        # 不返回 ValidationError 原文，它可能包含被误填到别的字段里的秘密。
        raise ConfigurationError("配置无效，请按 .env.example 检查字段、类型和范围。") from None
