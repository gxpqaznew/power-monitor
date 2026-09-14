"""电价设置窗口 —— 原生 Win32 控件（下拉框 / 编辑框 / 按钮）。

居民电价是全国最不统一的价目之一：各省单价不同，同一个省内还分档位、
分峰谷时段、部分地区还分丰枯水期。写死在配置里对谁都不合适，所以这里
做两件事：

1. 内置各省预设（见 ``tariffs.py``），选省份 → 选方案 → 自动填报；
2. 每一项都是普通编辑框，可以直接改成自己电费账单上的数字。

窗口用系统自带控件搭，输入法、光标、选区、Tab 轮转、右键菜单全部由系统
负责——详情面板那种逐像素自绘的做法在这里只会平白多出几十处坑。
"""

from __future__ import annotations

import ctypes
import re

from . import debug, tariffs
from .tariffs import REGIONS, Plan, Region
from .w32 import (
    BN_CLICKED,
    BS_DEFPUSHBUTTON,
    BS_PUSHBUTTON,
    CB_ADDSTRING,
    CB_GETCURSEL,
    CB_RESETCONTENT,
    CB_SETCURSEL,
    CBN_SELCHANGE,
    CBS_AUTOHSCROLL,
    CBS_DROPDOWNLIST,
    CBS_HASSTRINGS,
    CBS_NOINTEGRALHEIGHT,
    CLEARTYPE_QUALITY,
    COLOR_BTNFACE,
    DC_HASDEFID,
    DEFAULT_CHARSET,
    DM_GETDEFID,
    ES_AUTOHSCROLL,
    ES_LEFT,
    FW_BOLD,
    FW_NORMAL,
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    IDCANCEL,
    IDOK,
    MB_ICONWARNING,
    MB_OK,
    SM_CXSCREEN,
    SM_CYSCREEN,
    SS_LEFT,
    SS_RIGHT,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    TRANSPARENT,
    WM_CLOSE,
    WM_COMMAND,
    WM_CTLCOLORSTATIC,
    WM_DESTROY,
    WM_SETFONT,
    WNDCLASSEXW,
    WNDPROC,
    WS_BORDER,
    WS_CAPTION,
    WS_CHILD,
    WS_EX_CONTROLPARENT,
    WS_GROUP,
    WS_OVERLAPPED,
    WS_SYSMENU,
    WS_TABSTOP,
    WS_VISIBLE,
    WS_VSCROLL,
    gdi32,
    kernel32,
    user32,
    wintypes,
)

_CLASS_NAME = "PowerMonitorFeeWnd"
_FONT_FACE = "Microsoft YaHei UI"
_LOGPIXELSX = 88
_SPI_GETWORKAREA = 0x0030

# 控件 ID
IDC_HINT = 90
IDC_HEAD1 = 91
IDC_HEAD2 = 92
IDC_HEAD3 = 93
IDC_HEAD4 = 94
IDC_REGION = 100
IDC_PLAN = 101
IDC_PEAK = 110
IDC_FLAT = 111
IDC_VALLEY_DRY = 112
IDC_VALLEY_WET = 113
IDC_PEAK_HOURS = 120
IDC_VALLEY_HOURS = 121
IDC_WET_MONTHS = 122
IDC_NOTE = 130
IDC_SOURCE = 131
IDC_SAVE = 140
IDC_CANCEL = 141
IDC_REFILL = 142
IDC_LBL_REGION = 150
IDC_LBL_PLAN = 151
IDC_LBL_PEAK = 152
IDC_LBL_FLAT = 153
IDC_LBL_DRY = 154
IDC_LBL_WET = 155
IDC_LBL_PEAK_HOURS = 156
IDC_LBL_VALLEY_HOURS = 157
IDC_LBL_WET_MONTHS = 158
IDC_LBL_BASE = 159
IDC_LBL_MONITOR = 160
IDC_LBL_CALIB = 161
IDC_BASE = 162
IDC_MONITOR = 163
IDC_CALIB = 164

