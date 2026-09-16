"""系统托盘图标（Shell_NotifyIcon）。

自建一个不可见的消息窗口承载托盘回调。菜单内容由外部 ``build_menu`` 提供，
本模块只负责弹出、分发命令、以及维护图标与提示文本。

注意：所有操作都必须在创建窗口的那个线程（主线程）里做。
"""

from __future__ import annotations

import ctypes
import time

from . import debug
from .w32 import (
    MF_CHECKED,
    MF_GRAYED,
    MF_POPUP,
    MF_SEPARATOR,
    MF_STRING,
    NIF_ICON,
    NIF_MESSAGE,
    NIF_TIP,
    NIM_ADD,
    NIM_DELETE,
    NIM_MODIFY,
    NOTIFYICONDATAW,
    TPM_RIGHTBUTTON,
    TPM_RETURNCMD,
    WM_LBUTTONUP,
    WM_RBUTTONUP,
    WM_TRAY_READDED,
    WM_TRAYICON,
    WNDCLASSEXW,
    WNDPROC,
    kernel32,
    shell32,
    user32,
    wintypes,
)

# 菜单命令 ID
CMD_TOGGLE_PANEL = 1
CMD_MODE_CURRENT = 10
CMD_MODE_COST = 11
CMD_MODE_SESSION = 12
CMD_MODE_AVERAGE = 13
CMD_TOGGLE_AUTOSTART = 20
CMD_PIN_TASKBAR = 21
CMD_FEE_SETTINGS = 22
CMD_TOGGLE_STRIP = 23
CMD_STATS = 24
CMD_RESET = 30
CMD_OPEN_CONFIG = 31
CMD_ABOUT = 32
CMD_QUIT = 40

# 长条外观 / 内容（托盘菜单的二级子菜单，见 app._build_menu）。
# 每档占一个「区间」，命令号 = 基址 + 该档在 stripopts 目录里的下标；
# 区间留得比现有档位数宽，以后加档位不用动老命令号。
CMD_THEME_BASE = 50      # 50..59  长条质感（strippts.THEMES 下标）
CMD_FONT_BASE = 60       # 60..69  长条字号（strippts.FONT_SCALES 下标）
CMD_SIZE_BASE = 70       # 70..79  长条大小（strippts.SIZES 下标）
CMD_FIELD_BASE = 80      # 80..99  长条显示内容（strippts.FIELDS 下标，勾选式）

# 长条位置：锁定开关 + 位置复位。都不是「档位」，所以各给一个单独的号。
CMD_TOGGLE_LOCK = 25
CMD_POS_RESET = 26
# 长条字号「复位标准」：拖边缘能把字号调成任意连续值（0.60~1.60），菜单里
# 六档只是快捷取值，所以给一个一键回 1.00 的单独号（同样不是档位区间）。
CMD_FONT_RESET = 27

# 「查看某一天 / 某一次开机」：菜单里直接列出最近若干条，点一条就跳到它的明细。
# 用户的原话是「能不能让用户在菜单里可以自主选择某一天去看……你这样对用户来说
# 还是一个黑箱」—— 所以不给「打开统计窗口自己找」，而是把最近这些天/这些次
# 直接摆到菜单上。命令号 = 基址 + 该条在 recent 列表里的下标。
CMD_VIEW_DAY_BASE = 100      # 100..119  最近 20 天
CMD_VIEW_SESSION_BASE = 120  # 120..139  最近 20 次开机
MENU_RECENT = 10             # 菜单里最多列几条（再多就该进窗口里翻了）

# 「选项类」子菜单点完继续弹的上限。正常用不到这么多，纯粹防呆：
# 万一 on_command 的返回值出问题，也不会变成死循环把程序卡死。
MAX_POPUP_ROUNDS = 40

_CLASS_NAME = "PowerMonitorTrayWnd"
_windows: dict[int, "TrayIcon"] = {}
_wndproc_ref: WNDPROC | None = None  # 必须持引用，否则回调被 GC 掉会崩

# NIM_ADD 在登录 / 开机 / explorer 刚重启时会瞬时失败（典型是
# ERROR_TIMEOUT 1460：任务栏还没准备好应答，超时约 4 秒）。
# 失败一次就判定「系统没有通知区域」会让程序误降级成普通窗口，
# 开机自启场景下尤其明显，所以必须重试。
NIM_ADD_ATTEMPTS = 4
NIM_ADD_DELAY = 2.0


def _dbg(msg: str) -> None:
    debug.log("tray", msg)


