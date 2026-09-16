"""自绘菜单（ctxmenu）真机走查：真弹窗、真命中、真点击、真关掉。

离屏渲染（_menushot.py）管「画得对不对」，这里管「窗口行为对不对」——
这一类组件的故障模式同样是**逻辑全对、就是收不到消息 / 窗口根本弹不出来**：

  A. show() 真建出分层窗口（在 _windows 注册表里、可见、带 WS_EX_LAYERED）；
  B. 卡片中心命中菜单自己、圆角外角穿透（alpha 0 不吃点击）；
  C. 左键点中一个 item → 回调收到 cmd、菜单自动关掉；
  D. hover 移到带子菜单的项 + 定时器到点 → 子菜单向右叠开、第一行与父行对齐；
  E. 焦点真走了（WM_KILLFOCUS 且焦点不在链上）→ 整条链关掉；
  F. ESC → 关掉；
  G. 屏幕像素抽查：菜单真的画出来了（不是白窗口 / 黑窗口）。

鼠标不动（不需要 SetCursorPos —— 点击都是直接 SendMessage 喂坐标）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import ctxmenu  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import (  # noqa: E402
    GWL_EXSTYLE,
    VK_ESCAPE,
    WM_KEYDOWN,
    WM_KILLFOCUS,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_TIMER,
    WS_EX_LAYERED,
    gdi32,
    user32,
    wintypes,
)

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


def lp(x: int, y: int) -> int:
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


def pump(seconds: float = 0.15) -> None:
    """抽一会儿消息：分层窗口 present / SetForegroundWindow 都靠消息循环。"""
    end = time.time() + seconds
    msg = wintypes.MSG()
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def grab_screen(x: int, y: int, w: int, h: int) -> bytes:
    """把屏幕一块区域抓成 RGB bytes（验证「真画出来了」）。"""
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020)   # SRCCOPY
    buf = ctypes.create_string_buffer(w * h * 4)
    from powermon.w32 import BITMAPINFO
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
        wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ]
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(bi.bmiHeader)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h            # 顶向下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    data = buf.raw
    out = bytearray(w * h * 3)
    for i in range(w * h):
        out[i * 3] = data[i * 4 + 2]       # B → R
        out[i * 3 + 1] = data[i * 4 + 1]
        out[i * 3 + 2] = data[i * 4]
    return bytes(out)


def sample_entries():
    return [
        {"type": "label", "text": "本次开机", "value": "2 小时 3 分"},
        {"type": "sep"},
        {"type": "item", "cmd": 1, "text": "打开详情面板"},
        {"type": "item", "cmd": 20, "text": "开机自启动", "checked": True},
        {"type": "sep"},
        {"type": "sub", "text": "长条质感", "entries": [
            {"type": "item", "cmd": 50, "text": "石墨（深色）", "checked": True,
             "radio": True},
            {"type": "item", "cmd": 51, "text": "宣纸（浅色）", "radio": True},
        ]},
        {"type": "item", "cmd": 40, "text": "退出"},
    ]


def main() -> int:
    enable_dpi_awareness()
    pt0 = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt0))
    got_cmds: list[int] = []
    fired = got_cmds.append

    entries = sample_entries()
    # 弹在屏幕中下部（躲开任务栏），真实托盘弹出是在右下角往上弹
    menu = ctxmenu.show(entries, 900, 900, fired)
    check("show() 建出了菜单窗口", menu is not None and bool(menu.hwnd))
    if menu is None or not menu.hwnd:
        return 1
    pump()

    # ---- A. 窗口属性 ----
    check("窗口在 _windows 注册表里", menu.hwnd in ctxmenu._windows)
    check("窗口可见", bool(user32.IsWindowVisible(menu.hwnd)))
    ex = user32.GetWindowLongPtrW(menu.hwnd, GWL_EXSTYLE)
    check("带 WS_EX_LAYERED（半透明卡片的前提）",
          bool(ex & WS_EX_LAYERED), f"exstyle={ex:#x}")

    l, t, r, b = rect_of(menu.hwnd)
    check("菜单向上弹（底边贴着弹出点）", abs(b - 900) <= 2,
          f"bottom={b} origin_y=900")

    # ---- B. 命中测试：卡片中心命中自己，圆角外角穿透 ----
    hit_mid = user32.WindowFromPoint(wintypes.POINT((l + r) // 2, (t + b) // 2))
    cls_buf = ctypes.create_unicode_buffer(64)
    if hit_mid:
        user32.GetClassNameW(hit_mid, cls_buf, 64)
    print(f"  hit_mid={hit_mid:#x} menu={menu.hwnd:#x} class={cls_buf.value}")
    check("卡片中心命中菜单自己", hit_mid == menu.hwnd,
          f"{hit_mid:#x}({cls_buf.value}) vs {menu.hwnd:#x}")
    hit_corner = user32.WindowFromPoint(wintypes.POINT(l + 2, t + 2))
    check("圆角外角穿透（alpha 0 不吃点击）",
          hit_corner != menu.hwnd, f"{hit_corner:#x}")

    # ---- G. 屏幕像素：真画出来了 ----
    grab = grab_screen(l, t, r - l, b - t)
    w, h = r - l, b - t
    mid_px = grab[((h // 2) * w + w // 2) * 3: ((h // 2) * w + w // 2) * 3 + 3]
    dark = (mid_px[0] < 120 and mid_px[1] < 120 and mid_px[2] < 140)
    check("屏幕上卡片中心是深色（真画出来了）", dark, str(tuple(mid_px)))

    # ---- D. 子菜单叠开 ----
    geom = menu._geom
    i_sub = next(i for i, (e, _y, _h) in enumerate(geom.rows)
                 if e.get("type") == "sub")
    _e, sry, srh = geom.rows[i_sub]
    # 真实光标必须真的压在菜单上 —— 不然 _set_hover 登记的 TME_LEAVE 会让
    # 系统立刻补一条 WM_MOUSELEAVE（光标根本不在窗口上），刚开的子菜单又关掉。
    user32.SetCursorPos(l + geom.margin + 8, t + geom.margin + sry + srh // 2)
    user32.SendMessageW(menu.hwnd, WM_MOUSEMOVE, 0,
                        lp(geom.margin + 8, geom.margin + sry + srh // 2))
    check("hover 落在子菜单行上", menu.hover == i_sub,
          f"hover={menu.hover} sub={i_sub}")
    user32.SendMessageW(menu.hwnd, WM_TIMER, 1, 0)
    pump()
    check("定时器到点 → 子菜单叠开", menu.child is not None and bool(
        menu.child and menu.child.hwnd))
    if menu.child is not None and menu.child.hwnd:
        cl, ct, cr, cb = rect_of(menu.child.hwnd)
        parent_row_top = t + geom.margin + sry
        check("子菜单在父菜单右边叠开", cl >= r - geom.margin - 4,
              f"child_left={cl} parent_right={r}")
        check("子菜单第一行与父行对齐（上下差 < 12px）",
              abs(ct + menu.child._geom.margin + menu.child._geom.pad
                  - parent_row_top) < 12,
              f"child_first_row≈{ct + menu.child._geom.margin + menu.child._geom.pad} "
              f"vs parent_row={parent_row_top}")
        # 点子菜单里没勾的那一项（cmd=51）
        cgeom = menu.child._geom
        j = next(i for i, (e, _y, _h) in enumerate(cgeom.rows)
                 if e.get("cmd") == 51)
        _e, jy, jh = cgeom.rows[j]
        user32.SendMessageW(menu.child.hwnd, WM_LBUTTONUP, 0,
                            lp(cgeom.margin + 8, cgeom.margin + jy + jh // 2))
        pump()
        check("点子菜单项 → 回调收到它的 cmd", got_cmds == [51], str(got_cmds))
        check("点完菜单自动关掉", menu.hwnd is None
              or not user32.IsWindow(menu.hwnd or 0))

    # ---- E. 点别处（真把前台让出去 → 系统送来真 KILLFOCUS）→ 关 ----
    got_cmds.clear()
    menu2 = ctxmenu.show(entries, 900, 900, fired)
    check("再次弹出（测 KILLFOCUS）", menu2 is not None and bool(
        menu2 and menu2.hwnd))
    if menu2 and menu2.hwnd:
        pump()
        # 假造 WM_KILLFOCUS 不算数（GetFocus 读的还是菜单自己）——
        # 真把前台让给任务栏，系统才会给我们送真的 KILLFOCUS。
        tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)
        user32.SetForegroundWindow(tray_wnd)
        pump(0.3)
        check("前台让出去 → 收到真 KILLFOCUS → 整条菜单关掉",
              not user32.IsWindow(menu2.hwnd))

    # ---- F. ESC 关 ----
    menu3 = ctxmenu.show(entries, 900, 900, fired)
    if menu3 and menu3.hwnd:
        pump()
        user32.SendMessageW(menu3.hwnd, WM_KEYDOWN, VK_ESCAPE, 0)
        pump()
        check("ESC → 关掉", not user32.IsWindow(menu3.hwnd))
    else:
        check("ESC → 关掉", False, "菜单没建出来")

    # ---- C. 点 item → 回调 + 自动关 ----
    got_cmds.clear()
    menu4 = ctxmenu.show(entries, 900, 900, fired)
    check("再次弹出（测点击分发）", menu4 is not None and bool(
        menu4 and menu4.hwnd))
    if menu4 and menu4.hwnd:
        pump()
        geom4 = menu4._geom
        k = next(i for i, (e, _y, _h) in enumerate(geom4.rows)
                 if e.get("cmd") == 1)
        _e, ky, kh = geom4.rows[k]
        user32.SendMessageW(menu4.hwnd, WM_LBUTTONUP, 0,
                            lp(geom4.margin + 8, geom4.margin + ky + kh // 2))
        pump()
        check("点「打开详情面板」→ 回调收到 cmd=1", got_cmds == [1], str(got_cmds))
        check("点完自动关掉", not user32.IsWindow(menu4.hwnd))

    ctxmenu.close_all()
    user32.SetCursorPos(pt0.x, pt0.y)      # 光标还回去
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