CUSTOM_LABEL = "— 自定义（不套用预设）—"
CUSTOM_PLAN_LABEL = "手动填写"

_SPLIT = re.compile(r"[,，、;；\s]+")
_RANGE = re.compile(r"^(\d{1,2})\s*[-~—]\s*(\d{1,2})$")
_HOURS_CHUNK = re.compile(r"^\d{1,2}(?:[-~]\d{1,2})?$")

_dialogs: dict[int, "FeeSettingsDialog"] = {}
_proc_ref: WNDPROC | None = None


def _dbg(msg: str) -> None:
    debug.log("fee", msg)


def _fmt_price(value: float) -> str:
    """保留 4 位小数显示：电价就是 4 位精度，多余位数反而碍眼。"""
    return f"{value:.4f}"


def _parse_price(text: str, label: str) -> float:
    raw = (text or "").strip().replace("￥", "").replace("¥", "").replace("元", "")
    if not raw:
        raise ValueError(f"「{label}」不能为空。\n不需要的时段可以填 0，但格子不能空着。")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"「{label}」的“{text.strip()}”不是合法数字。") from None
    if value < 0:
        raise ValueError(f"「{label}」不能是负数。")
    if value > 10:
        raise ValueError(f"「{label}」是 {value} 元/度，明显偏高——请确认没写错（应为元/度）。")
    return round(value, 6)


def _parse_num(text: str, label: str, lo: float, hi: float, unit: str = "") -> float:
    """解析一个带范围校验的数字（功耗 / 系数）。"""
    raw = (text or "").strip().replace("W", "").replace("w", "").replace("瓦", "")
    if not raw:
        raise ValueError(f"「{label}」不能为空。不需要就填 0。")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"「{label}」的“{text.strip()}”不是合法数字。") from None
    if not (lo <= value <= hi):
        raise ValueError(f"「{label}」应在 {lo:g} ~ {hi:g}{unit} 之间，填的是 {value:g}。")
    return value


def _parse_hours(text: str, label: str) -> str:
    """校验并规整时段写法。空串表示「没有这一段」，是合法值。"""
    raw = (text or "").strip().replace("：", ":").replace("点", "").replace("小时", "")
    if not raw:
        return ""
    for chunk in _SPLIT.split(raw):
        if not chunk:
            continue
        if not _HOURS_CHUNK.match(chunk):
            raise ValueError(
                f"「{label}」的“{chunk}”看不懂。\n"
                "写法示例：8-22（8 点到 21 点）、23-7（跨零点）、11-17,20-22"
            )
        for part in chunk.replace("~", "-").split("-"):
            if int(part) > 23:
                raise ValueError(f"「{label}」里的 {part} 不是有效小时（0～23）。")
    if not tariffs.parse_hours(raw):
        raise ValueError(f"「{label}」解析不出时间段，请参考：8-22 / 23-7 / 11-17,20-22")
    return raw


def _parse_months(text: str, label: str) -> list[int]:
    """丰水期月份：支持 6,7,8 与 6-10 与 11-3（跨年）三种写法。空 = 无季节差。"""
    raw = (text or "").strip().replace("月", "").replace("月份", "")
    if not raw:
        return []
    months: set[int] = set()
    for chunk in _SPLIT.split(raw):
        if not chunk:
            continue
        match = _RANGE.match(chunk)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if not (1 <= start <= 12 and 1 <= end <= 12):
                raise ValueError(f"「{label}」里的 {chunk} 超出 1～12 月。")
            cur = start
            while True:
                months.add(cur)
                if cur == end:
                    break
                cur = cur % 12 + 1
            continue
        try:
            month = int(chunk)
        except ValueError:
            raise ValueError(
                f"「{label}」的“{chunk}”看不懂。\n写法示例：6,7,8,9,10 或 6-10；留空表示无丰枯差价。"
            ) from None
        if not 1 <= month <= 12:
            raise ValueError(f"「{label}」里的 {month} 不是有效月份（1～12）。")
        months.add(month)
    return sorted(months)


