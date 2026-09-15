import json
from pathlib import Path

from ..schemas import JobInput
from .documents import Documents
from .jobs import Jobs

SAMPLES = Path(__file__).parents[1] / "sample_data"


def seed_demo(database):
    """可重复运行；只添加虚构区的原始样例，不替用户确认事实或改变岗位状态。"""
    documents = Documents(database)
    resume = SAMPLES / "fictional-resume.pdf"
    if not resume.exists():
        resume = SAMPLES / "fictional-resume.md"
    result = {"documents": [], "jobs": []}
    for path, kind in [(resume, "resume"), (SAMPLES / "fictional-notes.md", "notes")]:
        result["documents"].append(
            documents.import_file(
                path.name, path.read_bytes(), kind, "demo", "demo-v1-" + path.name
            )["id"]
        )
    jobs = Jobs(database)
    for index, data in enumerate(json.loads((SAMPLES / "jobs.json").read_text(encoding="utf-8"))):
        result["jobs"].append(jobs.create(JobInput(**data), f"demo-v1-job-{index}")["id"])
    return result
