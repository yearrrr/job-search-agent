import argparse
import json
import sqlite3
import sys

from .agent.graph import run_smoke
from .agent.model import ModelError
from .config import ConfigurationError, load_settings
from .probe import check_api


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="个人求职管家：资料与岗位管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="仅绑定 127.0.0.1 启动本地页面")
    sub.add_parser("seed-demo", help="加载虚构简历和岗位，不自动确认事实")
    smoke = sub.add_parser("smoke", help="离线虚构演示，不调用真实模型")
    smoke.add_argument("action", choices=["start", "status", "resume"])
    smoke.add_argument("--task-id", required=True)
    smoke.add_argument("--reject", action="store_true", help="恢复时不保存演示结果")
    probe = sub.add_parser("check-api", help="一次非敏感短文本请求，可能产生 API 费用")
    probe.add_argument("--live", action="store_true", help="显式允许此次真实请求")
    args = parser.parse_args()
    try:
        settings = load_settings()
        if args.command == "seed-demo":
            from .db import Database
            from .services.demo import seed_demo

            database = Database(settings.data_dir)
            database.initialize()
            result = seed_demo(database)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            import uvicorn

            from .main import create_app

            uvicorn.run(
                create_app(settings),
                host="127.0.0.1",
                port=settings.port,
                log_level="warning",
                access_log=False,
            )
            return 0
        if args.command == "smoke":
            result = run_smoke(settings, args.task_id, action=args.action, approved=not args.reject)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["status"] == "failed" else 0
        if not args.live:
            print("未发送请求。真实探测需本地 Key、JOB_AGENT_MODE=deepseek 和 --live。")
            return 2
        result = check_api(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ok" else 1
    except (ConfigurationError, ModelError) as error:
        print(str(error))
        return 2
    except ValueError:
        print("输入参数无效，请查看命令帮助。")
        return 2
    except (OSError, sqlite3.Error):
        print("本地文件或数据库操作失败，请检查资料目录权限和数据库版本。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
