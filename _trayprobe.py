"""诊断：托盘图标右键到底有没有弹出菜单。

用官方 API ``Shell_NotifyIconGetRect`` 拿到图标的真实屏幕矩形（比扫任务栏可靠），
再用 **SendInput 真实右键**（合成 SendMessageW 不给前台权，测出来是假阴性），
然后查 ``PowerMonitorCtxMenu`` 窗口有没有出现。

同时对照测一次长条右键，把「菜单模块坏了」和「托盘入口坏了」分开。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path
from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.w32 import (  # noqa: E402
    WM_TRAYICON,
    gdi32,
    shell32,
    user32,
)

ctypes.windll.shcore.SetProcessDpiAwareness(1)

# 从 WMI 启动时没有控制台，把输出落盘再看。
# （为什么要用 WMI 启动它：从 agent 沙箱起的进程与 WMI 起的 app 处于不同的完整性
#   级别，SendInput 注入的点击到不了 app —— 表现为「点了没反应」的假象。）
if "--log" in sys.argv:
    _log = open(Path(__file__).resolve().parent / "_trayprobe.out", "w",
                encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log

WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205

TRAY_CLASS = "PowerMonitorTrayWnd"
STRIP_CLASS = "PowerMonitorTaskbarStrip"
MENU_CLASS = "PowerMonitorCtxMenu"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", GUID)]


shell32.Shell_NotifyIconGetRect.argtypes = [
    ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long


def class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def find_all(cls: str) -> list:
    hits = []
    enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum
    def cb(h, lp):
        if class_of(h) == cls:
            hits.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return hits


def find_child(cls: str):
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    hits = []
    enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum
    def cb(h, lp):
        if class_of(h) == cls:
            hits.append(h)
        return True
    user32.EnumChildWindows(tray, cb, 0)
    return hits[0] if hits else None


def visible_menus() -> list:
    out = []
    for h in find_all(MENU_CLASS):
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        out.append((h, user32.IsWindowVisible(h),
                    (r.left, r.top, r.right, r.bottom)))
    return out


def real_right_click(x: int, y: int) -> None:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.35)
    for flags in (0x0008, 0x0010):     # RIGHTDOWN / RIGHTUP
        inp = INPUT(0, MOUSEINPUT(0, 0, 0, flags, 0, 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.06)


def dismiss() -> None:
    user32.keybd_event(0x1B, 0, 0, 0)
    user32.keybd_event(0x1B, 0, 2, 0)
    time.sleep(0.5)


def log_lines() -> list[str]:
    p = Path(__file__).resolve().parent / "debug.log"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def synth_tray(hwnd, event: int) -> None:
    res = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(hwnd, WM_TRAYICON, 1, event, 0x0002, 2000,
                               ctypes.byref(res))


def synth_probe(hwnds) -> None:
    """往运行中的程序里灌事件，再用它自己的日志判断行为。

    合成消息拿不到前台权（菜单会被 KILLFOCUS 秒关），所以**不能**用「菜单窗口
    在不在」当判据；但「某类事件有没有被当成托盘点击」是纯分流的逻辑，
    看日志新增了什么就一清二楚 —— 这正是用户那个 bug 的分界线。
    """
    h = hwnds[0]
    base = log_lines()
    for _ in range(3):
        synth_tray(h, 0x0200)          # WM_MOUSEMOVE
    time.sleep(0.6)
    after = log_lines()
    new = [x for x in after[len(base):] if "[tray]" in x or "[ctxmenu]" in x]
    verdict = "✓ 修复生效" if not new else "✗ 仍然被当成托盘点击"
    print(f"  发 3 条 WM_MOUSEMOVE → 相关日志新增 {len(new)} 条：{verdict}")
    for x in new:
        print(f"      {x}")

    base = after
    synth_tray(h, 0x0205)              # WM_RBUTTONUP
    time.sleep(0.9)
    after = log_lines()
    new = [x for x in after[len(base):] if "[tray]" in x or "[ctxmenu]" in x]
    print(f"  发 1 条 WM_RBUTTONUP → 相关日志新增 {len(new)} 条"
          f"（期望：托盘点击 + ctxmenu 弹出）")
    for x in new:
        print(f"      {x}")


def main() -> int:
    tray_hwnds = find_all(TRAY_CLASS)
    print(f"托盘窗口  {len(tray_hwnds)} 个: {[hex(h) for h in tray_hwnds]}")
    if not tray_hwnds:
        print("没有在跑的程序（托盘窗口不存在）")
        return 1

    # ---- 0. 主线程还活着吗？----
    # 如果消息循环卡在某个同步调用里（比如绘制时又触发重绘），窗口还在屏幕上、
    # GetRect 也能work，但**任何消息都不会被处理** —— 表现就是「右键没反应」。
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    for h in tray_hwnds:
        print(f"  托盘窗口 {hex(h)} hung={bool(user32.IsHungAppWindow(h))}")
    strip0 = find_child(STRIP_CLASS)
    if strip0:
        res = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(strip0, 0, 0, 0, 0x0002, 2000,
                                        ctypes.byref(res))
        print(f"  长条 {hex(strip0)} hung={bool(user32.IsHungAppWindow(strip0))} "
              f"SendMessageTimeout(WM_NULL)={'应答' if ok else '超时→线程卡死'}")


    # ---- 1. 托盘图标真实矩形 ----
    box = None
    for h in tray_hwnds:
        nid = NOTIFYICONIDENTIFIER()
        nid.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        nid.hWnd = h
        nid.uID = 1
        r = wintypes.RECT()
        hr = shell32.Shell_NotifyIconGetRect(ctypes.byref(nid), ctypes.byref(r))
        print(f"  hwnd={hex(h)} GetRect hr=0x{hr & 0xFFFFFFFF:08X} "
              f"rect=({r.left},{r.top},{r.right},{r.bottom})")
        if hr == 0:
            box = (r.left, r.top, r.right, r.bottom)

    # ---- 2. 托盘右键 ----
    if box:
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        print(f"\n>>> 对托盘图标 ({cx},{cy}) 真实右键（之前菜单数={len(visible_menus())}）")
        real_right_click(cx, cy)
        time.sleep(1.0)
        ms = visible_menus()
        print(f"    右键后菜单窗口 {len(ms)} 个:")
        for h, vis, rect in ms:
            print(f"      {hex(h)} visible={vis} rect={rect}")
        # 再等一下看是不是被 KILLFOCUS 秒关了
        time.sleep(1.5)
        ms2 = visible_menus()
        print(f"    1.5s 后仍然在 {len(ms2)} 个")
        dismiss()
    else:
        print("\n拿不到托盘图标矩形（Shell_NotifyIconGetRect 失败）")

    # ---- 3. 对照：长条右键 ----
    strip = find_child(STRIP_CLASS)
    print(f"\n长条窗口 = {hex(strip) if strip else None}")
    if strip:
        r = wintypes.RECT()
        user32.GetWindowRect(strip, ctypes.byref(r))
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        print(f">>> 对长条 ({cx},{cy}) 真实右键")
        real_right_click(cx, cy)
        time.sleep(1.0)
        ms = visible_menus()
        print(f"    右键后菜单窗口 {len(ms)} 个:")
        for h, vis, rect in ms:
            print(f"      {hex(h)} visible={vis} rect={rect}")
        dismiss()

    # ---- 4. 合成事件序列（判据是程序自己的日志）----
    print("\n== 合成事件序列 ==")
    synth_probe(tray_hwnds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
