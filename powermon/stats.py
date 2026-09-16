"""用量统计窗口 —— 原生 Tab 控件 + ListView 明细表。

**为什么不塞进详情面板**：面板是纯 GDI 自绘的固定版面，一张几百行、能滚动的
表格要自己实现滚动条、鼠标滚轮、列宽拖拽、键盘翻页、选中高亮 —— 全是系统
控件白送的东西。这里就一个窗口 + 一个 Tab + 一个 ListView，代码量比自绘少一半，
而交互比自绘好。

**五个维度**（都是用户点名的）：

===========  ==========================================================
按天          每天一行（直接从每日账本出）
按月          各月汇总（按日期前 7 位归组）
按年          各年汇总（按日期前 4 位归组）
按每次开机    每次开机一条，键是**开机时刻** —— 同一次开机内重开程序接在
              同一条上，重启后才是新的一条。用户要的是「每次」而不是「当前次」。
按时段        0~23 点各累计多少 Wh（跨天累加），找「几点最费电」
===========  ==========================================================

数据全部来自 ``meter.EnergyMeter`` 的账本，本窗口只负责展示 —— 所以关掉窗口
不影响统计，统计也不会因为没开窗口而断。
"""

from __future__ import annotations

import ctypes
import time

from . import debug
from .meter import _duration as _short_time
from .w32 import (
    BN_CLICKED,
    BS_PUSHBUTTON,
    CB_ADDSTRING,
    CB_GETCURSEL,
    CB_RESETCONTENT,
    CB_SETCURSEL,
    CBN_SELCHANGE,
    CBS_DROPDOWNLIST,
    CLEARTYPE_QUALITY,
    COLOR_BTNFACE,
    DEFAULT_CHARSET,
    ES_AUTOVSCROLL,
    ES_MULTILINE,
    ES_READONLY,
    FW_BOLD,
    FW_NORMAL,
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    IDCANCEL,
    SIZE,
    SM_CXSCREEN,
    SM_CYSCREEN,
    SS_LEFT,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    TRANSPARENT,
    WM_CLOSE,
    WM_COMMAND,
    WM_CTLCOLOREDIT,
    WM_CTLCOLORSTATIC,
    WM_DESTROY,
    WM_NOTIFY,
    WM_SETFONT,
    WM_SETREDRAW,
    WM_SIZE,
    WNDCLASSEXW,
    WNDPROC,
    WS_CAPTION,
    WS_CHILD,
    WS_CLIPCHILDREN,
    WS_EX_CLIENTEDGE,
    WS_OVERLAPPED,
    WS_SYSMENU,
    WS_TABSTOP,
    WS_THICKFRAME,
    WS_VISIBLE,
    WS_VSCROLL,
    gdi32,
    kernel32,
    user32,
    wintypes,
)

# 底部两条文字的分组分隔符：既是视觉间隔，也是「截断时按什么切开」的标记。
_SEP = "     "

_CLASS_NAME = "PowerMonitorStatsWnd"
_FONT_FACE = "Microsoft YaHei UI"
_LOGPIXELSX = 88

# 控件 ID
IDC_TAB = 200
IDC_LIST = 201
IDC_SUMMARY = 202
IDC_REFRESH = 203
IDC_HINT = 204
# 「自己挑一条看」的选择器：下拉 + 前后翻，配一个只读明细框
IDC_PICK = 205
IDC_PREV = 206
IDC_NEXT = 207
IDC_DETAIL = 208
IDC_PICKLABEL = 209

# --------------------------------------------------------------------- 常量
# 这些不是 w32 里的公共常量（只有本窗口用），就近定义，避免把 w32 撑成杂物间。

TAB_CLASS = "SysTabControl32"
LIST_CLASS = "SysListView32"

TCM_FIRST = 0x1300
TCM_INSERTITEMW = TCM_FIRST + 62
TCM_GETITEMW = TCM_FIRST + 60
TCM_GETCURSEL = TCM_FIRST + 11
TCM_SETCURSEL = TCM_FIRST + 12
TCIF_TEXT = 0x0001

TCN_FIRST = -550
TCN_SELCHANGE = TCN_FIRST - 1

LVM_FIRST = 0x1000
LVM_INSERTCOLUMNW = LVM_FIRST + 97
LVM_SETCOLUMNWIDTH = LVM_FIRST + 30
LVM_INSERTITEMW = LVM_FIRST + 77
LVM_SETITEMTEXTW = LVM_FIRST + 116
LVM_DELETEALLITEMS = LVM_FIRST + 9
LVM_SETEXTENDEDLISTVIEWSTYLE = LVM_FIRST + 54
LVM_GETITEMCOUNT = LVM_FIRST + 4
# 读回用：自检要拿它验「设进去的文字真的进了控件」，而不是设了个野指针
LVM_GETITEMTEXTW = LVM_FIRST + 115
# 选中同步：点表格某一行 → 明细跟着换
LVM_GETNEXTITEM = LVM_FIRST + 12
LVM_SETITEMSTATE = LVM_FIRST + 43
LVM_ENSUREVISIBLE = LVM_FIRST + 19
LVNI_SELECTED = 0x0002
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001
LVIS_STATEIMAGEMASK = 0xF000

