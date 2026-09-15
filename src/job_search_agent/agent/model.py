"""一个 complete 对应一次尝试；无隐式重试、无第三方日志或备用服务商。"""

import json
import time
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import Settings
from .wire import media_summary

ERROR_MESSAGES = {
    "network_disabled": "当前为离线模式，未发送模型请求。",
    "missing_key": "请在本机配置 DEEPSEEK_API_KEY，不要发送到聊天中。",
    "invalid_request": "请求参数或长度不符合本地限制。",
    "timeout": "模型请求超时，本次未自动重试。",
    "network": "无法连接模型服务，本次未自动重试。",
    "authentication": "模型服务鉴权失败，请检查本地密钥。",
    "balance": "模型服务提示余额不足。",
    "rate_limit": "模型服务限流，请查看尝试记录并稍后再试。",
    "unavailable": "模型服务暂不可用。",
    "invalid_output": "模型输出不完整或结构不符合要求。",
    "http_error": "模型服务返回未预期的 HTTP 状态。",
    "budget": "已达到本任务的调用上限。",
    "tool_rejected": "工具名称或参数不在允许范围内。",
}


class ModelError(Exception):
    def __init__(self, code: str, *, usage=None):
        self.code = code
        self.usage = usage
        super().__init__(ERROR_MESSAGES[code])


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ModelReply(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    elapsed_seconds: float = 0


class Model(Protocol):
    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ModelReply: ...


class DeepSeekModel:
    ENDPOINT = "https://api.deepseek.com/chat/completions"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def check_ready(self) -> None:
        if self.settings.mode != "deepseek":
            raise ModelError("network_disabled")
        if not self.settings.key_configured:
            raise ModelError("missing_key")

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ModelReply:
        self.check_ready()
        output_limit = self.settings.max_output_tokens if max_tokens is None else max_tokens
        if (
            type(output_limit) is not int
            or not 1 <= output_limit <= self.settings.max_output_tokens
        ):
            raise ModelError("invalid_request")
        body = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,
            "max_tokens": output_limit,
            "thinking": {"type": "disabled"},
        }
        if tools:
            body["tools"] = tools
        try:
            if (
                len(json.dumps(media_summary(body), ensure_ascii=False))
                > self.settings.max_input_chars
                or len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 48 * 1024 * 1024
            ):
                raise ModelError("invalid_request")
        except (ValueError, TypeError):
            raise ModelError("invalid_request") from None

        started = time.perf_counter()
        owned = self._client is None
        # 禁用环境代理、重定向与连接层重试，密钥只发往固定官方端点。
        client = self._client or httpx.Client(trust_env=False, follow_redirects=False)
        try:
            with client.stream(
                "POST",
                self.ENDPOINT,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key.get_secret_value().strip()}"
                },
                timeout=self.settings.timeout_seconds,
                follow_redirects=False,
            ) as response:
                status_code = response.status_code
                raw = bytearray()
                if status_code == 200:
                    # 在读取过程中限制解压后的字节，避免先把巨大响应放进内存再检查。
                    for chunk in response.iter_bytes():
                        if time.perf_counter() - started > self.settings.timeout_seconds:
                            raise ModelError("timeout")
                        if len(raw) + len(chunk) > max(262144, output_limit * 24):
                            raise ModelError("invalid_output")
                        raw.extend(chunk)
        except httpx.TimeoutException:
            raise ModelError("timeout") from None
        except httpx.HTTPError:
            raise ModelError("network") from None
        finally:
            if owned:
                client.close()
        codes = {
            400: "invalid_request",
            401: "authentication",
            402: "balance",
            422: "invalid_request",
            429: "rate_limit",
            500: "unavailable",
            503: "unavailable",
        }
        if status_code != 200:
            raise ModelError(codes.get(status_code, "http_error"))
        usage = None
        try:
            payload = json.loads(raw)
            usage = Usage.model_validate(payload.get("usage") or {})
            # 不将服务商意外回显的密钥放入结果、业务表或检查点。
            if self.settings.api_key.get_secret_value().strip() in raw.decode("utf-8"):
                raise ValueError("secret echo")
            choice = payload["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ValueError("incomplete")
            calls = []
            for item in message.get("tool_calls") or []:
                if item.get("type") != "function":
                    raise ValueError("tool type")
                calls.append(
                    ToolCall(
                        id=item["id"],
                        name=item["function"]["name"],
                        arguments=json.loads(item["function"]["arguments"]),
                    )
                )
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("content type")
            if not calls and (not content or not content.strip()):
                raise ValueError("empty output")
            if (choice["finish_reason"] == "tool_calls") != bool(calls):
                raise ValueError("inconsistent finish reason")
            if len({call.id for call in calls}) != len(calls):
                raise ValueError("duplicate tool ids")
            # 用量缺失保留 None；异常用量也不能伪装为 0。
            return ModelReply(
                content=content,
                tool_calls=calls,
                usage=usage,
                elapsed_seconds=time.perf_counter() - started,
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            AttributeError,
            ValidationError,
            RecursionError,
        ):
            raise ModelError("invalid_output", usage=usage) from None


class DemoModel:
    """仅供工程演示的确定性模拟模型，绝不读取个人资料或请求网络。"""

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> ModelReply:
        if messages[-1]["role"] != "tool":
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id="demo-search-1",
                        name="search_demo_evidence",
                        arguments={"query": "Python"},
                    )
                ]
            )
        return ModelReply(
            content="【虚构演示】示例同学使用 Python 编写过课程练习。来源：fictional-profile:1。"
        )
