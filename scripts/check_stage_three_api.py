"""显式联网的固定虚构夹具；独立数据库，不读用户导入的档案，不修改 .env。"""

import argparse
import hashlib
import json
from pathlib import Path

from job_search_agent.config import ConfigurationError, load_settings
from job_search_agent.db import Database
from job_search_agent.schemas import JobInput
from job_search_agent.services.documents import Documents
from job_search_agent.services.tasks import Tasks

RESUME = """虚构测试：下面全部是固定合成数据，仅用于工程验证。
## 教育背景
虚构同学，计算机科学与技术，本科在读，预计 2027 年毕业，当前没有硕士学历。
## 项目经历
使用 Python 编写 API 服务和 pytest 测试，使用 LangGraph 实现工具调用与 SQLite 状态恢复。
"""
JD = """虚构测试 JD，仅用于工程验证，不是真实招聘。
任职要求：
能使用 Python 编写 API 和测试。
硕士及以上学历。
加分项：
使用过 Docker 部署项目。
"""


def main():
    parser = argparse.ArgumentParser(description="阶段三：独立虚构夹具真实工具循环验证")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print(
            "未发送请求。加 --live 才会将固定合成简历/JD 发往 DeepSeek；最多 6 次模型、8 次工具尝试，无自动重试。"
        )
        return 2
    env_file = Path(".env")
    before = hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
    settings = load_settings()
    settings = settings.model_copy(
        update={
            "data_dir": settings.data_dir / "stage-three-live-fixture",
            "mode": "deepseek",
            "max_model_calls": 6,
            "max_retries": 0,
            "max_tool_calls": 8,
        }
    )
    db = Database(settings.data_dir)
    db.initialize()
    docs = Documents(db)
    doc_id = docs.import_file(
        "stage-three-fixture.txt",
        RESUME.encode("utf-8"),
        "resume",
        "demo",
        "stage-three-fixture-import-v1",
    )["id"]
    expected = {
        line for line in RESUME.splitlines() if line and not line.startswith(("虚构测试", "##"))
    }
    for f in docs.detail(doc_id)["facts"]:
        if f["text"] not in expected:
            raise ValueError("固定夹具已变化，停止测试。")
        if f["status"] == "pending":
            docs.update_fact(
                f["id"],
                f["text"],
                f["category"],
                "confirmed",
                f["revision"],
                "fixture-confirm:" + f["id"],
            )
    from job_search_agent.services.jobs import Jobs

    job_id = Jobs(db).create(
        JobInput(
            company="阶段三合成企业（虚构）", title="AI Agent 流程测试", corpus="demo", raw_jd=JD
        ),
        "stage-three-fixture-job-v1",
    )["id"]
    tasks = Tasks(db, settings)
    task_id = tasks.create(job_id, "deepseek", "stage-three-live-task-v2")["id"]
    tasks.run(task_id)
    task = tasks.detail(task_id)
    if task["status"] == "waiting_input":
        tasks.submit(task_id, task["revision"], "skip_all", "", "", 0, "stage-three-live-skip-v2")
        tasks.run(task_id)
        task = tasks.detail(task_id)
    report = {
        k: task[k]
        for k in ("id", "status", "error_code", "model_calls", "tool_calls", "tokens", "elapsed")
    }
    report["model"] = settings.model
    report["judgments"] = task["view"].get("items")
    report["env_unchanged"] = before == (
        hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
    )
    report["draft"] = task["draft"]["content"] if task["draft"] else None
    report_file = db.data_dir / "report.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("draft", "judgments")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if task["status"] in ("waiting_confirmation", "completed") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, ValueError, OSError):
        print("本地配置或固定夹具校验失败，未继续测试；请检查本地文件。")
        raise SystemExit(2) from None
