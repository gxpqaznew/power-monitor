"""自绘菜单（ctxmenu）真机走查：真弹窗、真命中、真点击、真关掉。

离屏渲染（_menushot.py）管「画得对不对」，这里管「窗口行为对不对」——
这一类组件的故障模式同样是**逻辑全对、就是收不到消息 / 窗口根本弹不出来**：

  A. show() 真建出分层窗口（在 _windows 注册表里、可见、带 WS_EX_LAYERED）；
  B. 卡片中心命中菜单自己、圆角外角穿透（alpha 0 不吃点击）；
  C. 左键点中一个 item → 回调收到 cmd、菜单自动关掉；
  D. hover 移到带子菜单的项 + 定时器到点 → 子菜单向右叠开、第一行与父行对齐；
  E. KILLFOCUS 分两种来源：落座期内没键按下的（explorer 归还前台，假）→ 重抢前台
     留着；落座期内鼠标键按着的（用户真点别处）→ 立刻关；过了落座期 → 立刻关；
  F. ESC → 关掉；
  G. 屏幕像素抽查：菜单真的画出来了（不是白窗口 / 黑窗口）。

鼠标基本不动（点击都是直接 SendMessage 喂坐标）；只有 E2 要造「按住左键」这个
真实状态，用 SendInput 注入按下并在结束时补一次抬起，光标最后还回原位。
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
    mouse_button_down,
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


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]


# SendInput 注入的键会真的更新系统的 async key state（GetAsyncKeyState 读得到），
# 所以「按住左键」这种状态可以真造出来，不用去 mock 我们自己的判据函数。
# dx/dy 给 0：位置交给 SetCursorPos，这里只发按键状态。
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


def send_mouse(flags: int) -> None:
    inp = _INPUT(0, _MOUSEINPUT(0, 0, 0, flags, 0, 0))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def steal_focus(tray_wnd) -> None:
    """模拟「用户切到别的程序」：焦点被清走 + 前台让出去，全程没鼠标键按下。

    只发 ``SetForegroundWindow`` 不够 —— 从**自己进程**调它，系统不保证把本线程的
    焦点也清掉（实测同一套代码：0.08s 时送了 KILLFOCUS，0.5s 时压根没送），
    于是「菜单没关」到底是产品不关还是探针没送到刺激都说不清。
    ``SetFocus(NULL)`` 是同一件事的确定版本：同线程内直接送 WM_KILLFOCUS。
    """
    user32.SetFocus(None)
    user32.SetForegroundWindow(tray_wnd)


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


def hx(hwnd) -> str:
    return f"0x{(hwnd or 0):x}"


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

    # ---- E. KILLFOCUS：两种来源必须分开处理 ----
    #
    # 菜单弹出后要抢前台。可打开菜单的那次点击 explorer 还没处理完，它返回后会把
    # 前台还给任务栏 → 一条「假的」KILLFOCUS 紧跟而来，菜单刚出现就被自己关掉，
    # 用户看到的是「右键弹不出来」。所以落座期内（``ctxmenu._POPUP_SETTLE``）
    # 收到 KILLFOCUS 会先重抢一次前台再说。
    #
    # 🔴 但宽限必须让位于「用户真的按了鼠标」，判据是**此刻有没有鼠标键按着**：
    # 用户点别处时，激活变更由那一下按下触发，KILLFOCUS 是在「按下」那条消息里
    # 同步送来的，键还是按下的；explorer 归还前台那一下早松手了。
    # 少了这一条，宽限期内用户点别处菜单会「抢回来」，看着像关不掉 ——
    # 只是把毛病从一头换到另一头。
    #
    # 三种情形都用**真信号**各验一遍（E2 的按键用 SendInput 真按住，不 mock）：
    #   E1 落座期 + 无键（前台被系统还回去）→ 菜单留着，且确实重抢过
    #   E2 落座期 + 真按住左键（用户点别处）→ 立刻关，一次都不许重抢
    #   E3 过了落座期 + 无键（切走程序）→ 立刻关（宽限不会把菜单钉死）
    tray_wnd = user32.FindWindowW("Shell_TrayWnd", None)

    check("GetAsyncKeyState 声明了 restype=c_short",
          user32.GetAsyncKeyState.restype is ctypes.c_short,
          "按 c_int 读会把 EAX 脏高位当「按下」，判据随机")
    check("没按键时读出来是「没按住」", not mouse_button_down())

    # E1：落座期 + 没键按下（explorer 归还前台那种假 KILLFOCUS）→ 菜单留着
    got_cmds.clear()
    menu2 = ctxmenu.show(entries, 900, 900, fired)
    check("再次弹出（测 KILLFOCUS）", menu2 is not None and bool(
        menu2 and menu2.hwnd))
    if menu2 and menu2.hwnd:
        pump(0.08)
        check("已落座（推后的抢前台确已执行）", menu2._focus_at > 0,
              f"_focus_at={menu2._focus_at}")
        # 假造 WM_KILLFOCUS 不算数（GetFocus 读的还是菜单自己）——
        # 真把前台让给任务栏，系统才会给我们送真的 KILLFOCUS。
        steal_focus(tray_wnd)
        pump(0.15)
        alive = bool(user32.IsWindow(menu2.hwnd))
        check("落座期 + 没键按下 → 重抢前台，菜单留着",
              alive and menu2._refocus_tries >= 1,
              f"alive={alive} tries={menu2._refocus_tries}")
        ctxmenu.close_all()
        pump()
    else:
        check("已落座（推后的抢前台确已执行）", False, "菜单没建出来")
        check("落座期 + 没键按下 → 重抢前台，菜单留着", False, "菜单没建出来")

    # E2：落座期 + 鼠标键按着（用户在点别处）→ 立刻关，一次都不许重抢
    got_cmds.clear()
    menu_b = ctxmenu.show(entries, 900, 900, fired)
    if menu_b and menu_b.hwnd:
        pump(0.08)
        gb = menu_b._geom
        # 卡片顶部内边距的正中：横向是实心、纵向落在 padding 里 —— 按下去不会命中
        # 任何一行（``_hit`` 返回 -1），所以「按住」这个动作本身不会误触发菜单项。
        user32.SetCursorPos(menu_b._pos[0] + gb.w // 2,
                            menu_b._pos[1] + gb.margin + 3)
        pump(0.05)
        send_mouse(_MOUSEEVENTF_LEFTDOWN)
        pump(0.05)
        down = mouse_button_down()
        check("真按住左键 → 读得到「有键按着」", down, f"mouse_button_down()={down}")
        # 和 E1/E3 用**同一个**刺激（清焦点 + 让出前台），唯一区别是这会儿键按着。
        # 这样 E1/E2/E3 就是一个干净的 2×2：落座期内 ×（有键/无键）、落座期外。
        steal_focus(tray_wnd)
        pump(0.25)
        # 先确认「前台真的让出去了」：探针没把刺激送到的话，下面那条断言就没意义。
        fg = user32.GetForegroundWindow()
        check("前台确实让给了任务栏（探针前提）", fg == tray_wnd,
              f"foreground={hx(fg)} tray={hx(tray_wnd)}")
        closed = not user32.IsWindow(menu_b.hwnd)
        check("落座期 + 鼠标键按着（用户点别处）→ 立刻关，不重抢",
              closed and menu_b._refocus_tries == 0,
              f"closed={closed} tries={menu_b._refocus_tries}")
        send_mouse(_MOUSEEVENTF_LEFTUP)
        pump(0.05)
        check("松开后回到「没键按着」", not mouse_button_down())
    else:
        check("真按住左键 → 读得到「有键按着」", False, "菜单没建出来")
        check("前台确实让给了任务栏（探针前提）", False, "菜单没建出来")
        check("落座期 + 鼠标键按着（用户点别处）→ 立刻关，不重抢", False, "菜单没建出来")
        check("松开后回到「没键按着」", False, "菜单没建出来")

    # E3：过了落座期 + 没键按下（真的点了别处）→ 立刻关
    #     （宽限是有界的：不许把菜单钉死在屏幕上）
    got_cmds.clear()
    menu_c = ctxmenu.show(entries, 900, 900, fired)
    if menu_c and menu_c.hwnd:
        pump(0.5)                                # 0.5 > _POPUP_SETTLE(0.35)
        steal_focus(tray_wnd)
        pump(0.25)
        age = time.monotonic() - menu_c._focus_at if menu_c._focus_at else -1
        check("过了落座期 + 无键（切走程序）→ 立刻关",
              not user32.IsWindow(menu_c.hwnd),
              f"alive={bool(user32.IsWindow(menu_c.hwnd))} age={age:.2f}s "
              f"tries={menu_c._refocus_tries}")
    else:
        check("过了落座期 + 无键（切走程序）→ 立刻关", False, "菜单没建出来")

    # E4：弹出那一刻键就已经按着 → 不算「用户这一次点击」，宽限照样生效。
    #     现在没有哪条弹菜单的路是「按下就弹」的（托盘 / 长条都在 BUTTONUP 上弹），
    #     所以这一条是**预防性**的：哪天真改成按下就弹，落座期那次假 KILLFOCUS
    #     就会带着按下的键、被当成「用户点了别处」把菜单秒关。_btn_at_show 用赋值
    #     模拟（真造出「弹出时键已按着」需要先在别处按一手，会带来额外副作用）。
    got_cmds.clear()
    menu_d = ctxmenu.show(entries, 900, 900, fired)
    if menu_d and menu_d.hwnd:
        pump(0.08)
        menu_d._btn_at_show = True
        gd = menu_d._geom
        user32.SetCursorPos(menu_d._pos[0] + gd.w // 2,
                            menu_d._pos[1] + gd.margin + 3)
        pump(0.05)
        send_mouse(_MOUSEEVENTF_LEFTDOWN)        # 真按键，只是「弹出时已按着」是模拟的
        pump(0.05)
        steal_focus(tray_wnd)
        pump(0.25)
        alive = bool(user32.IsWindow(menu_d.hwnd))
        check("弹出时键已按着 → 不当「用户点击」，宽限仍生效",
              alive and menu_d._refocus_tries >= 1,
              f"alive={alive} tries={menu_d._refocus_tries} "
              f"held={mouse_button_down()}")
        send_mouse(_MOUSEEVENTF_LEFTUP)
        pump(0.05)
        ctxmenu.close_all()
        pump()
    else:
        check("弹出时键已按着 → 不当「用户点击」，宽限仍生效", False, "菜单没建出来")

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
