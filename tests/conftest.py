from uuid import uuid4

import pytest

from job_search_agent.config import ENV_FIELDS, Settings


def pytest_configure(config):
    # 本机系统临时目录可能有旧权限；每次使用项目内新的、被 Git 忽略的目录。
    if config.option.basetemp is None:
        root = (config.rootpath / ".local" / "pytest").resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / str(uuid4())).resolve()
        assert target.is_relative_to(root) and not target.exists()
        config.option.basetemp = str(target)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    # 测试永远不用开发者的 .env、资料目录或真实 Key。
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path):
    # 首版回归继续使用较小预算，验证达到边界时确实停止；V2 默认值另有覆盖。
    return Settings(
        data_dir=tmp_path / "data",
        max_model_calls=8,
        max_tool_calls=12,
        max_input_chars=20000,
        max_output_tokens=2048,
    )


@pytest.fixture
def scenario(settings):
    from job_search_agent.db import Database
    from job_search_agent.schemas import JobInput
    from job_search_agent.services.common import uid
    from job_search_agent.services.documents import Documents
    from job_search_agent.services.jobs import Jobs
    from job_search_agent.services.tasks import Tasks

    db = Database(settings.data_dir)
    db.initialize()
    docs = Documents(db)
    result = docs.import_file(
        "fictional.md",
        "## 教育背景\n虚构同学，计算机本科，2027 年毕业。\n## 项目经历\n使用 Python 编写 API 和测试。\n使用 Docker 部署虚构项目。".encode(),
        "resume",
        "demo",
        uid(),
    )
    facts = docs.detail(result["id"])["facts"]
    for fact in facts[:2]:
        docs.update_fact(
            fact["id"], fact["text"], fact["category"], "confirmed", fact["revision"], uid()
        )
    docs.import_file(
        "notes.txt", "Docker Kubernetes embedding 学习笔记。".encode(), "notes", "demo", uid()
    )
    personal = docs.import_file(
        "personal.txt", "使用 Docker 构建生产服务。".encode(), "project", "personal", uid()
    )
    f = docs.detail(personal["id"])["facts"][0]
    docs.update_fact(f["id"], f["text"], f["category"], "confirmed", f["revision"], uid())
    job = Jobs(db).create(
        JobInput(
            company="虚构企业",
            title="AI Agent 工程师",
            corpus="demo",
            job_code="DEMO-3",
            batch="",
            source_url="",
            raw_jd="任职要求：\n熟悉 Python。\n硕士及以上。\n加分项：\n使用过 Docker。",
        ),
        uid(),
    )
    return db, Tasks(db, settings), job["id"], facts