def _fmt_months(months) -> str:
    return ",".join(str(int(m)) for m in months or ())


@WNDPROC
def _dialog_proc(hwnd, msg, wparam, lparam):
    dialog = _dialogs.get(hwnd)
    if dialog is not None:
        try:
            handled, result = dialog._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class FeeSettingsDialog:
    """电价设置窗口。单例：重复打开只是把已有窗口提到前面。"""

    def __init__(self, cfg, on_saved=None) -> None:
        self.cfg = cfg
        self._on_saved = on_saved
        self._hwnd = None
        self._hinstance = None
        self._bg_brush = None
        self._fonts: dict[str, int] = {}
        self._controls: dict[int, int] = {}
        self._region_index: list[Region | None] = []
        self._plan_index: list[Plan | None] = []
        self.scale = 1.0
        self.client_w = 470
        self.client_h = 478

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
        for key, size, bold in (
            ("body", 13, False),
            ("small", 11, False),
            ("head", 12, True),
        ):
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
        _proc_ref = _dialog_proc

        self._detect_dpi()
        if not self._fonts:
            self._make_fonts()
        self._hinstance = kernel32.GetModuleHandleW(None)
        self._bg_brush = user32.GetSysColorBrush(COLOR_BTNFACE)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = _dialog_proc
        wc.hInstance = self._hinstance
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))  # IDC_ARROW
        wc.hbrBackground = self._bg_brush
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:  # 已注册过无所谓
                return False

        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU
        rect = wintypes.RECT(0, 0, self.s(self.client_w), self.s(self.client_h))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, WS_EX_CONTROLPARENT)
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        x, y = self._center(win_w, win_h)

        self._hwnd = user32.CreateWindowExW(
            WS_EX_CONTROLPARENT, _CLASS_NAME, "电价设置", style,
            x, y, win_w, win_h, None, None, self._hinstance, None,
        )
        if not self._hwnd:
            return False

        _dialogs[self._hwnd] = self
        self._build_children()
        self._layout()
        self._load_from_config()
        _dbg(f"窗口已创建 hwnd={self._hwnd} scale={self.scale:.2f}")
        return True

    def _center(self, win_w: int, win_h: int) -> tuple[int, int]:
        work = wintypes.RECT()
        got = user32.SystemParametersInfoW(
            _SPI_GETWORKAREA, 0, ctypes.byref(work), 0
        )
        if not got:
            right, bottom, left, top = (
                user32.GetSystemMetrics(SM_CXSCREEN),
                user32.GetSystemMetrics(SM_CYSCREEN),
                0,
                0,
            )
        else:
            right, bottom, left, top = work.right, work.bottom, work.left, work.top
        cx = left + (right - left - win_w) // 2
        cy = top + max(0, (bottom - top - win_h) // 3)
        return cx, cy

    # ------------------------------------------------------------- 子控件

    def _child(self, cls: str, text: str, style: int, cid: int, font: str = "body"):
        handle = user32.CreateWindowExW(
            0, cls, text, WS_CHILD | WS_VISIBLE | style,
            0, 0, 10, 10, self._hwnd, cid, self._hinstance, None,
        )
        if handle:
            user32.SendMessageW(handle, WM_SETFONT, self._font(font) or 0, 1)
            self._controls[cid] = handle
        return handle

    def _build_children(self) -> None:
        lbl = "STATIC"
        edit = "EDIT"
        btn = "BUTTON"
        combo = "COMBOBOX"

        self._child(lbl, "选中省份会自动填报，数字都可以直接改。", SS_LEFT, IDC_HINT, "small")
        self._child(lbl, "地区与方案", SS_LEFT, IDC_HEAD1, "head")
        self._child(lbl, "所在省份", SS_RIGHT, IDC_LBL_REGION)
        self._child(lbl, "用电方案", SS_RIGHT, IDC_LBL_PLAN)
        combo_style = (
            CBS_DROPDOWNLIST | CBS_HASSTRINGS | CBS_AUTOHSCROLL
            | CBS_NOINTEGRALHEIGHT | WS_TABSTOP | WS_VSCROLL | WS_GROUP
        )
        self._child(combo, "", combo_style, IDC_REGION)
        self._child(combo, "", combo_style, IDC_PLAN)

        self._child(lbl, "电价（元／度）", SS_LEFT, IDC_HEAD2, "head")
        for cid, text in (
            (IDC_LBL_PEAK, "峰段电价"),
            (IDC_LBL_FLAT, "平段电价"),
            (IDC_LBL_DRY, "谷段·枯水"),
            (IDC_LBL_WET, "谷段·丰水"),
        ):
            self._child(lbl, text, SS_RIGHT, cid)

        self._child(lbl, "时段与丰水期", SS_LEFT, IDC_HEAD3, "head")
        self._child(lbl, "峰段时段", SS_RIGHT, IDC_LBL_PEAK_HOURS)
        self._child(lbl, "谷段时段", SS_RIGHT, IDC_LBL_VALLEY_HOURS)
        self._child(lbl, "丰水期月份", SS_RIGHT, IDC_LBL_WET_MONTHS)

        self._child(lbl, "功耗模型（瓦）", SS_LEFT, IDC_HEAD4, "head")
        self._child(lbl, "其他功耗", SS_RIGHT, IDC_LBL_BASE)
        self._child(lbl, "显示器功耗", SS_RIGHT, IDC_LBL_MONITOR)
        self._child(lbl, "校准系数", SS_RIGHT, IDC_LBL_CALIB)

        edit_style = ES_LEFT | ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP
        for cid in (IDC_PEAK, IDC_FLAT, IDC_VALLEY_DRY, IDC_VALLEY_WET,
                    IDC_PEAK_HOURS, IDC_VALLEY_HOURS, IDC_WET_MONTHS,
                    IDC_BASE, IDC_MONITOR, IDC_CALIB):
            self._child(edit, "", edit_style, cid)

        self._child(lbl, "", SS_LEFT, IDC_NOTE, "small")
        self._child(lbl, "", SS_LEFT, IDC_SOURCE, "small")

        self._child(btn, "重新填入预设", BS_PUSHBUTTON | WS_TABSTOP, IDC_REFILL)
        self._child(btn, "保存并生效", BS_DEFPUSHBUTTON | WS_TABSTOP, IDC_SAVE)
        self._child(btn, "取消", BS_PUSHBUTTON | WS_TABSTOP, IDC_CANCEL)

    # ------------------------------------------------------------- 版面

    def _place(self, cid: int, x: int, y: int, w: int, h: int) -> None:
        handle = self._controls.get(cid)
        if handle:
            user32.MoveWindow(handle, x, y, w, h, True)

    def _layout(self) -> None:
        S = self.s
        m = S(16)
        lx = m                 # 标签左边缘（右对齐到 lw）
        lw = S(76)
        cx = lx + lw + S(4)    # 控件左边缘
        col2 = m + S(220)
        cx2 = col2 + lw + S(4)
        ew = S(124)            # 两列表格里的编辑框宽
        wide = self.s(self.client_w) - cx - m
        inner = self.s(self.client_w) - m * 2
        eh = S(24)             # 编辑框高

        y = S(12)
        self._place(IDC_HINT, m, y, inner, S(17)); y += S(22)
        self._place(IDC_HEAD1, m, y, inner, S(17)); y += S(21)
        self._place(IDC_LBL_REGION, lx, y + S(3), lw, S(20))
        # 下拉框的闭合高度由字体决定（比编辑框高几像素，且改不了：CB_SETITEMHEIGHT
        # 对非自绘下拉框无效），行距留宽一点，免得压到下一行的标题上
        self._place(IDC_REGION, cx, y, wide, S(280)); y += S(30)
        self._place(IDC_LBL_PLAN, lx, y + S(3), lw, S(20))
        self._place(IDC_PLAN, cx, y, wide, S(240)); y += S(34)

        self._place(IDC_HEAD2, m, y, inner, S(17)); y += S(21)
        self._place(IDC_LBL_PEAK, lx, y + S(3), lw, S(20))
        self._place(IDC_PEAK, cx, y, ew, eh)
        self._place(IDC_LBL_FLAT, col2, y + S(3), lw, S(20))
        self._place(IDC_FLAT, cx2, y, ew, eh)
        y += S(27)
        self._place(IDC_LBL_DRY, lx, y + S(3), lw, S(20))
        self._place(IDC_VALLEY_DRY, cx, y, ew, eh)
        self._place(IDC_LBL_WET, col2, y + S(3), lw, S(20))
        self._place(IDC_VALLEY_WET, cx2, y, ew, eh)
        y += S(29)

        self._place(IDC_HEAD3, m, y, inner, S(17)); y += S(21)
        self._place(IDC_LBL_PEAK_HOURS, lx, y + S(3), lw, S(20))
        self._place(IDC_PEAK_HOURS, cx, y, ew, eh)
        self._place(IDC_LBL_VALLEY_HOURS, col2, y + S(3), lw, S(20))
        self._place(IDC_VALLEY_HOURS, cx2, y, ew, eh)
        y += S(27)
        self._place(IDC_LBL_WET_MONTHS, lx, y + S(3), lw, S(20))
        self._place(IDC_WET_MONTHS, cx, y, ew, eh)
        y += S(29)

        self._place(IDC_HEAD4, m, y, inner, S(17)); y += S(21)
        self._place(IDC_LBL_BASE, lx, y + S(3), lw, S(20))
        self._place(IDC_BASE, cx, y, ew, eh)
        self._place(IDC_LBL_MONITOR, col2, y + S(3), lw, S(20))
        self._place(IDC_MONITOR, cx2, y, ew, eh)
        y += S(27)
        self._place(IDC_LBL_CALIB, lx, y + S(3), lw, S(20))
        self._place(IDC_CALIB, cx, y, ew, eh)
        y += S(29)

        self._place(IDC_NOTE, m, y, inner, S(40)); y += S(44)
        self._place(IDC_SOURCE, m, y, inner, S(32)); y += S(36)

        bh = S(28)
        self._place(IDC_REFILL, m, y, S(112), bh)
        self._place(IDC_CANCEL, self.s(self.client_w) - m - S(88), y, S(88), bh)
        self._place(IDC_SAVE, self.s(self.client_w) - m - S(88) * 2 - S(8), y, S(88), bh)

        # 版面算错（少留一行）会让按钮被窗口底边切掉一半，静默出错很难发现
        bottom = y + bh
        if bottom > self.s(self.client_h):
            _dbg(f"版面溢出：内容底部 {bottom} > 客户区高 {self.s(self.client_h)}")

    # ------------------------------------------------------------- 数据

    def _set_text(self, cid: int, text: str) -> None:
        handle = self._controls.get(cid)
        if handle:
            user32.SetWindowTextW(handle, text)

    def _text(self, cid: int) -> str:
        handle = self._controls.get(cid)
        if not handle:
            return ""
        length = user32.GetWindowTextLengthW(handle) + 2
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(handle, buf, length)
        return buf.value.strip()

    def _combo_index(self, cid: int) -> int:
        handle = self._controls.get(cid)
        if not handle:
            return -1
        return int(user32.SendMessageW(handle, CB_GETCURSEL, 0, 0))

    def _combo_fill(self, cid: int, items: list[str], select: int = 0) -> None:
        handle = self._controls.get(cid)
        if not handle:
            return
        user32.SendMessageW(handle, CB_RESETCONTENT, 0, 0)
        for text in items:
            # SendMessageW 的 LPARAM 声明是整数（LRESULT 一脉的签名），传字符串
            # 只能自己取指针。这里必须留一个引用：写成一行的话，c_wchar_p 这个
            # 临时对象会在取完地址、真正调用之前就被回收，下拉框读到的是野内存里
            # 的垃圾字符——而选中下标、各项数量全是好的，只有显示是乱的，极难查。
            buf = ctypes.c_wchar_p(text)
            user32.SendMessageW(
                handle, CB_ADDSTRING, 0, ctypes.cast(buf, ctypes.c_void_p).value or 0
            )
        user32.SendMessageW(handle, CB_SETCURSEL, max(0, select), 0)

    def _selected_region(self) -> Region | None:
        idx = self._combo_index(IDC_REGION)
        if 0 <= idx < len(self._region_index):
            return self._region_index[idx]
        return None

    def _selected_plan(self) -> Plan | None:
        idx = self._combo_index(IDC_PLAN)
        if 0 <= idx < len(self._plan_index):
            return self._plan_index[idx]
        return None

    def _load_from_config(self) -> None:
        """打开窗口时：下拉框定位到当前生效的地区/方案，编辑框填当前值。"""
        cfg = self.cfg
        self._region_index = [None] + list(REGIONS)

        region = tariffs.find_region(cfg.tariff_region)
        index = 0
        if region is not None and region in REGIONS:
            index = REGIONS.index(region) + 1
        self._combo_fill(IDC_REGION, [CUSTOM_LABEL] + tariffs.region_names(), index)

        if region is None:
            self._plan_index = [None]
            self._combo_fill(IDC_PLAN, [CUSTOM_PLAN_LABEL], 0)
        else:
            self._plan_index = list(region.plans)
            labels = tariffs.plan_labels(region)
            plan_index = 0
            if cfg.tariff_plan in labels:
                plan_index = labels.index(cfg.tariff_plan)
            self._combo_fill(IDC_PLAN, labels, plan_index)

        self._fill_fields_from_cfg(cfg)
        self._refresh_note()

    def _fill_fields_from_cfg(self, cfg) -> None:
        self._set_text(IDC_PEAK, _fmt_price(cfg.price_peak))
        self._set_text(IDC_FLAT, _fmt_price(cfg.price_flat))
        self._set_text(IDC_VALLEY_DRY, _fmt_price(cfg.price_valley_dry))
        self._set_text(IDC_VALLEY_WET, _fmt_price(cfg.price_valley_wet))
        self._set_text(IDC_PEAK_HOURS, cfg.peak_hours or "")
        self._set_text(IDC_VALLEY_HOURS, cfg.valley_hours or "")
        self._set_text(IDC_WET_MONTHS, _fmt_months(cfg.valley_wet_months))
        # 功耗模型：显示器填 0 就等同于「不计入」
        self._set_text(IDC_BASE, f"{cfg.baseline_watts:.0f}")
        self._set_text(IDC_MONITOR, f"{cfg.monitor_watts:.0f}" if cfg.include_monitor else "0")
        self._set_text(IDC_CALIB, f"{cfg.calibration:.2f}")

    def _fill_fields_from_plan(self, plan: Plan) -> None:
        self._set_text(IDC_PEAK, _fmt_price(plan.peak))
        self._set_text(IDC_FLAT, _fmt_price(plan.flat))
        self._set_text(IDC_VALLEY_DRY, _fmt_price(plan.valley))
        self._set_text(IDC_VALLEY_WET, _fmt_price(plan.resolved_valley_wet()))
        self._set_text(IDC_PEAK_HOURS, plan.peak_hours or "")
        self._set_text(IDC_VALLEY_HOURS, plan.valley_hours or "")
        self._set_text(IDC_WET_MONTHS, _fmt_months(plan.wet_months))

    def _refresh_note(self) -> None:
        region = self._selected_region()
        plan = self._selected_plan()
        if region is None:
            self._set_text(
                IDC_NOTE,
                "不套用预设：按自己电费账单上的单价填写。保存后会标记为「自定义」。",
            )
            self._set_text(IDC_SOURCE, "数据来源：用户手动设置")
            return
        bits = []
        if region.effective:
            bits.append(f"执行日期 {region.effective}")
        if region.verify:
            bits.append("⚠ 该地区只有网络汇总口径，建议按电网账单核对")
        lines = ["　·　".join(bits)] if bits else []
        if plan is not None and plan.note:
            lines.append(plan.note)
        self._set_text(IDC_NOTE, "\n".join(lines) or "该方案无附加说明。")
        self._set_text(IDC_SOURCE, f"数据来源：{region.source}")

    # ------------------------------------------------------------- 交互

    def _on_region_changed(self) -> None:
        """换省份：重填方案下拉框，并套用第一个（默认）方案。"""
        idx = self._combo_index(IDC_REGION)
        region = self._region_index[idx] if 0 <= idx < len(self._region_index) else None
        if region is None:
            self._plan_index = [None]
            self._combo_fill(IDC_PLAN, [CUSTOM_PLAN_LABEL], 0)
            self._refresh_note()
            return
        self._plan_index = list(region.plans)
        self._combo_fill(IDC_PLAN, tariffs.plan_labels(region), 0)
        plan = self._selected_plan()
        if plan is not None:
            self._fill_fields_from_plan(plan)
        self._refresh_note()

    def _on_plan_changed(self) -> None:
        plan = self._selected_plan()
        if plan is not None:
            self._fill_fields_from_plan(plan)
        self._refresh_note()

    def _save(self) -> None:
        try:
            peak = _parse_price(self._text(IDC_PEAK), "峰段电价")
            flat = _parse_price(self._text(IDC_FLAT), "平段电价")
            dry = _parse_price(self._text(IDC_VALLEY_DRY), "谷段电价（枯水/常规）")
            wet = _parse_price(self._text(IDC_VALLEY_WET), "谷段电价（丰水期）")
            peak_hours = _parse_hours(self._text(IDC_PEAK_HOURS), "峰段时段")
            valley_hours = _parse_hours(self._text(IDC_VALLEY_HOURS), "谷段时段")
            wet_months = _parse_months(self._text(IDC_WET_MONTHS), "丰水期月份")
            base_w = _parse_num(self._text(IDC_BASE), "其他功耗", 0, 500, " W")
            monitor_w = _parse_num(self._text(IDC_MONITOR), "显示器功耗", 0, 500, " W")
            calib = _parse_num(self._text(IDC_CALIB), "校准系数", 0.5, 2.0)
        except ValueError as exc:
            user32.MessageBoxW(
                self._hwnd, str(exc), "电价设置有误", MB_OK | MB_ICONWARNING
            )
            return

        if peak_hours and valley_hours:
            overlap = tariffs.parse_hours(peak_hours) & tariffs.parse_hours(valley_hours)
            if overlap:
                hours = "、".join(f"{h}:00" for h in sorted(overlap))
                user32.MessageBoxW(
                    self._hwnd,
                    f"峰段与谷段有重叠：{hours}。\n同一小时不能既是峰段又是谷段，请改一下。",
                    "电价设置有误", MB_OK | MB_ICONWARNING,
                )
                return

        cfg = self.cfg
        region = self._selected_region()
        plan = self._selected_plan()
        if region is not None and plan is not None:
            untouched = (
                abs(peak - plan.peak) < 1e-9
                and abs(flat - plan.flat) < 1e-9
                and abs(dry - plan.valley) < 1e-9
                and abs(wet - plan.resolved_valley_wet()) < 1e-9
                and peak_hours == plan.peak_hours
                and valley_hours == plan.valley_hours
                and tuple(wet_months) == tuple(plan.wet_months)
            )
            cfg.tariff_region = region.name
            cfg.tariff_plan = plan.label if untouched else f"{plan.label}（已手动调整）"
            cfg.tariff_source = region.source
            cfg.tariff_effective = region.effective
            cfg.tariff_verify = region.verify
            cfg.tariff_note = plan.note
        else:
            cfg.tariff_region = "自定义"
            cfg.tariff_plan = CUSTOM_PLAN_LABEL
            cfg.tariff_source = "用户手动设置"
            cfg.tariff_effective = ""
            cfg.tariff_verify = False
            cfg.tariff_note = ""

        cfg.price_peak = peak
        cfg.price_flat = flat
        cfg.price_valley_dry = dry
        cfg.price_valley_wet = wet
        cfg.peak_hours = peak_hours
        cfg.valley_hours = valley_hours
        cfg.valley_wet_months = wet_months
        # 显示器填 0 = 不计入（等价于关掉 include_monitor），保留原瓦数备用
        cfg.baseline_watts = base_w
        if monitor_w > 0:
            cfg.monitor_watts = monitor_w
        cfg.include_monitor = monitor_w > 0
        cfg.calibration = calib
        cfg.save()

        _dbg(
            f"已保存 地区={cfg.tariff_region} 方案={cfg.tariff_plan} "
            f"峰={peak} 平={flat} 谷枯={dry} 谷丰={wet} "
            f"峰时段={peak_hours or '（无）'} 谷时段={valley_hours or '（无）'} "
            f"丰水期={wet_months} ｜ 其他={base_w}W 显示器={monitor_w}W "
            f"校准×{calib:.2f}"
        )

        if self._on_saved is not None:
            try:
                self._on_saved()
            except Exception:  # noqa: BLE001 - 回调失败不该拦着窗口关闭
                pass
        self._close()

    def _close(self) -> None:
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)

    # ------------------------------------------------------------- 消息

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_COMMAND:
            cid = wparam & 0xFFFF
            code = (wparam >> 16) & 0xFFFF
            if code == CBN_SELCHANGE and cid == IDC_REGION:
                self._on_region_changed()
                return True, 0
            if code == CBN_SELCHANGE and cid == IDC_PLAN:
                self._on_plan_changed()
                return True, 0
            if code == BN_CLICKED:
                if cid in (IDC_SAVE, IDOK):
                    self._save()
                    return True, 0
                if cid in (IDC_CANCEL, IDCANCEL):
                    self._close()
                    return True, 0
                if cid == IDC_REFILL:
                    self._on_region_changed()
                    return True, 0
            return False, 0

        if msg == DM_GETDEFID:
            # IsDialogMessage 靠这条消息找默认按钮，才能让回车触发「保存」。
            # 普通窗口类不实现它，所以得自己答。
            return True, (DC_HASDEFID << 16) | IDOK

        if msg == WM_CTLCOLORSTATIC:
            gdi32.SetBkMode(wparam, TRANSPARENT)
            return True, self._bg_brush or 0

        if msg == WM_CLOSE:
            self._close()
            return True, 0

        if msg == WM_DESTROY:
            _dialogs.pop(self._hwnd, None)
            self._hwnd = None
            for font in self._fonts.values():
                gdi32.DeleteObject(font)
            self._fonts.clear()
            self._controls.clear()
            return True, 0

        return False, 0

    # ------------------------------------------------------------- 显隐

    def show(self) -> None:
        if not self._hwnd:
            if not self.create():
                return
        user32.ShowWindow(self._hwnd, 5)  # SW_SHOW
        # 与详情面板同样的路子：先临时置顶再取消，绕开系统的前台锁
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(self._hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetForegroundWindow(self._hwnd)
        first = self._controls.get(IDC_REGION)
        if first:
            user32.SetFocus(first)

    def destroy(self) -> None:
        self._close()


__all__ = ["FeeSettingsDialog", "CUSTOM_LABEL"]
