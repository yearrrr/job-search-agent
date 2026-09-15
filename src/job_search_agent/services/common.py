import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from ..schemas import Conflict, InputError


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def get_row(connection, table, row_id):
    # 表名只能来自服务内部常量，不暴露给 HTTP 或模型。
    if table not in {"documents", "facts", "jobs", "todos"}:
        raise InputError("不支持的数据类型。")
    row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise InputError("记录不存在，请返回列表刷新。")
    return dict(row)


def once(connection, operation_id, payload, action):
    """同一事务内检查操作键、执行业务、记录结果。冲突直接回滚。"""
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 100:
        raise InputError("操作标识无效，请刷新页面。")
    fingerprint = digest(payload)
    previous = connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise Conflict("该操作已用于不同内容，请刷新页面后重试。")
        return json.loads(previous["result_json"])
    result = action()
    connection.execute(
        "INSERT INTO operations VALUES (?,?,?,?)",
        (operation_id, fingerprint, json.dumps(result, ensure_ascii=False), now()),
    )
    return result
