"""便捷入口；与 job-agent check-api 使用同一实现。"""

import sys

from job_search_agent.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "check-api")
    raise SystemExit(main())
