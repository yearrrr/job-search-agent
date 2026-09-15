"""业务任务、跨进程串行运行和调用账本；检查点不承担业务去重职责。"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from uuid import UUID

from ..agent.contracts import TASK_ERRORS
from ..agent.model import ModelError, ModelReply
from ..agent.privacy import guard_secret
from ..agent.retrieval import compact_fact
from ..schemas import CATEGORY_LABELS, Conflict, InputError, bounded
from .common import digest, now, once, uid
from .jobs import Jobs
from .profile import Profile


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class TaskStopped(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(TASK_ERRORS[code])


def row_task(connection, task_id):
    row = connection.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise InputError("任务不存在，请返回岗位重新打开。")
    return dict(row)


def make_snapshot(database, job_id):
    job = Jobs(database).detail(job_id)
    requirements = []
    for field, label in (
        ("requirements", "任职要求"),
        ("responsibilities", "岗位职责"),
        ("nice_to_have", "加分项"),
    ):
        value = job["fields"][field]
        for line in value["text"].splitlines():
            text = line.strip()
            if not text:
                continue
            if len(text) > 1000:
                raise InputError("岗位要求单条过长，请先将岗位字段按要求分行。")
            requirements.append(
                {
                    "id": f"r{len(requirements) + 1}",
                    "text": text,
                    "kind": label,
                    "method": value["method"],
                    "sources": [
                        s for s in value["sources"] if text in s["quote"] or s["quote"] in text
                    ],
                }
            )
    if not requirements or len(requirements) > 24:
        raise InputError("请先核对岗位字段：职责、要求和加分项合计需要 1–24 条，每条单独一行。")
    facts = [compact_fact(f) for f in Profile(database).confirmed(job["corpus"])]
    if len(facts) > 200:
        raise InputError("首版支持单个资料区最多 200 条已确认事实，请先整理重复条目。")
    return {
        "job_id": job_id,
        "company": job["company"],
        "title": job["title"],
        "corpus": job["corpus"],
        "job_fields_hash": digest(job["fields"]),
        "requirements": requirements,
        "facts": facts,
    }


def ensure_fresh(connection, snapshot):
    row = connection.execute(
        "SELECT fields_json FROM jobs WHERE id=?", (snapshot["job_id"],)
    ).fetchone()
    if not row or digest(json.loads(row["fields_json"])) != snapshot["job_fields_hash"]:
        raise TaskStopped("stale_evidence")
    for fact in snapshot["facts"]:
        current = connection.execute(
            """SELECT f.revision,f.text,f.status,d.corpus,d.kind FROM facts f
               JOIN documents d ON d.id=f.document_id WHERE f.id=?""",
            (fact["id"],),
        ).fetchone()
        if (
            not current
            or current["status"] != "confirmed"
            or current["revision"] != fact["revision"]
            or current["text"] != fact["text"]
            or current["corpus"] != snapshot["corpus"]
            or current["kind"] not in ("resume", "project")
        ):
            raise TaskStopped("stale_evidence")


@contextmanager
def task_lock(data_dir, task_id):
    # UUID 来源于任务表；操作系统锁在进程退出时自动释放，不靠超时抢走正在工作的任务。
    try:
        valid = str(UUID(task_id)) == task_id
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise InputError("任务标识无效。")
    folder = (data_dir / "task-locks").resolve()
    if not folder.is_relative_to(data_dir.resolve()):
        raise OSError("任务锁目录超出本地资料目录。")
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = (folder / f"{task_id}.lock").resolve()
    if not lock_path.is_relative_to(folder.resolve()):
        raise OSError("任务锁路径无效。")
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Tasks:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def create(self, job_id, mode, operation_id):
        if mode not in ("mock", "deepseek"):
            raise InputError("运行模式无效。")
        if mode == "deepseek" and not self.settings.key_configured:
            raise InputError("请先在本机配置 DeepSeek Key；也可选择离线流程演示。")
        # 操作重放应打开原任务，即使事实已经有了新版本。
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def create():
                snapshot = make_snapshot(self.db, job_id)
                guard_secret(snapshot, self.settings, "invalid_request")
                task_id, timestamp = uid(), now()
                limits = {
                    k: getattr(self.settings, k)
                    for k in (
                        "max_model_calls",
                        "max_tool_calls",
                        "max_retries",
                        "max_input_chars",
                        "max_output_tokens",
                        "timeout_seconds",
                        "model",
                    )
                }
                connection.execute(
                    """INSERT INTO agent_tasks(id,job_id,mode,status,snapshot_json,limits_json,created_at,updated_at)
                       VALUES (?,?,?,'queued',?,?,?,?)""",
                    (task_id, job_id, mode, dump(snapshot), dump(limits), timestamp, timestamp),
                )
                return {"id": task_id}

            return once(connection, operation_id, ["agent.create", job_id, mode], create)

    def detail(self, task_id):
        with self.db.connect() as connection:
            result = row_task(connection, task_id)
            for field in ("snapshot", "limits", "view"):
                result[field] = json.loads(result.pop(field + "_json"))
            result.pop("command_json")
            result.pop("command_wait_id")
            attempts = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM agent_attempts WHERE task_id=? ORDER BY created_at,step_key",
                    (task_id,),
                )
            ]
            for a in attempts:
                a["request"] = json.loads(a.pop("request_json"))
                a["result"] = json.loads(a.pop("result_json") or "null")
            result["attempts"] = attempts
            result["model_calls"] = sum(a["kind"] == "model" for a in attempts)
            result["tool_calls"] = sum(a["kind"] == "tool" for a in attempts)
            model_attempts = [a for a in attempts if a["kind"] == "model"]
            result["tokens"] = (
                sum(a["total_tokens"] for a in model_attempts)
                if model_attempts and all(a["total_tokens"] is not None for a in model_attempts)
                else None
            )
            result["elapsed"] = round(sum(a["elapsed_seconds"] or 0 for a in attempts), 3)
            result["known_tokens"] = sum(a["total_tokens"] or 0 for a in model_attempts)
            result["unknown_usage_calls"] = sum(a["total_tokens"] is None for a in model_attempts)
            result["draft"] = connection.execute(
                "SELECT * FROM draft_edits WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            result["draft"] = dict(result["draft"]) if result["draft"] else None
            material = connection.execute(
                "SELECT id,version FROM material_versions WHERE task_id=?", (task_id,)
            ).fetchone()
            result["material"] = dict(material) if material else None
            return result

    def status(self, task_id):
        """轮询只查询摘要，不反复读取整份模型请求和原文。"""
        with self.db.connect() as connection:
            task = connection.execute(
                "SELECT status,revision,error_code FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise InputError("任务不存在，请返回岗位重新打开。")
            counts = connection.execute(
                """SELECT coalesce(sum(kind='model'),0),coalesce(sum(kind='tool'),0)
                   FROM agent_attempts WHERE task_id=?""",
                (task_id,),
            ).fetchone()
            return {
                **{k: task[k] for k in ("status", "revision", "error_code")},
                "model_calls": counts[0],
                "tool_calls": counts[1],
            }

    def list_for_job(self, job_id):
        with self.db.connect() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    "SELECT id,status,mode,created_at FROM agent_tasks WHERE job_id=? ORDER BY created_at DESC",
                    (job_id,),
                )
            ]

    def submit(
        self,
        task_id,
        revision,
        action,
        text,
        category,
        draft_revision,
        operation_id,
        review_token="",
        *,
        connection=None,
    ):
        """只保存用户指令；后台 worker 根据中断 ID 消费，不把旧提交喂给新问题。"""
        if action not in (
            "answer",
            "skip",
            "skip_all",
            "later",
            "confirm_fact",
            "reject_fact",
            "confirm_draft",
            "continue",
        ):
            raise InputError("任务操作无效。")
        if action == "answer":
            text = bounded(text, 1000)
            if "\n" in text or "\r" in text or category not in CATEGORY_LABELS:
                raise InputError(
                    "请用一段文字说明一条经历，并选择分类；多条经历可从原始资料页面补充。"
                )
        else:
            text, category = "", ""
        payload = {
            "action": action,
            "text": text,
            "category": category,
            "draft_revision": draft_revision,
            "review_token": bounded(review_token, 100, empty=True),
        }
        guard_secret(payload, self.settings, "invalid_request")
        owned = connection is None
        with self.db.connect() if owned else nullcontext(connection) as connection:
            if owned:
                connection.execute("BEGIN IMMEDIATE")

            def save():
                task = row_task(connection, task_id)
                if task["status"] == "completed" and action == "confirm_draft":
                    return {"id": task_id}
                if task["revision"] != revision:
                    raise Conflict("任务已有新进展，请刷新后操作。")
                if action == "continue":
                    if task["status"] not in ("queued", "processing"):
                        raise Conflict("当前任务不需要恢复运行，请刷新查看。")
                    return {"id": task_id}
                view = json.loads(task["view_json"])
                kind = (view.get("wait") or {}).get("kind")
                allowed = {
                    "input": {"answer", "skip", "skip_all", "later"},
                    "fact": {"confirm_fact", "reject_fact"},
                    "draft": {"confirm_draft"},
                }
                if task["status"] not in (
                    "waiting_input",
                    "waiting_confirmation",
                ) or action not in allowed.get(kind, set()):
                    raise Conflict("此操作与当前等待步骤不匹配，请刷新页面。")
                if action == "later":
                    connection.execute(
                        "UPDATE agent_tasks SET deferred=1,updated_at=? WHERE id=?",
                        (now(), task_id),
                    )
                else:
                    if action == "confirm_draft":
                        latest = connection.execute(
                            "SELECT revision FROM draft_edits WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                            (task_id,),
                        ).fetchone()
                        if not latest or latest["revision"] != draft_revision:
                            raise Conflict("草稿已有更新，请刷新后核对最新版本。")
                    connection.execute(
                        """UPDATE agent_tasks SET command_json=?,command_wait_id=wait_id,status='processing',
                           deferred=0,updated_at=? WHERE id=?""",
                        (dump(payload), now(), task_id),
                    )
                return {"id": task_id}

            return once(
                connection, operation_id, ["agent.submit", task_id, revision, payload], save
            )

    def attempt(self, task_id, step_key, kind, request, call):
        with self.db.connect() as connection:
            limits = json.loads(row_task(connection, task_id)["limits_json"])
        # 只有明确返回 429/500/503 的模型请求重试；超时、断网、未知结果及工具都不重试。
        maximum = limits.get("max_retries", 0) if kind == "model" else 0
        for retry in range(maximum + 1):
            key = step_key if retry == 0 else f"{step_key}:retry:{retry}"
            try:
                return self._attempt_once(task_id, key, kind, request, call)
            except ModelError as error:
                if error.code not in ("rate_limit", "unavailable") or retry == maximum:
                    raise
                self.wait_retry(task_id, f"{step_key}:retry:{retry + 1}", retry, limits)

    def wait_retry(self, task_id, key, retry, limits):
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT count(*) FROM agent_attempts WHERE task_id=? AND kind='model'", (task_id,)
            ).fetchone()[0]
            cached = connection.execute(
                "SELECT 1 FROM agent_attempts WHERE task_id=? AND step_key=?", (task_id, key)
            ).fetchone()
            if count >= limits["max_model_calls"] and not cached:
                raise ModelError("budget")
            connection.execute(
                "INSERT OR IGNORE INTO agent_retry_schedule VALUES (?,?,?)",
                (task_id, key, time.time() + 0.25 * (2**retry)),
            )
            deadline = connection.execute(
                "SELECT not_before FROM agent_retry_schedule WHERE task_id=? AND step_key=?",
                (task_id, key),
            ).fetchone()[0]
        # 恢复不会重置已等待的时间；系统时钟回拨也不会无限睡眠。
        delay = min(max(0, deadline - time.time()), 0.25 * (2**retry))
        if delay and not cached:
            time.sleep(delay)

    def _attempt_once(self, task_id, step_key, kind, request, call):
        guard_secret(request, self.settings, "invalid_request")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = row_task(connection, task_id)
            limits = json.loads(task["limits_json"])
            existing = connection.execute(
                "SELECT * FROM agent_attempts WHERE task_id=? AND step_key=?", (task_id, step_key)
            ).fetchone()
            if existing:
                if digest(json.loads(existing["request_json"])) != digest(request):
                    raise TaskStopped("uncertain_attempt")
                if existing["status"] == "ok":
                    data = json.loads(existing["result_json"])
                    return ModelReply.model_validate(data) if kind == "model" else data
                if existing["status"] == "failed" and existing["error_code"] not in TASK_ERRORS:
                    raise ModelError(existing["error_code"])
                raise TaskStopped("uncertain_attempt")
            count = connection.execute(
                "SELECT count(*) FROM agent_attempts WHERE task_id=? AND kind=?", (task_id, kind)
            ).fetchone()[0]
            if count >= limits["max_model_calls" if kind == "model" else "max_tool_calls"]:
                raise ModelError("budget")
            if len(dump(request)) > limits["max_input_chars"]:
                raise ModelError("invalid_request")
            connection.execute(
                """INSERT INTO agent_attempts(task_id,step_key,kind,status,request_json,created_at)
                   VALUES (?,?,?,'started',?,?)""",
                (task_id, step_key, kind, dump(request), now()),
            )
        started, result, error_code = time.perf_counter(), None, None
        settled, usage = False, {}
        try:
            result = call()
            raw_result = result.model_dump() if kind == "model" else result
            usage = result.usage.model_dump() if kind == "model" else {}
            guard_secret(raw_result, self.settings)
            if len(dump(raw_result)) > 40000:
                raise ModelError("invalid_output")
            settled = True
            return result
        except (ModelError, TaskStopped) as error:
            error_code = error.code
            if getattr(error, "usage", None) is not None:
                usage = error.usage.model_dump()
            settled = True
            raise
        except Exception:
            error_code = "internal"
            settled = True
            raise TaskStopped("internal") from None
        finally:
            # SystemExit/KeyboardInterrupt 等未完成尝试保留 started，不能伪装成功或用量为零。
            if settled:
                self.finish_attempt(task_id, step_key, kind, result, error_code, started, usage)

    def finish_attempt(self, task_id, step_key, kind, result, error_code, started, usage):
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE agent_attempts SET status=?,result_json=?,error_code=?,elapsed_seconds=?,
                       prompt_tokens=?,completion_tokens=?,total_tokens=? WHERE task_id=? AND step_key=?""",
                (
                    "failed" if error_code else "ok",
                    dump(result.model_dump() if kind == "model" else result)
                    if result is not None and not error_code
                    else None,
                    error_code,
                    time.perf_counter() - started,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    usage.get("total_tokens"),
                    task_id,
                    step_key,
                ),
            )

    def run(self, task_id, model=None):
        try:
            self._run(task_id, model)
        except (OSError, sqlite3.Error):
            self.storage_error(task_id)

    def storage_error(self, task_id):
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE agent_tasks SET error_code='storage' WHERE id=?", (task_id,)
                )
        except (OSError, sqlite3.Error):
            pass

    def _run(self, task_id, model=None):
        from ..agent.workflow import execute_task

        # 页面和进程都使用同一入口；刷新 GET 不会执行模型。
        with task_lock(self.db.data_dir, task_id) as acquired:
            if not acquired:
                return
            with self.db.connect() as connection:
                task = row_task(connection, task_id)
                if task["status"] in ("completed", "failed"):
                    return
                if task["status"].startswith("waiting") and not task["command_json"]:
                    return
                connection.execute(
                    "UPDATE agent_tasks SET status='processing',updated_at=? WHERE id=?",
                    (now(), task_id),
                )
            try:
                view, status, wait_id = execute_task(self, task, model)
                with self.db.connect() as connection:
                    connection.execute(
                        """UPDATE agent_tasks SET view_json=?,status=?,wait_id=?,command_json=NULL,
                           command_wait_id=NULL,revision=revision+1,error_code=NULL,updated_at=? WHERE id=?""",
                        (dump(view), status, wait_id, now(), task_id),
                    )
            except (ModelError, TaskStopped) as error:
                self.fail(task_id, error.code)
            except (OSError, sqlite3.Error):
                # 保留未消费的指令和 processing 状态，修复存储后可从检查点重放。
                self.storage_error(task_id)
            except Exception:
                self.fail(task_id, "internal")

    def fail(self, task_id, code):
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET status='failed',error_code=?,updated_at=? WHERE id=?",
                (code, now(), task_id),
            )

    def save_progress(self, task_id, view):
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET view_json=?,updated_at=? WHERE id=?",
                (dump(view), now(), task_id),
            )
