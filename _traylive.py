"""真机走查：托盘图标的「右键弹菜单 / 双击出卡片」，含**重新注册之后**那一路。

为什么非要真机跑：这三个坑（v1.0.15 / v1.0.16 修的）本质都是「Windows 到底有没有
把点击回调打过来」「窗口过程有没有被堵住」。单元测试里把 ``Shell_NotifyIconW``
拦下来只能验「注册信息写对了没有」，验不了端到端 —— 而用户遇到的正是端到端不行。

四个用例：

  1. **双击出卡片**：两次真点击（``SendInput``）→ 详情面板要可见。
     双击的两次「抬起」以前各 toggle 一次，卡片弹出来又被收回去 = 「双击没反应」。
  2. **右键弹菜单**：真右键 → 自绘菜单窗口要出来，ESC 要关得掉。
  3. **重新注册之后右键还能弹**（★ 用户报的那个）：手动给托盘窗口发一条
     ``TaskbarCreated`` —— explorer 重启 / 点「常驻任务栏」走的都是这条重注册路径。
     以前重注册时带着「只有提示文字」的窄 flags，回调没登记进去，右键从此失灵。
  4. **重注册不许阻塞**（★ 同样用户报的那一段）：``TaskbarCreated`` 是在窗口过程里
     **同步**处理的，老代码在失败路径上 ``sleep(NIM_ADD_DELAY=2.0) × 2`` = **4 秒**，
     这几天托盘窗口收不到任何消息 —— 表现就是「点了常驻之后几秒钟右键没反应」。
     这一项量「从发出 TaskbarCreated 到菜单弹出来」的墙钟时间。
     （实测老代码 5.55 s，修完 < 1 s。见 ``_trayaddprobe.py``。）

⚠️ 合成消息（``SendMessageW``）**不能**用来喂托盘点击：那样系统不给前台权，自绘菜单
会被立刻收回去（KILLFOCUS），看起来像「菜单弹不出来」。必须用真输入。

⚠️ ESC 那一项的断言按「屏幕上还剩几个菜单窗口」判，**不要**盯某个具体 hwnd ——
菜单窗口可能在两帧之间被替换掉，盯 hwnd 会偶发假 FAIL（已实测踩到）。

用法：python _traylive.py [exe 关键字]
    默认关键字「能耗统计」（安装版）。长条/图标没在跑的时候会明确报「找不到实例」。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import user32  # noqa: E402

TRAY_CLASS = "PowerMonitorTrayWnd"
MENU_CLASS = "PowerMonitorCtxMenu"
PANEL_CLASS = "PowerMonitorPanelWnd"
WM_KEYDOWN = 0x0100
WM_CLOSE = 0x0010
VK_ESCAPE = 0x1B

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def skip(label: str, why: str) -> None:
    print(f"[SKIP] {label}  ({why})")


# ------------------------------------------------------------------ 找实例

class _PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def processes(keyword: str) -> list[tuple[int, str]]:
    """按 exe 名找进程。PyInstaller onefile 会有「引导器 + 子进程」两个同名进程，
    两个都算一个实例；这里返回全部，调用方不区分。"""
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    out: list[tuple[int, str]] = []
    entry = _PE32()
    entry.dwSize = ctypes.sizeof(_PE32)
    if k.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            if keyword.lower() in entry.szExeFile.lower():
                out.append((entry.th32ProcessID, entry.szExeFile))
            if not k.Process32NextW(snap, ctypes.byref(entry)):
                break
    k.CloseHandle(snap)
    return out


def _class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def find_window_by_class(pid_set: set[int], cls: str, deep: bool = False):
    """在指定进程的窗口里找某个类名的窗口。

    ``deep=False``（默认）只枚举**顶层**窗口 —— 托盘消息窗口是隐藏的顶层窗口。
    ``deep=True`` 改成顺着 ``Shell_TrayWnd`` 枚举**子**窗口：长条是任务栏的子窗口，
    顶层枚举一辈子也找不到它（以前这里就是这么把自己坑了 —— 明明长条好好地挂在
    任务栏上，探针却一路报「没出现」，看着像功能坏了）。
    """
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pid_set and _class_of(hwnd) == cls:
            found.append(hwnd)
            return False
        return True

    if deep:
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            user32.EnumChildWindows(tray, cb, 0)
    else:
        user32.EnumWindows(cb, 0)
    return found[0] if found else None


def windows_of_class(cls: str) -> list[int]:
    """全屏范围内某个类的顶层窗口（用来数「还开着几个菜单」）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if _class_of(hwnd) == cls:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


# ------------------------------------------------------------------ 真输入

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]