LVN_FIRST = -100
LVN_ITEMCHANGED = LVN_FIRST - 1

LVS_REPORT = 0x0001
LVS_SINGLESEL = 0x0004
LVS_SHOWSELALWAYS = 0x0008
LVS_EX_FULLROWSELECT = 0x00000020
LVS_EX_GRIDLINES = 0x00000001
LVS_EX_DOUBLEBUFFER = 0x00010000

LVCF_FMT = 0x0001
LVCF_WIDTH = 0x0002
LVCF_TEXT = 0x0004
LVCF_SUBITEM = 0x0008
LVCFMT_LEFT = 0x0000
LVCFMT_RIGHT = 0x0001

LVIF_TEXT = 0x0001
LVIF_STATE = 0x0008

# 明细最多画多少行。400 天 / 240 次开机本来就都在这个数以内，
# 留个上限只是防止将来某天账本被手工填成一万行。
MAX_ROWS = 500


class TCITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("pszText", ctypes.c_wchar_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_void_p),
    ]


class LVCOLUMNW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("fmt", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("pszText", ctypes.c_wchar_p),
        ("cchTextMax", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("iOrder", ctypes.c_int),
        ("cxMin", ctypes.c_int),
        ("cxDefault", ctypes.c_int),
        ("cxIdeal", ctypes.c_int),
    ]


class LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", ctypes.c_wchar_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_void_p),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", ctypes.c_uint),
        ("puColumns", ctypes.c_void_p),
    ]


class NMHDR(ctypes.Structure):
    _fields_ = [
        ("hwndFrom", wintypes.HWND),
        ("idFrom", ctypes.c_size_t),
        ("code", ctypes.c_int),
    ]


# Tab 顺序 = 菜单里的顺序。key 直接透给 meter.stats_rows()。
TABS = (
    ("day", "按天"),
    ("month", "按月"),
    ("year", "按年"),
    ("session", "按每次开机"),
    ("hour", "按时段"),
)

# ListView 的列：(标题, 宽度基准 px, 是否右对齐)
COLUMNS = (
    ("时间", 186, False),
    ("电量", 104, True),
    ("电费", 96, True),
    ("运行时长", 104, True),
    ("说明", 178, False),
)

_windows: dict[int, "StatsWindow"] = {}
_proc_ref: WNDPROC | None = None
_measure_dc: int | None = None


def _dbg(msg: str) -> None:
    debug.log("stats", msg)


def _measure(font, text: str) -> int:
    """量一段文字的像素宽。用一个常驻的兼容 DC，别每次都建。

    底部两条要按像素宽决定「留哪几组」，靠 STATIC 自己换行不行 ——
    换行位置不受控，第二行还会溢出控件高度压到下一行上。
    """
    global _measure_dc
    if not font or not text:
        return 0
    if _measure_dc is None:
        screen = user32.GetDC(None)
        _measure_dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
    old = gdi32.SelectObject(_measure_dc, font)
    size = SIZE()
    gdi32.GetTextExtentPoint32W(_measure_dc, text, len(text), ctypes.byref(size))
    gdi32.SelectObject(_measure_dc, old)
    return size.cx


@WNDPROC
def _window_proc(hwnd, msg, wparam, lparam):
    window = _windows.get(hwnd)
    if window is not None:
        try:
            handled, result = window._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里绝不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _send_text(hwnd, msg, wparam, item_struct) -> None:
    """发带字符串指针的消息。

    两个必须一起做对的事，少一个都会变成「显示乱码但一切下标正常」的怪病：

    1. ``SendMessageW`` 的 LPARAM 在 ``w32`` 里声明成整数类型，结构指针不能直接
       传进去，要自己 ``addressof`` 取地址（这里踩过一次 ArgumentError，也见过
       传成野指针后控件显示垃圾字符）。
    2. 结构里的 ``pszText`` 指向 Python 字符串对象。结构体本身由调用方以局部
       变量持有，调用期间不会被回收；写成一行临时量就会读到野内存。
    """
    user32.SendMessageW(hwnd, msg, wparam,
                        ctypes.addressof(item_struct))


