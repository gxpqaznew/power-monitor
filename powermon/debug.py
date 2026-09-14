"""排障日志：设 ``POWERMON_DEBUG=1`` 后，关键分支写到 ``debug.log``。

托盘登记、Shell_NotifyIcon 这类行为高度依赖 explorer 的时序——出问题时
只看「结果对不对」没法定位，必须留下程序自己的视角。默认关闭，零开销。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_ENABLED = os.environ.get("POWERMON_DEBUG") == "1"


def enabled() -> bool:
    return _ENABLED


def log(tag: str, msg: str) -> None:
    """追加一行日志。任何异常都吞掉——排障代码不该影响主流程。"""
    if not _ENABLED:
        return
    try:
        from .config import app_dir

        with open(Path(app_dir()) / "debug.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}\n")
    except OSError:
        pass
