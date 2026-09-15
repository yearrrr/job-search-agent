import subprocess
from pathlib import Path


def test_git_excludes_personal_data_but_keeps_code():
    project = Path(__file__).resolve().parents[1]
    private = [
        ".env",
        ".env.local",
        "data/resume.pdf",
        "data/app.sqlite3",
        "data/checkpoints.sqlite3-wal",
        "data/exports/draft.md",
        ".local/evaluation.json",
        "logs/debug.log",
        ".venv/pyvenv.cfg",
    ]
    public = [
        ".env.example",
        "src/job_search_agent/main.py",
        "tests/test_model.py",
        "requirements.lock",
    ]
    result = subprocess.run(
        ["git", "-C", str(project), "check-ignore", "--no-index", "-z", "--stdin"],
        input=("\0".join(private + public) + "\0").encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, "Git 检查失败；请安装 Git 并在项目目录初始化仓库。"
    assert set(result.stdout.decode().strip("\0").split("\0")) == set(private)
