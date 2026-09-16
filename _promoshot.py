"""宣传片素材：渲染一张「曲线有数据」的详情面板，并把已有 UI 截图一起归档。

_panellive.py 喂的是空曲线（卡片显示「正在采集数据…」），宣传片里不好看。
这里造一段 30 分钟的真实感功率曲线，让曲线卡画满。

输出到 _preview/panel_promo.png
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _panellive import grab, make_snapshot, pump  # noqa: E402

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon import w32  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.panel import Panel  # noqa: E402
from powermon.w32 import user32  # noqa: E402


def fake_curve(now: float, n: int = 180, step: float = 10.0):
    """30 分钟功率曲线：待机 ~110W，几处负载尖峰到 300W+。

    用确定性伪随机（正弦叠加），每次跑出来一样，方便反复出片。
    """
    out = []
    for i in range(n):
        ts = now - (n - 1 - i) * step
        t = i / n
        base = 118.0
        # 两条低频起伏（模拟开合网页 / 编译）
        slow = 34.0 * math.sin(t * math.pi * 4.1 + 0.7)
        # 三个窄峰
        spike = 0.0
        for c, w, h in ((0.22, 0.030, 210.0), (0.55, 0.045, 165.0), (0.83, 0.022, 240.0)):
            d = (t - c) / w
            spike += h * math.exp(-d * d)
        # 细抖动
        jitter = 7.0 * math.sin(i * 2.7) + 4.0 * math.sin(i * 1.13 + 2.0)
        out.append((ts, max(70.0, base + slow + spike + jitter)))
    return out


def main() -> int:
    enable_dpi_awareness()
    name = sys.argv[1] if len(sys.argv) > 1 else "panel_promo.png"

    # 用 Config.load() 而不是 Config()：后者不读盘，面板上的电价卡会显示默认值，
    # 与电价设置窗口那张截图对不上（宣传片里两张图并排出现，口径必须一致）。
    cfg = Config.load()
    panel = Panel(cfg)
    if not panel.create():
        print("面板创建失败")
        return 1

    now = time.time()
    snap = make_snapshot(
        cfg,
        now,
        session_wh=1284.6,
        session_cost=0.96,
        covered_seconds=17640.0,
        average_w=124.0,
        peak_w=418.0,
        current_w=137.0,
        cpu_w=64.0,
        gpu_w=41.0,
        base_w=32.0,
        cpu_util=38.0,
        today_wh=1284.0,
        today_cost=0.96,
        power_on_ts=now - 21600.0,
        first_seen_ts=now - 17640.0,
    )
    panel.set_data(snap, fake_curve(now))
    panel.show_panel()
    pump(1.0)

    r = w32.wintypes.RECT()
    user32.GetWindowRect(panel._hwnd, __import__("ctypes").byref(r))
    print(f"窗口 ({r.left},{r.top})-({r.right},{r.bottom}) "
          f"{r.right - r.left}x{r.bottom - r.top}")
    # 不留桌面背景，只抓窗口本身；再往内收一点，避开投影
    grab(r.left, r.top, r.right - r.left, r.bottom - r.top, name)
    panel.destroy()
    print("已销毁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