INPUT_MOUSE = 0
LDOWN, LUP, RDOWN, RUP = 0x0002, 0x0004, 0x0008, 0x0010


def _send(flag: int) -> None:
    inp = _INPUT(type=INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, flag, 0, None))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def click(x: int, y: int, right: bool = False, count: int = 1) -> None:
    """真的把光标挪过去按一下。

    ``SendInput`` 的 dx/dy 给 0：光标位置交给 SetCursorPos，这里只发按键。
    双击的两次点击必须落在系统双击间隔内，否则 Windows 不会给出双击消息。
    """
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    down, up = (RDOWN, RUP) if right else (LDOWN, LUP)
    for i in range(count):
        _send(down)
        time.sleep(0.03)
        _send(up)
        if i + 1 < count:
            time.sleep(0.05)


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def wait_window(cls: str, timeout: float = 2.5):
    """等某个窗口出现（自绘菜单 / 面板都是弹出即建窗）。"""
    end = time.time() + timeout
    while time.time() < end:
        pump(0.05)
        hwnd = user32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    return None


def close_menus_by_escape(rounds: int = 3) -> int:
    """给所有开着的菜单发 ESC，返回最终还剩几个。

    🔴 断言用「还剩几个窗口」而不是「某个 hwnd 还在不在」：菜单窗口可能在两帧
    之间被替换，盯 hwnd 会偶发假 FAIL（实测踩到过一次，10 次重放全绿）。
    """
    left = windows_of_class(MENU_CLASS)
    for _ in range(rounds):
        if not left:
            return 0
        for hwnd in left:
            user32.SendMessageW(hwnd, WM_KEYDOWN, VK_ESCAPE, 0)
        pump(0.35)
        left = windows_of_class(MENU_CLASS)
    return len(left)


def close_menus_now() -> None:
    """不走键盘，直接 WM_CLOSE 清场（用例之间用，别影响被测行为）。"""
    for hwnd in windows_of_class(MENU_CLASS):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    pump(0.4)


# ------------------------------------------------------------------ 图标矩形

class _NII(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]


