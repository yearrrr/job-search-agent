"""显式启用后，只发送固定的非敏感短文本；一次探测最多一次尝试。"""

import time
from uuid import uuid4

from .agent.model import DeepSeekModel, ModelError
from .config import Settings
from .db import Database

PROBE_MESSAGES = [
    {
        "role": "user",
        "content": "Reply with OK. This is a non-sensitive connectivity check.",
    }
]


def check_api(
    settings: Settings, model: DeepSeekModel | None = None, probe_id: str | None = None
) -> dict:
    model = model or DeepSeekModel(settings)
    model.check_ready()  # 模式/Key 不合格时不产生虚假的请求记录。
    database = Database(settings.data_dir)
    database.initialize()
    probe_id = probe_id or str(uuid4())
    if not database.begin_probe(probe_id):
        return database.get_probe(probe_id)
    started = time.perf_counter()
    try:
        reply = model.complete(PROBE_MESSAGES, max_tokens=32)
        if reply.tool_calls:
            raise ModelError("invalid_output")
    except ModelError as error:
        database.finish_probe(
            probe_id,
            error_code=error.code,
            elapsed_seconds=time.perf_counter() - started,
        )
    else:
        database.finish_probe(
            probe_id,
            error_code=None,
            elapsed_seconds=time.perf_counter() - started,
            **reply.usage.model_dump(),
        )
    # 不回显模型原文、HTTP 错误正文、密钥、请求消息或完整配置。
    return database.get_probe(probe_id)
