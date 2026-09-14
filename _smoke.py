"""冒烟测试：真的跑一遍消息循环，暴露只会在运行时炸掉的错误。

`first_run=False` 避免弹出首次运行的模态对话框；6 秒后自动退出。
启动方式必须带控制台（python，不是 pythonw），异常才能打到 stdout。
"""

import ctypes
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import PowerMonitorApp  # noqa: E402
from powermon.w32 import WM_PAINT, WM_QUIT, kernel32, user32  # noqa: E402

RUN_SECONDS = 6.0

LOG = Path(__file__).resolve().parent / "_preview" / "smoke.txt"
LOG.parent.mkdir(exist_ok=True)


def say(text: str) -> None:
    """同时写文件，避免被 SIGTERM 掐掉缓冲区里的输出。"""
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    print(text, flush=True)


LOG.write_text("", encoding="utf-8")
say("开始构造应用…")

# PostQuitMessage 只作用于「调用它的线程」，所以从别的线程收尾必须用
# PostThreadMessageW 把 WM_QUIT 投给主线程的消息队列。
MAIN_THREAD = kernel32.GetCurrentThreadId()

app = PowerMonitorApp(first_run=False)
say(f"构造完成（主线程 {MAIN_THREAD}），启动消息循环")


def stopper() -> None:
    time.sleep(RUN_SECONDS - 3)
    # 强制走一遍面板绘制（隐藏窗口也能收到 WM_PAINT）
    if app.panel._hwnd:
        user32.SendMessageW(app.panel._hwnd, WM_PAINT, 0, 0)
        say("  [stopper] 已触发一次面板重绘")
    time.sleep(3)
    user32.PostThreadMessageW(MAIN_THREAD, WM_QUIT, 0, 0)
    say("  [stopper] 已向主线程投递退出消息")


threading.Thread(target=stopper, daemon=True).start()

started = time.perf_counter()
code = app.run()
elapsed = time.perf_counter() - started

snap = app.meter.snapshot()
say(f"消息循环退出码={code} 运行 {elapsed:.1f}s")
say(f"托盘窗口 hwnd={app.tray.hwnd}（0 = 已销毁）")
say(f"采样 {snap.samples} 次 · 已统计 {snap.session_wh:.4f} Wh")
say(f"开机口径={snap.power_on_source} · 本次开机 "
    f"{snap.power_on_seconds / 3600:.2f} h")
say(f"GPU={snap.gpu_names} 实测={snap.gpu_measured} CPU源={snap.cpu_source}")
say("SMOKE OK")