def icon_rect(hwnd, uid: int = 1):
    """``Shell_NotifyIconGetRect`` —— 直接问 Shell「这个图标在哪」。

    比在任务栏里翻 ToolbarWindow32 稳得多（还要跨进程读内存取文字）。
    图标折叠在 ^ 里且没展开时拿不到，会返回 None。
    """
    nii = _NII(cbSize=ctypes.sizeof(_NII), hWnd=hwnd, uID=uid)
    rect = wintypes.RECT()
    try:
        hr = ctypes.windll.shell32.Shell_NotifyIconGetRect(
            ctypes.byref(nii), ctypes.byref(rect))
    except (AttributeError, OSError):
        return None
    if hr != 0 or rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "能耗统计"
    enable_dpi_awareness()

    procs = processes(keyword)
    if not procs:
        print(f"找不到在跑的实例（exe 名含「{keyword}」）—— 先把程序跑起来再走查")
        return 1
    pids = {p for p, _n in procs}
    print(f"实例：{[f'{pid}:{name}' for pid, name in procs]}")

    tray = find_window_by_class(pids, TRAY_CLASS)
    check("找到托盘消息窗口（隐藏窗口，类名 PowerMonitorTrayWnd）", bool(tray),
          f"hwnd={tray}")
    if not tray:
        return 1

    rect = icon_rect(tray)
    check("Shell 报得出图标位置（图标被折叠在 ^ 里且没展开时拿不到）",
          rect is not None, f"rect={rect}")
    if rect is None:
        skip("托盘点击走查", "拿不到图标矩形 —— 把图标设成「常驻任务栏」再跑")
        print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
        return 0
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    print(f"图标矩形 {rect} → 点 ({cx},{cy})")

    # 🔴 冷启动前十几秒点它是没用的：程序还在采第一帧、建长条、暖毛玻璃缓存。
    #    实测刚启动 12 秒时第一下右键会失灵（图标在、位置也对，就是没反应），
    #    跑几个用例之后就 4/4 全稳。所以先等它进稳态再开始，否则会误判成 bug。
    #
    #    长条是任务栏的**子窗口**，枚举要 deep=True 才找得到 —— 以前这里走顶层枚举，
    #    于是「长条明明挂在任务栏上」却一路打印「没出现」，只是恰好也把 10 秒等满了
    #    才没暴露成假故障。现在是「找到就早退，但稳态时间一定等满」。
    deadline = time.time() + 12.0
    strip = find_window_by_class(pids, "PowerMonitorTaskbarStrip", deep=True)
    while strip is None and time.time() < deadline:
        pump(0.5)
        strip = find_window_by_class(pids, "PowerMonitorTaskbarStrip", deep=True)
    pump(max(0.0, deadline - time.time()))
    check("长条挂在任务栏上（任务栏子窗口，不是顶层窗口）", strip is not None,
          f"hwnd={strip} 已等满 12.0s")

    # 清场：上一轮留下的菜单 / 面板会把断言顶成恒真
    close_menus_now()
    panel = user32.FindWindowW(PANEL_CLASS, None)
    if panel and user32.IsWindowVisible(panel):
        user32.PostMessageW(panel, WM_CLOSE, 0, 0)
        pump(0.4)

    # ---- 1. 双击出卡片 ----
    click(cx, cy, count=2)
    panel = wait_window(PANEL_CLASS, 2.5)
    visible = bool(panel and user32.IsWindowVisible(panel))
    check("★ 双击托盘图标 → 详情面板显示出来（以前是弹一下就收回去）",
          visible, f"panel={panel} visible={visible}")
    if visible:
        user32.PostMessageW(panel, WM_CLOSE, 0, 0)
        pump(0.5)

    # ---- 2. 右键弹菜单 ----
    user32.SetCursorPos(cx, cy - 200)      # 光标先移开，免得残留 hover
    pump(0.2)
    click(cx, cy, right=True)
    menu = wait_window(MENU_CLASS, 2.5)
    check("★ 右键托盘图标 → 自绘菜单弹出来", bool(menu), f"menu={menu}")
    if menu:
        left = close_menus_by_escape()
        check("ESC 关掉菜单（顺带确认它是真窗口、吃键盘）", left == 0,
              f"ESC 之后还剩 {left} 个菜单窗口")

    # ---- 3. 重新注册之后再右键（用户报的那个场景）----
    # 「常驻任务栏」和 explorer 重启都会走这条：Shell 让所有图标重新 ADD。
    taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
    check("拿得到 TaskbarCreated 消息号", bool(taskbar_created),
          f"0x{taskbar_created:X}")

    close_menus_now()

    # 🔴 这里**故意不先 pump**：重注册这段路必须是不阻塞的。
    #    TaskbarCreated 在窗口过程里**同步**处理；而「对已存在图标重复 NIM_ADD
    #    必然失败」（_trayaddprobe.py 实测 err=E_FAIL、0.6 ms），老代码就会走
    #    失败路径 sleep(NIM_ADD_DELAY=2.0) × 2 = 4 秒 —— 那 4 秒托盘窗口收不到
    #    任何消息，右键当然没反应。所以第一次右键就在「刚发出去」立刻打，
    #    并要求 2.5 秒内出菜单（修好后实测 < 1 s，老代码 5.55 s）。
    t_post = time.monotonic()
    user32.PostMessageW(tray, taskbar_created, 0, 0)

    rect2 = icon_rect(tray)
    check("重新注册之后图标还在（位置照旧）", rect2 is not None, f"rect={rect2}")
    cx2, cy2 = ((rect2[0] + rect2[2]) // 2, (rect2[1] + rect2[3]) // 2) \
        if rect2 else (cx, cy)
    user32.SetCursorPos(cx2, cy2 - 200)
    pump(0.15)

    menu2 = None
    attempts = 0
    while attempts < 3 and menu2 is None:
        attempts += 1
        click(cx2, cy2, right=True)
        menu2 = wait_window(MENU_CLASS, 2.0)
        if menu2 is None:
            pump(0.3)
    elapsed = time.monotonic() - t_post

    check("★ 重新注册之后右键**仍然**弹得出菜单"
          "（以前这一步之后回调就没了，右键从此失灵）",
          menu2 is not None and bool(user32.IsWindow(menu2)),
          f"menu={menu2} 第 {attempts} 次点击 / {elapsed:.2f}s")
    check("★ 重注册没有把托盘阻塞住"
          "（老代码在这里睡 4 秒，右键要等 2 秒以上才恢复）",
          menu2 is not None and elapsed <= 2.5,
          f"从收到 TaskbarCreated 到菜单弹出 {elapsed:.2f}s")
    left2 = close_menus_by_escape()
    check("重注册之后菜单照样关得掉（回调链完整：弹出 + 键盘都走通了）",
          left2 == 0, f"ESC 之后还剩 {left2} 个菜单窗口")

    panel2 = user32.FindWindowW(PANEL_CLASS, None)
    if panel2 and user32.IsWindowVisible(panel2):
        user32.PostMessageW(panel2, WM_CLOSE, 0, 0)
    user32.SetCursorPos(rect[0] - 300, cy)     # 光标还回去，别停在图标上
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
