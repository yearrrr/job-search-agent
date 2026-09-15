"""显式 --live 后，只发送仓库中的虚构简历；不打印密钥或完整配置。"""

import argparse
import hashlib
import json
from pathlib import Path

from job_search_agent.config import load_settings
from job_search_agent.db import Database
from job_search_agent.services.demo import seed_demo
from job_search_agent.services.extraction import extract_candidates


def main():
    parser = argparse.ArgumentParser(description="一次虚构简历真实模型字段提取测试")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print("未发送请求。加 --live 才会将公开虚构简历文本发往已配置的 DeepSeek。")
        return 2
    env_file = Path(".env")
    before = hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
    settings = load_settings().model_copy(update={"mode": "deepseek"})
    database = Database(settings.data_dir)
    database.initialize()
    sample = seed_demo(database)
    result = extract_candidates(
        database, settings, sample["documents"][0], "stage-two-live-fictional-v1"
    )
    result["model"] = settings.model
    result["env_unchanged"] = before == (
        hashlib.sha256(env_file.read_bytes()).hexdigest() if env_file.exists() else None
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
