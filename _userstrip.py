"""抓「真实运行中的已安装版本」在任务栏上留下的长条 —— 用来验证用户 config.json
里的字段勾选（不是测试里造的假快照）在实机上到底显示成什么样。

与 ``_striplive.py`` 的区别：那个是自己建一个长条再抓，这个**什么都不建**，
只抓屏。所以要先手动把程序跑起来。

用法：
    python _userstrip.py [x0] [y0] [w] [h] [名字.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon import taskbar, w32  # noqa: E402

import _striplive  # noqa: E402


def main() -> int:
    enable_dpi_awareness()
    argv = sys.argv[1:]
    print(taskbar.describe())
    tb = w32.wintypes.RECT()
    hwnd = w32.user32.FindWindowW("Shell_TrayWnd", None)
    w32.user32.GetWindowRect(hwnd, w32.ctypes.byref(tb))
    start = w32.user32.FindWindowExW(hwnd, None, "Start", None)
    sr = w32.wintypes.RECT()
    w32.user32.GetWindowRect(start, w32.ctypes.byref(sr))
    x0 = int(argv[0]) if argv else 60
    w = int(argv[2]) if len(argv) > 2 else max(0, sr.left - x0)
    y0 = int(argv[1]) if len(argv) > 1 else tb.top - 8
    h = int(argv[3]) if len(argv) > 3 else (tb.bottom - tb.top) + 12
    name = argv[4] if len(argv) > 4 else "strip_user.png"
    print(f"开始按钮左边缘 = {sr.left}；抓取区域 = ({x0},{y0}) {w}x{h}")
    _striplive.grab(x0, y0, w, h, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
