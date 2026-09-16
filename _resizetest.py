"""真机验证：拖长条上下边缘改字号（无级缩放，菜单六档只是快捷取值）。

用户的原话是「长条不能自动拖动改变大小」。实现：光标落在胶囊上下边缘带
（4 设计像素）里变 ↕，按住竖拖无级调字号（0.60~1.60），**拖动中不落盘、
松手才写** ``cfg.strip_font_scale``；上下方向互补于「横拖挪位置」。

这个脚本必须真建窗口、真挪光标（resize 读的是 ``GetCursorPos``，光标不动
位移恒为 0 —— 和拖动一样的坑），真问窗口现在的尺寸：

  A. 光标在上下边缘带 → 光标变 ↕（IDC_SIZENS）；中间 → ↔（IDC_SIZEWE）；
  B. 按住上边缘往上拖 → 字号变大、窗口跟着变高、拖动中不落盘；
  C. 松手 → ``strip_font_scale`` 以连续值落盘、下一帧不弹回；
  D. 拖过头被夹住（上限 1.60 / 下限 0.60）；
  E. 微动（变化 < 0.02）不重排、不落盘；
  F. 中间按住还是横拖挪位置（没把老功能顶掉）；
  G. 捕获被抢（WM_CAPTURECHANGED）当成松手；
  H. 新实例（= 重启程序）接上连续字号，窗口高度真的不一样。

**不会碰用户的真实配置**：配置路径指到临时目录；鼠标会被临时挪动，跑完还原。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import config as config_mod  # noqa: E402
from powermon import stripopts, taskbar  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.strip import _RESIZE_RATE, TaskbarStrip  # noqa: E402
from powermon.w32 import (  # noqa: E402
    HTCLIENT,
    IDC_SIZENS,
    IDC_SIZEWE,
    int_resource,
    user32,
    wintypes,
)

PASS = FAIL = 0
MK_LBUTTON = 0x0001
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_SETCURSOR = 0x0020
WM_CAPTURECHANGED = 0x0215


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def send(hwnd, msg, wparam=0, lparam=0):
    return user32.SendMessageW(hwnd, msg, wparam, lparam)


def lp(x: int, y: int) -> int:
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def current_cursor_id() -> int | None:
    """当前光标对应哪个系统光标（32644=↔ / 32645=↕），都不是返回 None。"""
    cur = user32.GetCursor()
    for cid in (IDC_SIZEWE, IDC_SIZENS):
        if cur == user32.LoadCursorW(None, int_resource(cid)):
            return cid
    return None


def main() -> int:
    enable_dpi_awareness()
    info = taskbar.taskbar()
    print(f"taskbar = {info}")
    if info is None:
        print("没有任务栏（explorer 没跑？）—— 这个测试无效")
        return 1

    from _striplive import make_snapshot

    tmp = Path(tempfile.mkdtemp(prefix="pm_resize_"))
    config_mod.CONFIG_PATH = tmp / "config.json"
    cfg = config_mod.Config()
    cfg.strip_enabled = True
    cfg.strip_theme = "dark"
    cfg.strip_fields = ["current", "cost", "session", "today"]
    cfg.strip_locked = False
    cfg.strip_font_scale = 1.0

    pt0 = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt0))
    strip = TaskbarStrip(cfg)
    snap = make_snapshot(cfg, time.time())
    try:
        ok = strip.create(snap)
        check("长条建得出来（要有任务栏）", ok, f"hwnd={strip.hwnd}")
        if not ok:
            return 1
        strip.tick(snap)
        time.sleep(0.2)

        left, top, right, bottom = strip._rect
        width = right - left
        height = bottom - top
        scale = strip._scale or 1.0
        band = strip._edge_band()
        cx = left + width // 2
        print(f"胶囊 rect={strip._rect} 高={height} scale={scale:.3f} "
              f"边缘带={band:.1f}px")

        # ---- A. 光标形状：边缘 ↕ / 中间 ↔ ----
        user32.SetCursorPos(cx, top + 2)          # 顶边缘带里
        send(strip.hwnd, WM_SETCURSOR, strip.hwnd, HTCLIENT)
        cid_top = current_cursor_id()
        check("光标压在顶边缘带 → 变 ↕（IDC_SIZENS）",
              cid_top == IDC_SIZENS, f"cursor={cid_top}")
        user32.SetCursorPos(cx, top + height // 2)
        send(strip.hwnd, WM_SETCURSOR, strip.hwnd, HTCLIENT)
        cid_mid = current_cursor_id()
        check("光标压在中间 → 变 ↔（IDC_SIZEWE）",
              cid_mid == IDC_SIZEWE, f"cursor={cid_mid}")

        # ---- B. 按住顶边缘往上拖：字号变大、窗口变高、拖动中不落盘 ----
        rise_px = 30                              # 物理像素
        user32.SetCursorPos(cx, top + 2)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp(width // 2, 2))
        check("顶边缘按下 → 进入缩放（不是横拖）",
              strip._resize is not None and strip._drag is None)
        user32.SetCursorPos(cx, top + 2 - rise_px)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        dy_design = rise_px / scale
        want = min(stripopts.FONT_SCALE_MAX,
                   1.0 + dy_design / 48.0 * _RESIZE_RATE)
        got = strip._resize_scale or 0.0
        check(f"上拖 {rise_px}px → 字号 ≈ {want:.3f}",
              abs(got - want) < 0.03, f"resize_scale={got:.3f} want={want:.3f}")
        h2 = rect_of(strip.hwnd)[3] - rect_of(strip.hwnd)[1]
        check("字号变大 → 窗口真的变高了", h2 > height,
              f"{height} → {h2}")
        check("拖动中不落盘（松手才写）",
              abs(cfg.strip_font_scale - 1.0) < 1e-9,
              f"cfg={cfg.strip_font_scale}")
        check("缩放期间保持「激活」外观（光标甩出窗口也不闪）",
              strip._hover is True)

        # ---- C. 松手：连续值落盘、下一帧不弹回 ----
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        check("松手后退出缩放状态",
              strip._resize is None and strip._resize_scale is None)
        check("连续字号写进配置", abs(cfg.strip_font_scale - round(want, 3)) < 1e-6,
              f"cfg={cfg.strip_font_scale} want≈{round(want, 3)}")
        on_disk = tmp / "config.json"
        check("配置真的落盘了",
              on_disk.exists() and "strip_font_scale" in
              on_disk.read_text("utf-8"))
        h3 = rect_of(strip.hwnd)[3] - rect_of(strip.hwnd)[1]
        strip.tick(snap)
        h4 = rect_of(strip.hwnd)[3] - rect_of(strip.hwnd)[1]
        check("新字号不会被下一帧弹回去", h4 == h3 and h3 == h2,
              f"{h2} → {h3} → {h4}")

        # ---- D. 拖过头要夹住 ----
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp(width // 2, 2))
        user32.SetCursorPos(cx, top - 3000)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        check("往上甩出屏幕 → 夹在上限 1.60",
              abs(cfg.strip_font_scale - stripopts.FONT_SCALE_MAX) < 1e-9,
              f"cfg={cfg.strip_font_scale}")
        left2, top2, right2, bottom2 = strip._rect
        height2 = bottom2 - top2
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON,
             lp(width // 2, height2 - 2))          # 底边缘带
        check("底边缘按下 → 也是缩放",
              strip._resize is not None)
        user32.SetCursorPos(cx, bottom2 + 3000)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        check("往下甩出屏幕 → 夹在下限 0.60",
              abs(cfg.strip_font_scale - stripopts.FONT_SCALE_MIN) < 1e-9,
              f"cfg={cfg.strip_font_scale}")

        # ---- E. 微动不重排不落盘 ----
        cfg.strip_font_scale = 1.0
        cfg.save()
        strip._key = None
        strip.tick(snap)
        strip._relayout()
        mtime_before = on_disk.stat().st_mtime_ns
        time.sleep(0.05)
        left3, top3, right3, bottom3 = strip._rect
        # 按下前先把光标挪到按下点 —— _resize_start 读的是真实光标
        # （不挪的话它记住的是上一次测试甩出去的位置，位移算出来是天量）
        user32.SetCursorPos((left3 + right3) // 2, top3 + 2)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON,
             lp((right3 - left3) // 2, 2))
        user32.SetCursorPos((left3 + right3) // 2, top3 + 1)   # 只挪 1px
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        skipped = strip._resize_scale
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        check("微动 1px（变化 < 0.02）不触发重排",
              skipped is None or abs(skipped - 1.0) < 0.02,
              f"resize_scale={skipped}")
        check("微动不落盘", abs(cfg.strip_font_scale - 1.0) < 1e-9,
              f"cfg={cfg.strip_font_scale}")

        # ---- F. 中间按住还是横拖（没把老功能顶掉） ----
        mid_y = top3 + (bottom3 - top3) // 2
        before_x = strip._rect[0]
        user32.SetCursorPos((left3 + right3) // 2, mid_y)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON,
             lp((right3 - left3) // 2, (bottom3 - top3) // 2))
        check("中间按住 → 仍是横拖（不进缩放）",
              strip._drag is not None and strip._resize is None)
        user32.SetCursorPos((left3 + right3) // 2 + 60, mid_y)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        check("中间横拖挪的是位置（横坐标动了）",
              strip._rect[0] - before_x == 60,
              f"{before_x} → {strip._rect[0]}")
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        check("横拖松手后字号没被动过",
              abs(cfg.strip_font_scale - 1.0) < 1e-9,
              f"cfg={cfg.strip_font_scale}")

        # ---- G. 捕获被抢 = 松手 ----
        left4, top4, right4, _b4 = strip._rect
        user32.SetCursorPos((left4 + right4) // 2, top4 + 2)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp((right4 - left4) // 2, 2))
        user32.SetCursorPos((left4 + right4) // 2, top4 - 20)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        check("拖一半（缩放进行中）", strip._resize is not None)
        send(strip.hwnd, WM_CAPTURECHANGED, 0, 0)
        check("捕获被抢 → 当成松手（缩放结束、结果落盘）",
              strip._resize is None and cfg.strip_font_scale > 1.0,
              f"cfg={cfg.strip_font_scale}")

        # ---- H. 新实例接上连续字号 ----
        cfg.strip_font_scale = 1.35
        cfg.save()
        strip.destroy()
        strip2 = TaskbarStrip(cfg)
        snap2 = make_snapshot(cfg, time.time())
        try:
            if strip2.create(snap2):
                strip2.tick(snap2)
                check("新实例读到连续字号 1.35",
                      abs(strip2._font_scale() - 1.35) < 1e-6,
                      f"{strip2._font_scale()}")
                # 与 1.0 的高度对照：另外建一个默认字号实例比高度
                cfg1 = config_mod.Config()
                cfg1.strip_enabled = True
                cfg1.strip_theme = "dark"
                cfg1.strip_fields = ["current", "cost", "session", "today"]
                cfg1.strip_font_scale = 1.0
                strip3 = TaskbarStrip(cfg1)
                try:
                    if strip3.create(snap2):
                        strip3.tick(snap2)
                        h135 = rect_of(strip2.hwnd)[3] - rect_of(strip2.hwnd)[1]
                        h100 = rect_of(strip3.hwnd)[3] - rect_of(strip3.hwnd)[1]
                        check("1.35 字号的窗口真的比 1.0 高", h135 > h100,
                              f"{h135} vs {h100}")
                    else:
                        check("1.35 字号的窗口真的比 1.0 高", False, "对照建不出来")
                finally:
                    strip3.destroy()
            else:
                check("新实例读到连续字号 1.35", False, "建不出来")
        finally:
            strip2.destroy()
            strip = None  # 已经 destroy 过，finally 里别再碰
    finally:
        if strip is not None:
            strip.destroy()
        user32.SetCursorPos(pt0.x, pt0.y)
        print("已清理")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
