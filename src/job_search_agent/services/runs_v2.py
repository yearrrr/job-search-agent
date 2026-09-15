"""第二版有界任务账本；每次外部调用先登记，恢复只复用已落盘的确定结果。"""

import json
import sqlite3
import time

from ..agent.model import ERROR_MESSAGES, DeepSeekModel, ModelError, ModelReply
from ..agent.privacy import guard_secret
from ..agent.wire import media_summary
from ..schemas import InputError
from .common import digest, now, once, uid
from .tasks import TASK_ERRORS, TaskStopped, task_lock

RUN_ERRORS = {
    **ERROR_MESSAGES,
    **TASK_ERRORS,
    "source_file": "原始岗位文件无法读取，请检查本地文件后重新导入。",
    "repeated_search": "模型重复请求相同资料，已停止无效检索。已有调用和结果已保留。",
}
LIMIT_KEYS = (
    "model",
    "max_model_calls",
    "max_tool_calls",
    "max_retries",
    "max_input_chars",
    "max_output_tokens",
    "timeout_seconds",
)


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Runs:
    def __init__(self, database, settings, model_factory=DeepSeekModel):
        self.db, self.settings, self.model_factory = database, settings, model_factory

    def create(self, kind, corpus, target, snapshot, mode, operation_id):
        if kind not in ("jd", "preparation", "recommendation", "resume") or mode not in (
            "mock",
            "deepseek",
        ):
            raise InputError("任务类型或运行方式无效。")
        if kind == "jd" and mode != "deepseek":
            raise InputError("岗位解析需要模型；也可选择手动填写。")
        if mode == "deepseek" and not self.settings.key_configured:
            raise InputError("请先在本机配置模型密钥，或选择手动填写 / 离线演示。")
        guard_secret(snapshot, self.settings, "invalid_request")
        fingerprint = digest([kind, corpus, target, snapshot, mode])
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")

            def save():
                old = c.execute(
                    """SELECT id FROM v2_runs WHERE fingerprint=?
                    AND status IN ('queued','processing','completed') ORDER BY created_at DESC LIMIT 1""",
                    (fingerprint,),
                ).fetchone()
                if old:
                    return {"id": old["id"], "reused": True}
                identifier, timestamp = uid(), now()
                c.execute(
                    """INSERT INTO v2_runs
                    (id,kind,corpus,target_id,fingerprint,mode,status,snapshot_json,limits_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'queued',?,?,?,?)""",
                    (
                        identifier,
                        kind,
                        corpus,
                        target,
                        fingerprint,
                        mode,
                        dump(snapshot),
                        dump({k: getattr(self.settings, k) for k in LIMIT_KEYS}),
                        timestamp,
                        timestamp,
                    ),
                )
                return {"id": identifier, "reused": False}

            return once(c, operation_id, ["v2.run", fingerprint], save)

    def detail(self, run_id):
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM v2_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise InputError("任务不存在。")
            result = dict(row)
            for field in ("snapshot", "limits", "result"):
                result[field] = json.loads(result.pop(field + "_json") or "null")
            result["attempts"] = [
                dict(r)
                for r in c.execute(
                    """SELECT step,kind,status,error_code,elapsed_seconds,prompt_tokens,
                completion_tokens,total_tokens,cache_hit,request_json FROM v2_run_attempts
                WHERE run_id=? ORDER BY rowid""",
                    (run_id,),
                )
            ]
        for a in result["attempts"]:
            a["request"] = json.loads(a.pop("request_json"))
        result["model_calls"] = sum(a["kind"] == "model" for a in result["attempts"])
        result["tool_calls"] = sum(a["kind"] == "tool" for a in result["attempts"])
        result["cache_hits"] = sum(a["cache_hit"] for a in result["attempts"])
        result["tool_errors"] = sum(
            a["kind"] == "tool" and a["error_code"] == "tool_rejected" for a in result["attempts"]
        )
        result["can_recover_tools"] = (
            result["kind"] == "preparation"
            and result["status"] == "failed"
            and result["error_code"] == "tool_rejected"
        )
        result["known_tokens"] = sum(a["total_tokens"] or 0 for a in result["attempts"])
        result["unknown_usage"] = sum(
            a["kind"] == "model" and a["total_tokens"] is None for a in result["attempts"]
        )
        result["message"] = RUN_ERRORS.get(result["error_code"], "")
        if result["can_recover_tools"]:
            result["message"] = (
                "模型提交了无效的经历编号或工具参数，本次读取已拦截。可以沿用已有记录纠正后继续。"
            )
        return result

    def resume(self, run_id):
        """明确恢复旧参数错误或中断任务；同一进程锁下排队，GET 不会调用。"""
        with task_lock(self.db.data_dir, run_id) as acquired:
            run = self.detail(run_id)
            if not acquired or run["status"] == "completed":
                return {"status": run["status"]}
            if run["status"] == "failed" and not run["can_recover_tools"]:
                raise InputError("这次停止原因不支持直接继续。请查看错误说明；记录已保留。")
            if run["kind"] == "preparation":
                from ..agent.experience_tools import fresh

                try:
                    fresh(self.db, run["snapshot"])
                except TaskStopped as exc:
                    raise InputError(RUN_ERRORS[exc.code]) from None
            if run["kind"] in ("recommendation", "resume"):
                from .resumes_v2 import check_fresh

                try:
                    check_fresh(self.db, run["snapshot"])
                except TaskStopped as exc:
                    raise InputError(RUN_ERRORS[exc.code]) from None
            if run["status"] == "failed":
                # 保留原快照、限额和全部尝试，仅把可修复状态重新排队。
                self._status(run_id, "queued")
            return {"status": "queued" if run["status"] == "failed" else run["status"]}

    def list_for_job(self, job_id):
        with self.db.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    """SELECT id,status,mode,created_at FROM v2_runs
                WHERE kind='preparation' AND target_id=? ORDER BY created_at DESC""",
                    (job_id,),
                )
            ]

    def attempt(self, run, step, kind, request, call, cache_hit=False):
        guard_secret(request, self.settings, "invalid_request")
        logged = media_summary(request)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM v2_run_attempts WHERE run_id=? AND step=?", (run["id"], step)
            ).fetchone()
            if old:
                if digest(json.loads(old["request_json"])) != digest(logged):
                    raise TaskStopped("uncertain_attempt")
                if old["status"] == "ok":
                    value = json.loads(old["result_json"])
                    return ModelReply.model_validate(value) if kind == "model" else value
                if old["status"] == "failed" and old["error_code"] in ERROR_MESSAGES:
                    raise ModelError(old["error_code"])
                raise TaskStopped("uncertain_attempt")
            count = c.execute(
                "SELECT count(*) FROM v2_run_attempts WHERE run_id=? AND kind=?", (run["id"], kind)
            ).fetchone()[0]
            if count >= run["limits"]["max_model_calls" if kind == "model" else "max_tool_calls"]:
                raise ModelError("budget")
            if len(dump(logged)) > run["limits"]["max_input_chars"]:
                raise ModelError("invalid_request")
            c.execute(
                """INSERT INTO v2_run_attempts(run_id,step,kind,status,request_json,cache_hit,created_at)
                VALUES (?,?,?,'started',?,?,?)""",
                (run["id"], step, kind, dump(logged), int(cache_hit), now()),
            )
        started, value, usage, error, settled = time.perf_counter(), None, {}, None, False
        try:
            value = call()
            usage = value.usage.model_dump() if kind == "model" else {}
            payload = value.model_dump() if kind == "model" else value
            guard_secret(payload, self.settings)
            if len(dump(payload)) > max(40000, run["limits"]["max_output_tokens"] * 24):
                raise ModelError("invalid_output")
            settled = True
            return value
        except (ModelError, TaskStopped) as exc:
            error, settled = exc.code, True
            if getattr(exc, "usage", None):
                usage = exc.usage.model_dump()
            raise
        except (sqlite3.Error, OSError):
            # 请求可能已产生费用，保留 started，恢复时不会猜测结果并重发。
            raise
        except Exception:
            error, settled = "internal", True
            raise TaskStopped("internal") from None
        finally:
            if settled:
                with self.db.connect() as c:
                    c.execute(
                        """UPDATE v2_run_attempts SET status=?,result_json=?,error_code=?,
                        elapsed_seconds=?,prompt_tokens=?,completion_tokens=?,total_tokens=?
                        WHERE run_id=? AND step=?""",
                        (
                            "failed" if error else "ok",
                            dump(value.model_dump() if kind == "model" else value)
                            if not error
                            else None,
                            error,
                            time.perf_counter() - started,
                            usage.get("prompt_tokens"),
                            usage.get("completion_tokens"),
                            usage.get("total_tokens"),
                            run["id"],
                            step,
                        ),
                    )

    def complete(self, run, sequence, messages, tools=None, model=None):
        settings = self.settings.model_copy(update={**run["limits"], "mode": run["mode"]})
        model = model or self.model_factory(settings)
        request = {"messages": messages, "tools": tools, "max_tokens": settings.max_output_tokens}
        for retry in range(settings.max_retries + 1):
            try:
                return self.attempt(
                    run,
                    f"model:{sequence}:{retry}",
                    "model",
                    request,
                    lambda: model.complete(
                        messages, tools=tools, max_tokens=settings.max_output_tokens
                    ),
                )
            except ModelError as exc:
                if exc.code not in ("rate_limit", "unavailable") or retry == settings.max_retries:
                    raise
                time.sleep(min(1 + retry, 3))

    def run(self, run_id, model=None):
        try:
            with task_lock(self.db.data_dir, run_id) as acquired:
                if not acquired:
                    return
                run = self.detail(run_id)
                if run["status"] in ("completed", "failed"):
                    return
                self._status(run_id, "processing")
                try:
                    if run["kind"] == "jd":
                        from .jd_v2 import JD

                        result = JD(self.db, self.settings, self).parse(run, model)
                    elif run["kind"] == "preparation":
                        from ..agent.preparation import execute_preparation

                        result = execute_preparation(self, run, model)
                    else:
                        from .resumes_v2 import execute_resume

                        result = execute_resume(self, run, model)
                    self._status(run_id, "completed", result=result)
                except (ModelError, TaskStopped) as exc:
                    self._status(run_id, "failed", exc.code)
                except InputError:
                    self._status(run_id, "failed", "source_file")
                except (OSError, sqlite3.Error):
                    raise
                except Exception:
                    self._status(run_id, "failed", "internal")
        except (OSError, sqlite3.Error):
            try:
                self._status(run_id, "processing", "storage")
            except (OSError, sqlite3.Error):
                pass

    def _status(self, run_id, status, error=None, result=None):
        with self.db.connect() as c:
            c.execute(
                """UPDATE v2_runs SET status=?,error_code=?,result_json=?,updated_at=? WHERE id=?""",
                (status, error, dump(result) if result is not None else None, now(), run_id),
            )