class StatsWindow:
    """用量统计窗口。单例：重复打开只是把已有窗口提到前面。"""

    def __init__(self, meter, cfg) -> None:
        self.meter = meter
        self.cfg = cfg
        self._hwnd = None
        self._hinstance = None
        self._bg_brush = None
        self._fonts: dict[str, int] = {}
        self._controls: dict[int, int] = {}
        self._columns_done = False
        # 底部两条的「完整」文本；设进控件的是按当前宽度截断后的版本
        self._summary_groups: list[tuple[str, int]] = []
        self._hint_groups: list[tuple[str, int]] = []
        self.scale = 1.0
        # 宽一点是为了底部那条汇总能放下「今天 / 本月 / 今年 / 累计」四组；
        # 窄了也不崩（_fit_groups 会按重要性丢组），只是少看到一两组。
        # 多出来的高度留给「选择器 + 明细框」，这样点一条就能看到它的全部数字。
        self.client_w = 800
        self.client_h = 620
        # 当前分页的行（表格与下拉共用同一份，顺序也一致）
        self._rows: list[dict] = []
        self._kind = TABS[0][0]
        # 灌数据时会触发 LVN_ITEMCHANGED，用一个标志避免「自己触发自己」递归
        self._filling = False

    # ------------------------------------------------------------- 尺寸

    def s(self, px: float) -> int:
        return int(round(px * self.scale))

    def _detect_dpi(self) -> None:
        screen = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(screen, _LOGPIXELSX) or 96
        user32.ReleaseDC(None, screen)
        self.scale = max(1.0, dpi / 96.0)

    @property
    def hwnd(self):
        return self._hwnd

    @property
    def is_open(self) -> bool:
        return bool(self._hwnd)

    # ------------------------------------------------------------- 字体

    def _make_fonts(self) -> None:
        for key, size, bold in (("body", 13, False), ("small", 12, False),
                                ("head", 13, True)):
            self._fonts[key] = gdi32.CreateFontW(
                -self.s(size), 0, 0, 0,
                FW_BOLD if bold else FW_NORMAL,
                0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, _FONT_FACE,
            )

    def _font(self, key: str = "body"):
        return self._fonts.get(key)

    # ------------------------------------------------------------- 创建

    def create(self) -> bool:
        if self._hwnd:
            return True
        global _proc_ref
        _proc_ref = _window_proc

        self._detect_dpi()
        if not self._fonts:
            self._make_fonts()
        self._hinstance = kernel32.GetModuleHandleW(None)
        self._bg_brush = user32.GetSysColorBrush(COLOR_BTNFACE)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = _window_proc
        wc.hInstance = self._hinstance
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))  # IDC_ARROW
        wc.hbrBackground = self._bg_brush
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:  # 已经注册过就无所谓
                return False

        # WS_CLIPCHILDREN：主窗口重绘时不刷子控件那块，避免切换 Tab 时闪一下。
        #
        # ⚠️ 它必须放进 **style**，不能放进 CreateWindowExW 的 exStyle ——
        # 这里踩过一次：写成 `CreateWindowExW(WS_CLIPCHILDREN, ...)` 之后，
        # WS_CLIPCHILDREN(0x02000000) 在扩展样式里对应的是 **WS_EX_COMPOSITED**，
        # 于是整个窗口走了「自下而上 + 子控件双缓冲」的特殊绘制通路，结果是
        # **ListView 的表体一个字都不画**（表头是独立的 header 子窗口，照常显示），
        # 而 LVM_GETITEMCOUNT / LVM_GETITEMTEXTW 读回来一切正常 —— 数据全对、
        # 就是白板，极难从代码上看出来。
        style = (WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME
                 | WS_CLIPCHILDREN)
        rect = wintypes.RECT(0, 0, self.s(self.client_w), self.s(self.client_h))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        x, y = self._center(win_w, win_h)

        self._hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, "用量统计", style,
            x, y, win_w, win_h, None, None, self._hinstance, None,
        )
        if not self._hwnd:
            return False

        _windows[self._hwnd] = self
        self._build_children()
        self._layout()
        self.refresh()
        _dbg(f"窗口已创建 hwnd={self._hwnd} scale={self.scale:.2f}")
        return True

    def _center(self, win_w: int, win_h: int) -> tuple[int, int]:
        cx = user32.GetSystemMetrics(SM_CXSCREEN)
        cy = user32.GetSystemMetrics(SM_CYSCREEN)
        return (max(0, (cx - win_w) // 2), max(0, (cy - win_h) // 2))

    # ------------------------------------------------------------- 子控件

    def _child(self, cls, text, style, cid, font="body", ex=0):
        handle = user32.CreateWindowExW(
            ex, cls, text, WS_CHILD | WS_VISIBLE | style,
            0, 0, 10, 10, self._hwnd, cid, self._hinstance, None,
        )
        if handle:
            user32.SendMessageW(handle, WM_SETFONT, self._font(font) or 0, 1)
            self._controls[cid] = handle
        return handle

    def _build_children(self) -> None:
        self._child(TAB_CLASS, "", 0, IDC_TAB, "body")
        list_style = (LVS_REPORT | LVS_SINGLESEL | LVS_SHOWSELALWAYS)
        self._child(LIST_CLASS, "", list_style, IDC_LIST, "body",
                    ex=WS_EX_CLIENTEDGE)
        # 「自己挑一条看」的选择器。用户的原话是「你这样对用户来说还是一个黑箱」——
        # 一张只能从头看到尾的表不等于能查，得让人直接指名道姓地挑。
        self._child("STATIC", "选一天", SS_LEFT, IDC_PICKLABEL, "body")
        self._child("COMBOBOX", "", CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP,
                    IDC_PICK, "body")
        self._child("BUTTON", "◀", BS_PUSHBUTTON, IDC_PREV, "body")
        self._child("BUTTON", "▶", BS_PUSHBUTTON, IDC_NEXT, "body")
        detail_style = (ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | WS_VSCROLL)
        self._child("EDIT", "", detail_style, IDC_DETAIL, "body",
                    ex=WS_EX_CLIENTEDGE)
        self._child("STATIC", "", SS_LEFT, IDC_SUMMARY, "body")
        self._child("STATIC", "", SS_LEFT, IDC_HINT, "small")
        self._child("BUTTON", "刷新", BS_PUSHBUTTON, IDC_REFRESH, "body")

        # 网格线 + 整行选中 + 双缓冲：表格该有的样子
        listview = self._controls.get(IDC_LIST)
        if listview:
            user32.SendMessageW(
                listview, LVM_SETEXTENDEDLISTVIEWSTYLE, 0,
                LVS_EX_FULLROWSELECT | LVS_EX_GRIDLINES | LVS_EX_DOUBLEBUFFER,
            )
        self._fill_tabs()

    def _fill_tabs(self) -> None:
        tab = self._controls.get(IDC_TAB)
        if not tab:
            return
        for index, (_key, label) in enumerate(TABS):
            item = TCITEMW()
            item.mask = TCIF_TEXT
            item.pszText = label          # 先绑成局部变量再取地址，见 _send_text
            item.cchTextMax = len(label)
            _send_text(tab, TCM_INSERTITEMW, index, item)
        user32.SendMessageW(tab, TCM_SETCURSEL, 0, 0)

    def _ensure_columns(self) -> None:
        if self._columns_done:
            return
        listview = self._controls.get(IDC_LIST)
        if not listview:
            return
        for index, (title, width, right) in enumerate(COLUMNS):
            col = LVCOLUMNW()
            col.mask = LVCF_FMT | LVCF_WIDTH | LVCF_TEXT | LVCF_SUBITEM
            col.fmt = LVCFMT_RIGHT if right else LVCFMT_LEFT
            col.cx = self.s(width)
            col.iSubItem = index
            col.pszText = title
            col.cchTextMax = len(title)
            _send_text(listview, LVM_INSERTCOLUMNW, index, col)
        self._columns_done = True

    def _layout(self) -> None:
        if not self._hwnd:
            return
        rect = wintypes.RECT()
        user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom
        margin = self.s(12)
        tab_h = self.s(28)
        btn_w = self.s(64)
        btn_h = self.s(24)
        pick_w = self.s(30)
        # 底部两条**各占一行**：汇总一条、说明一条。都按控件宽度截断，
        # 不做自动换行 —— STATIC 换行时第二行会盖到下面那条上（第一版就是这样，
        # 截图里「48.37 | 已记录 45 天」被压掉了一半）。
        sum_h = self.s(21)
        hint_h = self.s(19)
        # 明细框固定 5 行高：够写「电量/电费/时长/峰平谷/开机次数」，再多就没人看
        detail_h = self.s(92)
        gap = self.s(5)

        self._move(IDC_TAB, margin, margin,
                   max(10, w - 2 * margin - btn_w - self.s(8)), tab_h)
        # 「刷新」跟分页同一行、右对齐：底部那条汇总要放「今天/本月/今年/累计」
        # 四组数字，把按钮挤在汇总行右边的话宽度就不够，四组会掉到三组。
        self._move(IDC_REFRESH, w - margin - btn_w,
                   margin + (tab_h - btn_h) // 2, btn_w, btn_h)

        pick_y = margin + tab_h + self.s(6)
        label_w = self.s(56)
        pick_x = margin + label_w
        # 下拉框的「高度」参数里包含了展开后的列表高度，所以给大一点，
        # 否则展开只看得见两三条（COMBOBOX 的老规矩）。
        self._move(IDC_PICKLABEL, margin, pick_y + self.s(4), label_w, btn_h)
        combo_right = w - margin - (pick_w + self.s(4)) * 2
        self._move(IDC_PICK, pick_x, pick_y,
                   max(self.s(80), combo_right - pick_x), btn_h + self.s(220))
        self._move(IDC_PREV, combo_right + self.s(4), pick_y, pick_w, btn_h)
        self._move(IDC_NEXT, combo_right + self.s(4) * 2 + pick_w, pick_y,
                   pick_w, btn_h)

        top = pick_y + btn_h + self.s(6)
        bottom = detail_h + sum_h + hint_h + gap * 3 + self.s(6)
        list_h = max(self.s(60), h - top - bottom - margin)
        self._move(IDC_LIST, margin + self.s(4), top,
                   max(10, w - 2 * margin - self.s(8)), list_h)

        detail_y = top + list_h + gap
        self._move(IDC_DETAIL, margin, detail_y,
                   max(10, w - 2 * margin), detail_h)
        sum_y = detail_y + detail_h + gap
        self._move(IDC_SUMMARY, margin, sum_y,
                   max(10, w - 2 * margin), sum_h)
        self._move(IDC_HINT, margin, sum_y + sum_h + gap,
                   max(10, w - 2 * margin), hint_h)
        self._apply_texts(w)

    # --------------------------------------------------------- 底部两条文字

    def _fit_groups(self, groups, font_key: str, width: int,
                    joiner: str = _SEP) -> str:
        """把分组塞进 ``width``，塞不下就按**重要性**丢，而不是按位置丢。

        ``groups`` 是 ``[(文字, 保留优先级)]``，优先级小的先被丢。

        为什么强调「按重要性」：第一版是「从后往前丢」，于是宽度不够时
        「今年」留下了、「累计」反而被丢掉 —— 而累计恰恰是用户最关心的那个数。
        显示顺序（今天→本月→今年→累计，时间由近及远）和丢弃顺序本来就该是
        两件事。
        """
        items = [(t, r) for t, r in groups if t]
        if not items:
            return ""
        font = self._font(font_key)
        if not font or width <= 0:
            return joiner.join(t for t, _ in items)
        dropped = 0
        while len(items) > 1:
            text = joiner.join(t for t, _ in items)
            if _measure(font, text) <= width:
                return text + ("…" if dropped else "")
            victim = min(range(len(items)), key=lambda i: items[i][1])
            items.pop(victim)
            dropped += 1
        text = items[0][0]
        while text and _measure(font, text) > width:
            text = text[:-1]
        return text + ("…" if dropped or len(text) < len(items[0][0]) else "")

    def _apply_texts(self, client_w: int) -> None:
        """按当前控件宽度把底部两条截断（窗口缩放后要重算）。"""
        sum_w = max(10, client_w - 2 * self.s(12))
        self._set_text(IDC_SUMMARY,
                       self._fit_groups(self._summary_groups, "body", sum_w))
        self._set_text(IDC_HINT,
                       self._fit_groups(self._hint_groups, "small",
                                        max(10, client_w - 2 * self.s(12))))

    def _move(self, cid, x, y, w, h) -> None:
        handle = self._controls.get(cid)
        if handle:
            user32.MoveWindow(handle, int(x), int(y), int(w), int(h), True)

    # ------------------------------------------------------------- 数据

    def _current_kind(self) -> str:
        tab = self._controls.get(IDC_TAB)
        if not tab:
            return TABS[0][0]
        index = int(user32.SendMessageW(tab, TCM_GETCURSEL, 0, 0))
        if index < 0 or index >= len(TABS):
            return TABS[0][0]
        return TABS[index][0]

    def refresh(self) -> None:
        """重填表格、选择器与汇总行。切 Tab、点刷新、打开窗口时调。"""
        if not self._hwnd:
            return
        try:
            totals = self.meter.stats_totals()
            self._kind = self._current_kind()
            rows = self.meter.stats_rows(self._kind, MAX_ROWS)
        except Exception as exc:  # noqa: BLE001 - 统计失败不该让窗口消失
            _dbg(f"refresh 失败: {exc!r}")
            return
        self._rows = rows
        self._fill_rows(rows)
        self._fill_pick(rows)
        # 只存「完整」的分组，真正设进控件的是按当前宽度截断后的版本
        # （见 _apply_texts / _fit_groups）
        self._summary_groups = self._summary_groups_for(totals)
        # 底部只有一行文字，三组内容按**重要性**排（数字大的先留，见 _fit_groups）：
        #   ① 怎么用明细（点一行 / 选择器）—— 这是「不再黑箱」的入口，最该留；
        #   ② 账本口径（保留多久）—— 用户核对历史时的前提；
        #   ③ 已记录多少天 / 多少次 —— 最次要，挤不下就先丢它。
        self._hint_groups = (
            ("点表格任意一行 = 下面就是那一条的全部数字（也可用上面的选择器）", 6),
            ("账本保留最近 400 天 / 240 次开机", 5),
            (f"已记录 {totals['days']} 天 · {totals['sessions']} 次开机", 4),
        )
        self._layout()
        # 默认选中最新那一条（表格第一行），明细区立刻有内容 ——
        # 打开就是「黑箱」的最大来源：表格有数据但没人告诉你该看哪一行。
        self._select_index(0, sync_pick=True)
        _dbg(f"刷新 {len(rows)} 行（{self._kind}）")

    # --------------------------------------------------- 选择器与明细

    def _pick_label(self, row: dict) -> str:
        """下拉框里那一条的文字：看得到时间 + 电量，不用点进去猜。"""
        cur = getattr(self.cfg, "currency", "\u00a5")
        return (f"{row.get('when', '')}    {_energy(row.get('wh', 0.0))}"
                f" · {cur}{row.get('cost', 0.0):.2f}")

    def _pick_title(self) -> str:
        return {"day": "选一天", "month": "选一个月", "year": "选一年",
                "session": "选一次开机", "hour": "选一个时段"}.get(
                    self._kind, "选一条")

    def _fill_pick(self, rows: list[dict]) -> None:
        """把当前分页的行灌进下拉框（顺序与表格一致：新的在前）。"""
        combo = self._controls.get(IDC_PICK)
        if not combo:
            return
        self._set_text(IDC_PICKLABEL, self._pick_title())
        user32.SendMessageW(combo, WM_SETREDRAW, 0, 0)
        user32.SendMessageW(combo, CB_RESETCONTENT, 0, 0)
        for row in rows:
            text = self._pick_label(row)
            buf = ctypes.c_wchar_p(text)     # 必须活过 SendMessage，见 _send_text
            user32.SendMessageW(
                combo, CB_ADDSTRING, 0,
                ctypes.cast(buf, ctypes.c_void_p).value,
            )
        user32.SendMessageW(combo, WM_SETREDRAW, 1, 0)
        user32.InvalidateRect(combo, None, True)

    def _selected_index(self) -> int:
        combo = self._controls.get(IDC_PICK)
        if not combo:
            return -1
        index = int(user32.SendMessageW(combo, CB_GETCURSEL, 0, 0))
        return index if 0 <= index < len(self._rows) else -1

    def _select_index(self, index: int, sync_pick: bool = True) -> None:
        """选中第 index 条：同步表格高亮、下拉框、明细框。"""
        if not self._rows:
            self._set_text(IDC_DETAIL, "还没有可看的记录 —— 程序跑一会儿就会有了。")
            return
        index = max(0, min(index, len(self._rows) - 1))
        listview = self._controls.get(IDC_LIST)
        if listview:
            # LVM_SETITEMSTATE 要的是 LVITEM 指针；state 在 stateMask 里的位、
            # stateMask 写想要的位 —— 只给 SELECTED 不给 FOCUSED 会出现
            # 「高亮在 A、焦点框在 B」的双选中怪相。
            item = LVITEMW()
            item.stateMask = LVIS_SELECTED | LVIS_FOCUSED
            item.state = LVIS_SELECTED | LVIS_FOCUSED
            item.iItem = index
            self._filling = True
            try:
                user32.SendMessageW(listview, LVM_SETITEMSTATE, index,
                                    ctypes.addressof(item))
                user32.SendMessageW(listview, LVM_ENSUREVISIBLE, index, 0)
            except Exception as exc:  # noqa: BLE001
                _dbg(f"选中行失败: {exc!r}")
            finally:
                self._filling = False
        if sync_pick:
            combo = self._controls.get(IDC_PICK)
            if combo:
                user32.SendMessageW(combo, CB_SETCURSEL, index, 0)
        self._set_text(IDC_DETAIL, self._detail_text(self._rows[index]))

    def _step(self, delta: int) -> None:
        index = self._selected_index()
        if index < 0:
            index = 0
        else:
            index += delta
        self._select_index(index, sync_pick=True)

    def _detail_text(self, row: dict) -> str:
        """选中那一条的完整数字。这是「不是黑箱」的落点。"""
        cur = getattr(self.cfg, "currency", "\u00a5")
        wh = float(row.get("wh", 0.0))
        peak = float(row.get("peak_wh", 0.0))
        valley = float(row.get("valley_wh", 0.0))
        flat = max(0.0, wh - peak - valley)
        secs = float(row.get("seconds", 0.0))
        avg_w = (wh * 3600.0 / secs) if secs > 0 else 0.0
        lines: list[str] = []

        if self._kind == "session":
            boot = row.get("boot")
            last = row.get("last")
            lines.append(f"本次开机时刻  {_stamp(boot)}")
            if boot and last:
                lines.append(f"开机持续      {_short_time(last - boot)}"
                             f"    程序统计到  {_short_time(secs)}")
            lines.append(f"这一段用电    {_energy(wh)}    {cur}{row.get('cost', 0.0):.2f}")
            if avg_w > 0:
                lines.append(f"该段平均功率  {avg_w:.0f} W")
            lines.append(f"峰段 {_energy(peak)}   平段 {_energy(flat)}"
                         f"   谷段 {_energy(valley)}")
        elif self._kind == "hour":
            lines.append(f"时段          {row.get('when', '')}")
            lines.append(f"累计用电      {_energy(wh)}    {cur}{row.get('cost', 0.0):.2f}"
                         "（谷价按当前月份估算）")
            lines.append(f"{row.get('note', '')}")
            lines.append(f"其中谷段电量  {_energy(valley)}")
        elif self._kind == "day":
            lines.append(f"日期          {row.get('when', '')}")
            lines.append(f"当天用电      {_energy(wh)}    {cur}{row.get('cost', 0.0):.2f}")
            lines.append(f"当天开机次数  {row.get('note', '')}")
            if secs > 0:
                lines.append(f"程序统计到    {_short_time(secs)}"
                             f"    平均 {avg_w:.0f} W")
            lines.append(f"峰段 {_energy(peak)}   平段 {_energy(flat)}"
                         f"   谷段 {_energy(valley)}")
        else:  # month / year
            unit = "一个月" if self._kind == "month" else "一年"
            lines.append(f"{unit}        {row.get('when', '')}")
            lines.append(f"合计用电      {_energy(wh)}    {cur}{row.get('cost', 0.0):.2f}")
            lines.append(f"覆盖天数      {row.get('note', '')}")
            if secs > 0:
                lines.append(f"程序统计到    {_short_time(secs)}")
        lines.append(f"峰段 {_energy(peak)}   平段 {_energy(flat)}"
                     f"   谷段 {_energy(valley)}")
        return "\r\n".join(lines)

    def _summary_groups_for(self, totals: dict) -> list[tuple[str, int]]:
        """汇总行的分组 + 保留优先级（数字越大越不该被丢）。

        显示顺序是「由近及远」；丢的顺序却是「今年 → 本月 → 今天 → 累计」——
        累计是用户最想知道的，哪怕窗口拉得很窄也得留着。
        """
        cur = getattr(self.cfg, "currency", "\u00a5")

        def one(pair) -> str:
            wh, cost = pair
            return f"{_energy(wh)} · {cur}{cost:.2f}"

        return [
            (f"今天 {one(totals['today'])}", 2),
            (f"本月 {one(totals['month'])}", 1),
            (f"今年 {one(totals['year'])}", 0),
            (f"累计 {one(totals['total'])}", 3),
        ]

    def _fill_rows(self, rows: list[dict]) -> None:
        listview = self._controls.get(IDC_LIST)
        if not listview:
            return
        self._ensure_columns()
        # 灌数据期间每次插入都会发一条 LVN_ITEMCHANGED（选中态在变），不挡掉的话
        # 每行都白跑一次「刷新明细」，几百行就是几百次无谓的开销。
        self._filling = True
        try:
            user32.SendMessageW(listview, WM_SETREDRAW, 0, 0)
            user32.SendMessageW(listview, LVM_DELETEALLITEMS, 0, 0)
            cur = getattr(self.cfg, "currency", "\u00a5")
            for index, row in enumerate(rows):
                cells = (
                    str(row.get("when", "")),
                    _energy(row.get("wh", 0.0)),
                    f"{cur}{row.get('cost', 0.0):.2f}",
                    _short_time(row.get("seconds", 0.0))
                    if row.get("seconds") else "—",
                    str(row.get("note", "") or ""),
                )
                # 第一列随 LVM_INSERTITEMW 一起给，其余列再 LVM_SETITEMTEXTW
                for sub, text in enumerate(cells):
                    item = LVITEMW()
                    if sub == 0:
                        item.mask = LVIF_TEXT
                        item.iItem = index
                        item.iSubItem = 0
                        item.pszText = cells[0]
                        item.cchTextMax = len(cells[0])
                        _send_text(listview, LVM_INSERTITEMW, 0, item)
                    else:
                        item.mask = LVIF_TEXT
                        item.iItem = index
                        item.iSubItem = sub
                        item.pszText = text
                        item.cchTextMax = len(text)
                        _send_text(listview, LVM_SETITEMTEXTW, index, item)
            user32.SendMessageW(listview, WM_SETREDRAW, 1, 0)
            # 关掉重绘后必须手动重画一次，否则内容要等下一次窗口失效才出现
            user32.InvalidateRect(listview, None, True)
        finally:
            self._filling = False

    def _set_text(self, cid, text: str) -> None:
        handle = self._controls.get(cid)
        if handle:
            user32.SetWindowTextW(handle, text)

    # ------------------------------------------------------------- 消息

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_SIZE:
            self._layout()
            return True, 0
        if msg == WM_COMMAND:
            cid = wparam & 0xFFFF
            code = (wparam >> 16) & 0xFFFF
            if code == BN_CLICKED:
                if cid == IDC_REFRESH:
                    self.refresh()
                    return True, 0
                if cid == IDC_PREV:
                    self._step(-1)
                    return True, 0
                if cid == IDC_NEXT:
                    self._step(1)
                    return True, 0
            if cid == IDC_PICK and code == CBN_SELCHANGE:
                # 下拉框里挑了某一天 / 某一次开机 → 表格跟着跳到那一行
                index = self._selected_index()
                if index >= 0:
                    self._select_index(index, sync_pick=False)
                return True, 0
            return False, 0
        if msg == WM_NOTIFY:
            header = ctypes.cast(lparam, ctypes.POINTER(NMHDR)).contents
            if header.idFrom == IDC_TAB and header.code == TCN_SELCHANGE:
                self.refresh()
                return True, 0
            if header.idFrom == IDC_LIST and header.code == LVN_ITEMCHANGED:
                # 灌数据时每次 InsertItem 都会触发一次，用标志挡掉（否则白跑一堆）
                if not self._filling:
                    listview = self._controls.get(IDC_LIST)
                    if listview:
                        index = int(user32.SendMessageW(
                            listview, LVM_GETNEXTITEM, -1, LVNI_SELECTED))
                        if 0 <= index < len(self._rows):
                            self._select_index(index, sync_pick=True)
                return True, 0
            return False, 0
        if msg == WM_CTLCOLORSTATIC or msg == WM_CTLCOLOREDIT:
            # 明细框是只读 EDIT：不接管的话它会是纯白底，和窗口的浅灰拼在一起
            # 像贴了张别的程序里的便签。接管成同一个底色，视觉上才是「一块内容」。
            gdi32.SetBkMode(wparam, TRANSPARENT)
            return True, self._bg_brush or 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(self._hwnd)
            return True, 0
        if msg == WM_DESTROY:
            _windows.pop(self._hwnd, None)
            self._hwnd = None
            self._columns_done = False
            self._rows = []
            for font in self._fonts.values():
                gdi32.DeleteObject(font)
            self._fonts.clear()
            self._controls.clear()
            return True, 0
        return False, 0

    # ------------------------------------------------------------- 显隐

    def show(self, kind: str | None = None, key: str | None = None) -> None:
        """打开窗口。``kind`` / ``key`` 让托盘菜单直接定位到某一天/某一次开机。

        用户的原话是「能不能让用户在菜单里可以自主选择某一天去看」——
        菜单里点「09-16 周三 0.98 kWh」就该直接落到那一天的明细上，
        而不是打开一张表让用户自己滚着找。
        """
        if not self._hwnd:
            if not self.create():
                return
        else:
            self.refresh()
        if kind:
            self._goto(kind, key)
        user32.ShowWindow(self._hwnd, 5)  # SW_SHOW
        # 与详情面板 / 电价窗口同样的路子：先临时置顶再取消，绕开前台锁
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(self._hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetForegroundWindow(self._hwnd)

    def _goto(self, kind: str, key: str | None) -> None:
        """切到 ``kind`` 分页，并把选择器定位到 ``key`` 那一条。"""
        tab = self._controls.get(IDC_TAB)
        if tab:
            for index, (k, _label) in enumerate(TABS):
                if k == kind:
                    user32.SendMessageW(tab, TCM_SETCURSEL, index, 0)
                    self.refresh()
                    break
        if key is None:
            return
        target = str(key)
        for index, row in enumerate(self._rows):
            if str(row.get("key", "")) == target:
                self._select_index(index, sync_pick=True)
                return
        # 找不到（比如那条记录刚好被 400 天 / 240 次的上限裁掉了）就停在最新一条，
        # 不要静默停在空白上让用户以为坏了。
        _dbg(f"定位不到 {target}（{kind}），已停在最新一条")

    def destroy(self) -> None:
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)


def _energy(wh: float) -> str:
    """电量：小于 1 kWh 用 Wh，否则用 kWh（两位小数）。"""
    if abs(wh) >= 1000.0:
        return f"{wh / 1000.0:.2f} kWh"
    return f"{wh:.1f} Wh"


def _stamp(ts) -> str:
    """开机时刻写成 ``2026-09-16 01:20``。读不出来就原样返回，别崩。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


__all__ = ["StatsWindow", "TABS", "COLUMNS"]
