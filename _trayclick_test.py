"""托盘事件分流：只有「点击类」才代表用户点了托盘图标。

守住的是这个 bug（用户报「右键点击该图标无法正常出现菜单」）：

    explorer 用**同一个**回调消息号转发落在托盘图标上的**所有**鼠标消息 ——
    鼠标在图标上飘一下就是一条 ``WM_MOUSEMOVE``。而 ``TrayIcon._on_message``
    早先对**任何**事件都先 ``ctxmenu.close_all()``，于是：

        右键 → 菜单弹出 → 手腕微动（一条 WM_MOUSEMOVE）→ 菜单当场被关

    用户看到的就是「右键弹不出菜单」。毛玻璃抓屏给弹出加了上百毫秒延迟
    （抓屏期间输入消息在排队），这条路径才被稳定踩中 —— v1.0.12 的菜单是
    立刻弹出的，几乎撞不上。

这条测试不碰真实窗口、不依赖前台权，纯逻辑，任何机器上都能跑。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import ctxmenu, tray, w32  # noqa: E402
from powermon.w32 import WM_TRAYICON  # noqa: E402

PASS = FAIL = 0

WM_MOUSELEAVE = 0x02A3
WM_MOUSEWHEEL = 0x020A
WM_NCHITTEST = 0x0084


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}"
          + (f"  ({detail})" if detail else ""))


class Counter:
    def __init__(self) -> None:
        self.close = 0
        self.popup = 0
        self.cmds: list[int] = []

    def reset(self) -> None:
        self.close = self.popup = 0
        self.cmds = []


def make(c: Counter):
    t = tray.TrayIcon(lambda anchor=None: None,
                      lambda cmd: c.cmds.append(int(cmd)))

    def _popup(anchor=None, origin=None):
        c.popup += 1
    t._popup = _popup          # type: ignore[method-assign]
    return t


def feed(t, event: int) -> None:
    t._on_message(WM_TRAYICON, 1, event)


def main() -> int:
    c = Counter()
    real_close = ctxmenu.close_all
    ctxmenu.close_all = lambda: setattr(c, "close", c.close + 1)
    try:
        t = make(c)

        # ---- 1. 非点击：一条都不该关菜单 ----------------------------
        for ev, name in ((0x0200, "WM_MOUSEMOVE"),
                         (WM_MOUSELEAVE, "WM_MOUSELEAVE"),
                         (WM_MOUSEWHEEL, "WM_MOUSEWHEEL"),
                         (0x020E, "WM_MOUSEHOVER")):
            c.reset()
            feed(t, ev)
            check(f"{name} 不关菜单、不弹菜单（★ 用户报的那个 bug）",
                  c.close == 0 and c.popup == 0 and not c.cmds,
                  f"close={c.close} popup={c.popup}")

        # ---- 2. 右键：先关旧菜单，再弹新的 --------------------------
        c.reset()
        feed(t, 0x0204)                       # WM_RBUTTONDOWN
        check("WM_RBUTTONDOWN 关掉旧菜单，但不弹",
              c.close == 1 and c.popup == 0 and not c.cmds,
              f"close={c.close} popup={c.popup}")

        c.reset()
        feed(t, 0x0205)                       # WM_RBUTTONUP
        check("WM_RBUTTONUP 关掉旧菜单并弹出菜单",
              c.close == 1 and c.popup == 1, f"close={c.close} popup={c.popup}")

        c.reset()
        feed(t, 0x0206)                       # WM_RBUTTONDBLCLK
        check("WM_RBUTTONDBLCLK 也算点击（关菜单）",
              c.close == 1 and c.popup == 0, f"close={c.close}")

        # ---- 3. 左键：开关详情面板 ---------------------------------
        c.reset()
        feed(t, 0x0202)                       # WM_LBUTTONUP
        check("WM_LBUTTONUP 关菜单并派发「开关面板」",
              c.close == 1 and c.cmds == [tray.CMD_TOGGLE_PANEL],
              f"cmds={c.cmds}")

        c.reset()
        feed(t, 0x0201)                       # WM_LBUTTONDOWN
        check("WM_LBUTTONDOWN 只关菜单、不派发命令",
              c.close == 1 and not c.cmds, f"cmds={c.cmds}")

        # ---- 4. 中键：只关菜单 -------------------------------------
        c.reset()
        feed(t, 0x0208)                       # WM_MBUTTONUP
        check("WM_MBUTTONUP 只关菜单", c.close == 1 and c.popup == 0,
              f"close={c.close}")

        # ---- 5. 白名单成员 -----------------------------------------
        need = {0x0201, 0x0202, 0x0203, 0x0204, 0x0205,
                0x0206, 0x0207, 0x0208, 0x0209}
        check("_TRAY_CLICK_EVENTS 覆盖全部 9 个点击类事件",
              set(tray._TRAY_CLICK_EVENTS) == need,
              f"缺 {sorted(need - set(tray._TRAY_CLICK_EVENTS))}")
        check("_TRAY_CLICK_EVENTS 不含鼠标移动 / 滚轮 / 悬停",
              not ({0x0200, WM_MOUSELEAVE, WM_MOUSEWHEEL, 0x020E}
                   & set(tray._TRAY_CLICK_EVENTS)))

        # ---- 6. 吃掉的仍然只是 WM_TRAYICON，别的消息照常透传 -------
        got: list = []

        def extra(msg, wp, lp):
            got.append(msg)
            return True, 0
        t2 = tray.TrayIcon(lambda anchor=None: None, lambda cmd: None,
                           extra_handler=extra)
        handled, _ = t2._on_message(WM_NCHITTEST, 0, 0)
        check("非托盘消息透传给 _extra（没被吞）",
              handled and got == [WM_NCHITTEST], f"got={got}")

        c.reset()
        t2._on_message(WM_TRAYICON, 1, 0x0200)
        check("WM_MOUSEMOVE 走托盘分支时不会漏给 _extra",
              got == [WM_NCHITTEST], f"got={got}")

        # ---- 7. 重新注册必须带回调 ----------------------------------
        #
        # 用户报的第二个 bug：点「常驻任务栏」→ 重启资源管理器 → 图标回来了，
        # 但右键不出菜单、双击不出卡片、单击也没反应。
        #
        # 根因是 set_icon / set_tooltip 往**唯一那份** nid 的 uFlags 上写单字段：
        # _tick() 每秒先 set_icon（NIF_ICON）再 set_tooltip（NIF_TIP），稳态就剩
        # NIF_TIP；explorer 重启触发重新 NIM_ADD 时，uCallbackMessage 根本没被
        # 登记 —— 图标看着完全正常，就是永远收不到点击。
        calls: list[tuple[int, int, int, int]] = []
        real_sni = w32.shell32.Shell_NotifyIconW
        full = w32.NIF_ICON | w32.NIF_MESSAGE | w32.NIF_TIP

        def fake_sni(action, nid_ptr):
            nid = ctypes.cast(
                nid_ptr, ctypes.POINTER(w32.NOTIFYICONDATAW)
            ).contents
            calls.append((int(action), int(nid.uFlags),
                          int(nid.uCallbackMessage), int(nid.uID)))
            return 1

        w32.shell32.Shell_NotifyIconW = fake_sni
        try:
            calls.clear()
            t3 = make(c)
            t3._hwnd = 0x1234
            t3._hicon = 0x5678
            t3._tip = "init"
            t3._add(retries=1)
            check("首次 NIM_ADD 带完整 flags（ICON|MESSAGE|TIP）",
                  [f for a, f, _cb, _uid in calls if a == w32.NIM_ADD] == [full],
                  f"{[hex(f) for a, f, _c, _u in calls if a == w32.NIM_ADD]}")

            for i in range(3):          # 模拟 _tick 反复 MODIFY
                t3.set_icon(0x7000 + i)
                t3.set_tooltip(f"tip{i}")
            check("set_icon / set_tooltip 之后 nid.uFlags 仍是完整的",
                  int(t3._nid.uFlags) == full,
                  f"uFlags={hex(int(t3._nid.uFlags))}")
            check("MODIFY 在副本上声明本次字段，不污染原件",
                  sorted({f for a, f, _c, _u in calls if a == w32.NIM_MODIFY})
                  == sorted({w32.NIF_ICON, w32.NIF_TIP}),
                  f"{[hex(f) for a, f, _c, _u in calls if a == w32.NIM_MODIFY]}")

            calls.clear()               # explorer 重启：走真实 TaskbarCreated 分支
            t3._on_message(t3._taskbar_created, 0, 0)
            adds = [(f, cb) for a, f, cb, _u in calls if a == w32.NIM_ADD]
            check("★ 重注册（TaskbarCreated）仍然登记 uCallbackMessage",
                  bool(adds) and all(f & w32.NIF_MESSAGE for f, _cb in adds)
                  and all(cb == WM_TRAYICON for _f, cb in adds),
                  f"flags={[hex(f) for f, _c in adds]} "
                  f"cb={[hex(x) for _f, x in adds]}")
            check("重注册用的还是同一个 hWnd/uID（图标不会变成孤儿）",
                  all(uid == 1 for a, _f, _c, uid in calls if a == w32.NIM_ADD),
                  f"{[uid for a, _f, _c, uid in calls if a == w32.NIM_ADD]}")
        finally:
            w32.shell32.Shell_NotifyIconW = real_sni

        # ---- 8. 双击：显示卡片，而不是 toggle 两次 ------------------
        c.reset()
        t4 = make(c)
        feed(t4, 0x0202)                    # 第一次 WM_LBUTTONUP
        check("单击 → 开关面板（零延迟，不等双击判定）",
              c.cmds == [tray.CMD_TOGGLE_PANEL], f"cmds={c.cmds}")
        feed(t4, 0x0203)                    # WM_LBUTTONDBLCLK
        feed(t4, 0x0202)                    # 双击里的第二次抬起
        check("★ 双击 → 最终是「显示卡片」，不是又关掉（★ 用户报的 bug）",
              c.cmds == [tray.CMD_TOGGLE_PANEL, tray.CMD_SHOW_PANEL],
              f"cmds={c.cmds}")

        c.reset()
        t5 = make(c)
        feed(t5, 0x0202)
        t5._last_up -= 1.0                  # 假装过了一秒：是两次独立单击
        feed(t5, 0x0202)
        check("两次慢速单击 → 两次 toggle（面板照样能关）",
              c.cmds == [tray.CMD_TOGGLE_PANEL, tray.CMD_TOGGLE_PANEL],
              f"cmds={c.cmds}")

        c.reset()
        t6 = make(c)
        feed(t6, 0x0202)
        feed(t6, 0x0202)                    # 没收到 DBLCLK，但两次挨得很近
        check("没有 DBLCLK 但两次抬起挨得近 → 仍按双击处理（显示卡片）",
              c.cmds == [tray.CMD_TOGGLE_PANEL, tray.CMD_SHOW_PANEL],
              f"cmds={c.cmds}")
        check("CMD_SHOW_PANEL 与 CMD_TOGGLE_PANEL 不撞号",
              tray.CMD_SHOW_PANEL != tray.CMD_TOGGLE_PANEL)

    finally:
        ctxmenu.close_all = real_close

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
