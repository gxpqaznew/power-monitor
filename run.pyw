"""无控制台启动入口。双击运行，或由计划任务 / 注册表自启调用。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import main  # noqa: E402

raise SystemExit(main())
