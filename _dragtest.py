"""真机验证：长条能不能拖。用户的原话是「首先用户不能拖动」。

长条本体是 ``WS_EX_LAYERED | WS_EX_TRANSPARENT`` 的子窗口（穿透点击，不能吃掉
任务栏的右键菜单），所以它自己收不到任何鼠标消息 —— 拖动只能挂在两端那两块
「把手」上。而把手能不能收到鼠标，是纯 Windows 命中测试行为：

  * 做成长条的**子窗口** → 收不到（长条的 WS_EX_TRANSPARENT 会把子窗口一起带成
    穿透，实测 ``WindowFromPoint`` 在把手上返回 ``Shell_TrayWnd``）；
  * 做成任务栏的**兄弟窗口** → 收得到。

所以这个脚本必须真建窗口、真问系统「这一点上是哪个窗口」：

  A. 两块把手都建出来了，位置正好在胶囊两端；
  B. ``WindowFromPoint`` 在把手上 → 把手自己（哪怕长条被抬到把手之上）；
  C. ``WindowFromPoint`` 在长条中间 → 不是长条（穿透点击没被破坏）；
  D. 模拟按住把手横拖 → 长条跟着走、偏移写进配置、再 tick 不会弹回；
  E. 拖过头会被夹住（拖不出屏幕 / 不会盖住时钟）；
  F. 位置复位能回去；重开一个实例（等价于重启程序）位置还在。

**不会碰用户的真实配置**：配置路径指到临时目录。鼠标会被临时挪动（用来产生
真实的拖动增量），跑完还原，不产生任何点击。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import config as config_mod  # noqa: E402
from powermon import taskbar  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import user32, wintypes  # noqa: E402

PASS = FAIL = 0
MK_LBUTTON = 0x0001


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


def center(hwnd) -> tuple[int, int]:
    l, t, r, b = rect_of(hwnd)
    return ((l + r) // 2, (t + b) // 2)


def who(x: int, y: int) -> int:
    return user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))


def send(hwnd, msg, wparam=0, lparam=0):
    return user32.SendMessageW(hwnd, msg, wparam, lparam)


def main() -> int:
    enable_dpi_awareness()
    info = taskbar.taskbar()
    print(f"taskbar = {info}")
    if info is None:
        print("没有任务栏（explorer 没跑？）—— 这个测试无效")
        return 1

    from _striplive import make_snapshot

    # 配置走临时目录：这个脚本会真的 save()，绝不能动用户那份 config.json
    tmp = Path(tempfile.mkdtemp(prefix="pm_drag_"))
    config_mod.CONFIG_PATH = tmp / "config.json"
    cfg = config_mod.Config()
    cfg.strip_enabled = True
    cfg.strip_theme = "dark"          # 固定色调，免得采样影响判断
    cfg.strip_fields = ["current", "cost", "session", "today"]

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

        grips = list(strip._grip_hwnds)
        check("两端各挂了一块拖动把手", len(grips) == 2, f"{len(grips)} 块")
        if len(grips) != 2:
            return 1
        left, top, right, bottom = strip._rect
        gl, gt, gr, gb = rect_of(grips[0])
        hgl, hgt, hgr, hgb = rect_of(grips[1])
        check("左手把贴在胶囊左端",
              abs(gl - left) <= 2 and gb - gt == bottom - top,
              f"把手 {rect_of(grips[0])} vs 胶囊 {(left, top, right, bottom)}")
        check("右手把贴在胶囊右端", abs(hgr - right) <= 2, f"{rect_of(grips[1])}")

        # ---- 命中测试：把手能收到鼠标、长条中间依然穿透 ----
        # 特意先把长条抬到把手**之上**：命中测试要能穿透明窗口落到把手上，
        # 否则 explorer 每次重排 z 序（每 2 秒我们自己也抬一次）都会让把手失灵。
        strip._ensure_above_siblings(force=True)
        cx, cy = center(grips[0])
        hit = who(cx, cy)
        check("WindowFromPoint 在把手上命中的就是把手（长条压在上面也照样命中）",
              hit == grips[0], f"{hit:#x} vs {grips[0]:#x}")

        mx = (left + right) // 2
        my = (top + bottom) // 2
        hit_mid = who(mx, my)
        check("长条中间仍然穿透（不抢任务栏的右键菜单）",
              hit_mid != strip.hwnd, f"{hit_mid:#x}")

        # ---- 拖动 ----
        before = strip._rect
        drag_px = 120
        user32.SetCursorPos(cx, cy)
        send(grips[0], 0x0201, MK_LBUTTON, 0)          # WM_LBUTTONDOWN
        check("按下把手进入拖动状态", strip._drag is not None)
        user32.SetCursorPos(cx + drag_px, cy)
        send(grips[0], 0x0200, MK_LBUTTON, 0)          # WM_MOUSEMOVE
        moved = strip._rect
        check(f"横拖 {drag_px}px → 长条左缘跟着走了 {drag_px}px",
              moved[0] - before[0] == drag_px,
              f"{before[0]} → {moved[0]}")
        check("拖动只改横坐标（高度/竖直位置不动）",
              moved[1] == before[1] and moved[3] - moved[1] == before[3] - before[1],
              f"{before} → {moved}")
        send(grips[0], 0x0202, 0, 0)                   # WM_LBUTTONUP
        check("松手后退出拖动状态", strip._drag is None)
        scale = strip._scale or 1.0
        check("拖动量按设计基准像素写进配置（换 DPI 不跑偏）",
              abs(cfg.strip_offset_x - (drag_px / scale)) < 1.5,
              f"strip_offset_x={cfg.strip_offset_x} scale={scale:.3f}")
        on_disk = tmp / "config.json"
        check("配置真的落盘了（重启后位置还在）",
              on_disk.exists() and "strip_offset_x" in on_disk.read_text("utf-8"))

        strip.tick(snap)
        check("拖着的位置不会被下一帧弹回去", strip._rect == moved,
              f"{strip._rect} vs {moved}")

        # ---- 拖过头要夹住 ----
        cx2, cy2 = center(grips[0])
        user32.SetCursorPos(cx2, cy2)
        send(grips[0], 0x0201, MK_LBUTTON, 0)
        user32.SetCursorPos(-4000, cy2)
        send(grips[0], 0x0200, MK_LBUTTON, 0)
        far_left = strip._rect
        send(grips[0], 0x0202, 0, 0)
        screen = taskbar.screen_rect()
        check("往左拖到屏幕外时被夹住（左缘不进屏幕外侧）",
              far_left[0] >= screen[0] + 4, f"左缘 {far_left[0]} 屏幕 {screen}")

        tray = taskbar.notification_area()
        if tray is not None:
            cx3, cy3 = center(grips[0])
            user32.SetCursorPos(cx3, cy3)
            send(grips[0], 0x0201, MK_LBUTTON, 0)
            user32.SetCursorPos(screen[2] + 2000, cy3)
            send(grips[0], 0x0200, MK_LBUTTON, 0)
            far_right = strip._rect
            send(grips[0], 0x0202, 0, 0)
            check("往右拖到时钟上时被夹住（不盖通知区域）",
                  far_right[2] <= tray[0] + 2,
                  f"右缘 {far_right[2]} 通知区域左边 {tray[0]}")
        strip._grip_sync()

        # ---- 位置复位 ----
        strip.reset_offset()
        strip.tick(snap)
        check("复位后回到默认落点", abs(strip._rect[0] - strip._base_left) <= 2,
              f"{strip._rect[0]} vs 默认 {strip._base_left}")
        check("复位也写进了配置", abs(cfg.strip_offset_x) < 0.01,
              f"{cfg.strip_offset_x}")

        # ---- 换个实例（= 重启程序）位置还在 ----
        # 注意偏移要挑一个**夹不到**的值：默认落点离屏幕左边只有一百来像素，
        # 写 -150 会被左边的夹取吃掉，测出来的就不是「配置接没接手」了。
        strip._offset_nominal = None          # 清掉内存里的临时值，逼它重新读配置
        cfg.strip_offset_x = -60.0
        cfg.save()
        strip.destroy()
        strip2 = TaskbarStrip(cfg)
        snap2 = make_snapshot(cfg, time.time())
        try:
            if strip2.create(snap2):
                strip2.tick(snap2)
                want = -60.0 * (strip2._scale or 1.0)
                delta = strip2._rect[0] - strip2._base_left
                check("重开一个实例位置还在（偏移从配置接手）",
                      abs(delta - int(round(want))) <= 2,
                      f"偏移 {delta}px / 期望 {want:.0f}px")
            else:
                check("重开一个实例位置还在（偏移从配置接手）", False, "建不出来")
        finally:
            strip2.destroy()
    finally:
        strip.destroy()
        user32.SetCursorPos(pt0.x, pt0.y)      # 光标还回去
        print("已清理")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
