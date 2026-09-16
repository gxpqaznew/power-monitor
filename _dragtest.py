"""真机验证：长条能不能拖、悬停有没有提示、锁定后是不是真穿透。

用户报过两件事：先是「用户不能拖动」，改完（两端挂把手、画两列小圆点）之后
又说「不要那左右六个点，太丑了」。所以现在的做法是：

  * **不留任何常驻装饰** —— 整块胶囊就是拖动面，鼠标指上去描边才转成强调色
    （``_apply_hover``）、光标变 ↔，移开就恢复原样；
  * 代价是长条不再穿透点击（要能收到鼠标），所以给了个「锁定位置」开关，
    勾上就加回 ``WS_EX_TRANSPARENT``，变回完全穿不透的纯显示窗口。

这个脚本必须真建窗口、真问系统「这一点上是哪个窗口」，因为这一类问题的故障
模式是**逻辑全对、就是画不出来 / 收不到消息**：

  A. 长条建出来，**任务栏子树里没有任何「把手」窗口**（别再长回来）；
  B. ``WindowFromPoint`` 在胶囊上命中的就是长条（能抓）；
  C. 圆角外面那点仍然穿透（长条不是一块会吃点击的方板）；
  D. 悬停 → ``_hover`` 置位 + 配色真的变了（鼠标指上去有反馈）；
      移开（WM_MOUSELEAVE）→ 复位；
  E. 右键 → 回调被调用（菜单补得回来）；
  F. 按住横拖 → 长条跟着走、只改横坐标、偏移按设计基准像素落盘、下一帧不弹回；
  G. 拖过头会被夹住（拖不出屏幕 / 不会盖住时钟）；
  H. 双击复位能回去；重开一个实例（等价于重启程序）位置还在；
  I. 锁定后扩展样式带上了 WS_EX_TRANSPARENT，胶囊上再也命中不到长条；解锁回来。

**不会碰用户的真实配置**：配置路径指到临时目录。鼠标会被临时挪动（拖动读的是
真实光标 ``GetCursorPos``，不挪光标位移恒为 0），跑完还原，不产生任何点击。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import config as config_mod  # noqa: E402
from powermon import strip as strip_mod  # noqa: E402
from powermon import taskbar  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import (  # noqa: E402
    GWL_EXSTYLE,
    WNDENUMPROC,
    WS_EX_TRANSPARENT,
    user32,
    wintypes,
)

PASS = FAIL = 0
MK_LBUTTON = 0x0001
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_MOUSELEAVE = 0x02A3
GRIP_CLASS = "PowerMonitorStripGrip"     # 已经删掉的把手类名，留着当墓碑守卫


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


def taskbar_descendants() -> list[tuple[int, str]]:
    """任务栏整棵子树（含深层）的 (hwnd, 类名)。

    长条是 ``Shell_TrayWnd`` 的**子窗口**，所以不能用 EnumWindows 找 ——
    那个只枚举顶层窗口，长条一个都看不见。
    """
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    out: list[tuple[int, str]] = []
    if not tray:
        return out

    def _cb(hwnd, _lparam):
        out.append((hwnd, _class_of(hwnd)))
        return True

    user32.EnumChildWindows(tray, WNDENUMPROC(_cb), 0)
    return out


def _class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def ex_style(hwnd) -> int:
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


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
    cfg.strip_locked = False

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

        # ---- A. 别再长出把手窗口 ----
        strip._ensure_above_siblings(force=True)
        names = [n for _h, n in taskbar_descendants()]
        check("任务栏子树里没有任何「拖动把手」窗口（小圆点已经删干净）",
              GRIP_CLASS not in names, f"类名表 {sorted(set(names))}")

        left, top, right, bottom = strip._rect
        check("胶囊尺寸就是窗口尺寸（没有多出来的空白边）",
              rect_of(strip.hwnd) == (left, top, right, bottom),
              f"{rect_of(strip.hwnd)} vs {(left, top, right, bottom)}")

        # ---- B/C. 命中测试 ----
        cx, cy = (left + right) // 2, (top + bottom) // 2
        hit = who(cx, cy)
        check("WindowFromPoint 在胶囊上命中的就是长条（能抓）",
              hit == strip.hwnd, f"{hit:#x} vs {strip.hwnd:#x}")
        hit_corner = who(left + 2, top + 2)
        check("圆角外面那点仍然穿透（长条不是一块吃点击的方板）",
              hit_corner != strip.hwnd, f"{hit_corner:#x}")

        # ---- D. 悬停 ----
        key_off = strip._style_key()
        send(strip.hwnd, WM_MOUSEMOVE, 0, 0)
        key_on = strip._style_key()
        check("鼠标移进胶囊 → 进入悬停态", strip._hover is True)
        check("悬停改变渲染键（这一下必然会重画）", key_on != key_off,
              f"{key_off} vs {key_on}")
        send(strip.hwnd, WM_MOUSELEAVE, 0, 0)
        check("鼠标移开 → 悬停态复位", strip._hover is False)

        # ---- E. 右键补菜单 ----
        fired: list[int] = []
        strip.menu_callback = lambda: fired.append(1)
        send(strip.hwnd, WM_RBUTTONUP, 0, 0)
        check("右键回调被调用（长条吃掉的右键能补回菜单）", fired == [1], str(fired))
        strip.menu_callback = None

        # ---- F. 拖动 ----
        before = strip._rect
        drag_px = 120
        user32.SetCursorPos(cx, cy)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, 0)
        check("按下胶囊进入拖动状态", strip._drag is not None)
        user32.SetCursorPos(cx + drag_px, cy)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        moved = strip._rect
        check(f"横拖 {drag_px}px → 长条左缘跟着走了 {drag_px}px",
              moved[0] - before[0] == drag_px,
              f"{before[0]} → {moved[0]}")
        check("拖动只改横坐标（高度/竖直位置不动）",
              moved[1] == before[1] and moved[3] - moved[1] == before[3] - before[1],
              f"{before} → {moved}")
        check("拖动期间保持「激活」外观（光标被甩出窗口也不闪）",
              strip._hover is True)
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
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

        # ---- G. 拖过头要夹住 ----
        cx2, cy2 = center(strip.hwnd)
        user32.SetCursorPos(cx2, cy2)
        send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, 0)
        user32.SetCursorPos(-4000, cy2)
        send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
        far_left = strip._rect
        send(strip.hwnd, WM_LBUTTONUP, 0, 0)
        screen = taskbar.screen_rect()
        check("往左拖到屏幕外时被夹住（左缘不进屏幕外侧）",
              far_left[0] >= screen[0] + 4, f"左缘 {far_left[0]} 屏幕 {screen}")

        tray = taskbar.notification_area()
        if tray is not None:
            cx3, cy3 = center(strip.hwnd)
            user32.SetCursorPos(cx3, cy3)
            send(strip.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, 0)
            user32.SetCursorPos(screen[2] + 2000, cy3)
            send(strip.hwnd, WM_MOUSEMOVE, MK_LBUTTON, 0)
            far_right = strip._rect
            send(strip.hwnd, WM_LBUTTONUP, 0, 0)
            check("往右拖到时钟上时被夹住（不盖通知区域）",
                  far_right[2] <= tray[0] + 2,
                  f"右缘 {far_right[2]} 通知区域左边 {tray[0]}")

        # ---- H. 双击复位 / 位置复位 ----
        cx4, cy4 = center(strip.hwnd)
        user32.SetCursorPos(cx4, cy4)
        send(strip.hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, 0)
        strip.tick(snap)
        check("双击胶囊 = 位置复位", abs(strip._rect[0] - strip._base_left) <= 2,
              f"{strip._rect[0]} vs 默认 {strip._base_left}")
        check("复位也写进了配置", abs(cfg.strip_offset_x) < 0.01,
              f"{cfg.strip_offset_x}")

        # ---- I. 锁定 / 解锁 ----
        check("默认不锁（不锁才拖得动）",
              not (ex_style(strip.hwnd) & WS_EX_TRANSPARENT),
              f"exstyle={ex_style(strip.hwnd):#x}")
        strip.set_interactive(False)
        check("锁定后带上了 WS_EX_TRANSPARENT",
              bool(ex_style(strip.hwnd) & WS_EX_TRANSPARENT),
              f"exstyle={ex_style(strip.hwnd):#x}")
        l2, t2, r2, b2 = strip._rect
        locked_hit = who((l2 + r2) // 2, (t2 + b2) // 2)
        check("锁定后胶囊上再也命中不到长条（真的穿透了）",
              locked_hit != strip.hwnd, f"{locked_hit:#x}")
        strip.set_interactive(True)
        unlocked_hit = who((l2 + r2) // 2, (t2 + b2) // 2)
        check("解锁后又命中得到（拖得动）", unlocked_hit == strip.hwnd,
              f"{unlocked_hit:#x}")

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