@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    tray = _windows.get(hwnd)
    if tray is not None:
        try:
            handled, result = tray._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里绝不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_class(hinstance) -> bool:
    global _wndproc_ref
    _wndproc_ref = _wnd_proc

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _wnd_proc
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinstance
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = None
    wc.lpszMenuName = None
    wc.lpszClassName = _CLASS_NAME
    wc.hIconSm = None

    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if atom:
        return True
    # 已经注册过（ERROR_CLASS_ALREADY_EXISTS = 1410）也算成功
    return ctypes.get_last_error() == 1410


class TrayIcon:
    """托盘图标 + 弹出菜单。"""

    def __init__(self, build_menu, on_command, extra_handler=None) -> None:
        """
        build_menu() -> HMENU   每次弹出前重建菜单（内容是动态的）
        on_command(cmd_id)      处理菜单命令
        extra_handler(msg, wparam, lparam) -> (handled, result)
                                应用自己的消息（定时器、内部通知）走这里
        """
        self._build_menu = build_menu
        self._on_command = on_command
        self._extra = extra_handler
        self._hwnd = None
        self._hicon = None
        self._added = False
        self._nid = NOTIFYICONDATAW()
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # ------------------------------------------------------------- 生命周期

    def create(self, hicon, tooltip: str = "") -> bool:
        hinstance = kernel32.GetModuleHandleW(None)
        if not _ensure_class(hinstance):
            return False

        self._hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, "PowerMonitor", 0,
            0, 0, 0, 0, None, None, hinstance, None,
        )
        if not self._hwnd:
            return False

        _windows[self._hwnd] = self

        self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        self._nid.uCallbackMessage = WM_TRAYICON
        self._nid.hIcon = hicon
        self._nid.szTip = tooltip[:127]
        self._hicon = hicon

        return self._add(retries=NIM_ADD_ATTEMPTS)

    def _add(self, retries: int = 1) -> bool:
        """注册托盘图标。

        ``Shell_NotifyIcon`` 是「一问一答」的跨进程调用：任务栏没及时答复
        就返回失败，但这不是永久性故障。失败一次就放弃会让程序误判成
        「系统没有通知区域」而降级，所以按次重试。
        """
        for attempt in range(1, retries + 1):
            ctypes.set_last_error(0)
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
                self._added = True
                _dbg(f"注册托盘成功（第 {attempt}/{retries} 次尝试）")
                return True
            err = ctypes.get_last_error()
            _dbg(f"注册托盘失败 err={err}（第 {attempt}/{retries} 次尝试）")
            if attempt < retries:
                time.sleep(NIM_ADD_DELAY)
        self._added = False
        return False

    def ensure_added(self) -> bool:
        """补偿入口：还没注册成功时再试一次，由定时器周期性调用。"""
        if self._added:
            return True
        if not self._hwnd:
            return False
        return self._add(retries=1)

    @property
    def added(self) -> bool:
        return self._added

    def destroy(self) -> None:
        if self._hwnd:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            _windows.pop(self._hwnd, None)
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._hicon:
            user32.DestroyIcon(self._hicon)
            self._hicon = None

    @property
    def hwnd(self):
        return self._hwnd

    # ------------------------------------------------------------- 更新

    def set_icon(self, hicon) -> None:
        if not self._hwnd or not hicon:
            return
        previous = self._hicon
        self._nid.uFlags = NIF_ICON
        self._nid.hIcon = hicon
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        self._hicon = hicon
        if previous:
            user32.DestroyIcon(previous)

    def set_tooltip(self, text: str) -> None:
        if not self._hwnd:
            return
        self._nid.uFlags = NIF_TIP
        self._nid.szTip = text[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    # ------------------------------------------------------------- 消息

    def popup(self) -> None:
        """在当前光标处弹出菜单。

        长条默认不再穿透点击（要能拖），于是会把任务栏那一片的右键菜单吃掉 ——
        所以长条收到右键时回调到这里，把菜单补上。位置由 ``_popup`` 自己取
        ``GetCursorPos``，所以不用传坐标。
        """
        self._popup()

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = lparam & 0xFFFF
            # 托盘图标的点击**不产生窗口焦点事件**，开着的菜单收不到
            # WM_KILLFOCUS —— 所以任何一次托盘点击都先把旧菜单关掉。
            from . import ctxmenu
            ctxmenu.close_all()
            if event == WM_LBUTTONUP:
                self._on_command(CMD_TOGGLE_PANEL)
            elif event == WM_RBUTTONUP:
                self._popup()
            return True, 0

        if msg == self._taskbar_created and self._taskbar_created:
            # 资源管理器重启过，托盘被清空，必须重新注册
            _dbg("收到 TaskbarCreated，重新注册托盘")
            self._added = False
            ok = self._add(retries=3)
            if self._extra is not None:
                self._extra(WM_TRAY_READDED, 1 if ok else 0, 0)
            return True, 0

        if self._extra is not None:
            return self._extra(msg, wparam, lparam)

        return False, 0

    def _popup(self, anchor: str | None = None, origin=None) -> None:
        """弹出自绘菜单（ctxmenu 的深色卡片）。

        早先用 ``TrackPopupMenu`` —— 灰色经典样式改不了色，用户原话「右键菜单栏
        也有点丑」。现在渲染交给 ctxmenu，这里只剩两件事：

        * 把 ``build_menu(anchor)`` 建出来的结构（MenuBuilder.entries）递过去；
        * 「点完不关」：on_command 返回锚点时，在**同一位置**重弹那一级
          （原生 TrackPopupMenu 时代就是这样，行为不变）。

        位置固定用第一次弹出的坐标：菜单才不会点一下跳一下。
        """
        from . import ctxmenu
        menu = self._build_menu(anchor)
        if not menu:
            return
        entries = MenuBuilder.entries_of(menu)
        # 自绘菜单只吃 entries，原生 HMENU 没用了（它只是为旧断言顺带建的）
        user32.DestroyMenu(menu)
        MenuBuilder.discard(menu)
        if not entries:
            return
        if origin is None:
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            origin = (point.x, point.y)

        def _dispatch(command: int) -> None:
            nxt = self._on_command(int(command))
            if nxt:
                # 「点完不关」的命令：在同一位置重弹它所属的那一级
                self._popup(nxt, origin)

        ctxmenu.show(entries, origin[0], origin[1], _dispatch)


# --------------------------------------------------------------------- 菜单构建


class MenuBuilder:
    """小助手：把菜单项拼装得更可读。

    除了拼出原生 HMENU（自检里要用 ``GetMenuItemCount`` 这类 API 做断言），
    同时把结构记进 ``entries`` —— **自绘菜单（ctxmenu）消费的是这份结构**，
    原生菜单只是顺带的、兼容旧断言的产物。attach 的子菜单结构直接嵌进
    ``entries``，所以弹出期间子菜单内容不需要再回去问 app。
    """

    _registry: dict[int, "MenuBuilder"] = {}

    def __init__(self) -> None:
        self.handle = user32.CreatePopupMenu()
        self.entries: list[dict] = []
        self._submenus: list["MenuBuilder"] = []
        MenuBuilder._registry[self.handle] = self

    @classmethod
    def entries_of(cls, handle) -> list[dict] | None:
        builder = cls._registry.get(int(handle))
        return builder.entries if builder is not None else None

    @classmethod
    def discard(cls, handle) -> None:
        """原生 HMENU 销毁后顺手反注册（含级联的子菜单），别让注册表一直涨。"""
        builder = cls._registry.pop(int(handle), None)
        if builder is None:
            return
        for sub in builder._submenus:
            cls.discard(sub.handle)

    def label(self, text: str, value: str = "") -> "MenuBuilder":
        """只读信息行（灰色不可点）。``value`` 画在右侧（如「今日  1.24 kWh」）。"""
        text_ = f"{text}\t{value}" if value else text
        user32.AppendMenuW(self.handle, MF_STRING | MF_GRAYED, 0, text_)
        self.entries.append({"type": "label", "text": text, "value": value})
        return self

    def item(self, cmd: int, text: str, checked: bool = False,
             enabled: bool = True, radio: bool = False) -> "MenuBuilder":
        flags = MF_STRING | (MF_CHECKED if checked else 0)
        if not enabled:
            flags |= MF_GRAYED
        user32.AppendMenuW(self.handle, flags, cmd, text)
        self.entries.append({
            "type": "item", "cmd": int(cmd), "text": text,
            "checked": bool(checked), "enabled": bool(enabled),
            "radio": bool(radio),
        })
        return self

    def separator(self) -> "MenuBuilder":
        user32.AppendMenuW(self.handle, MF_SEPARATOR, 0, None)
        self.entries.append({"type": "sep"})
        return self

    def attach(self, text: str, submenu: "MenuBuilder") -> "MenuBuilder":
        user32.AppendMenuW(self.handle, MF_POPUP, submenu.handle, text)
        self._submenus.append(submenu)
        self.entries.append({"type": "sub", "text": text,
                             "entries": submenu.entries})
        return self

    @staticmethod
    def submenu() -> "MenuBuilder":
        return MenuBuilder()
