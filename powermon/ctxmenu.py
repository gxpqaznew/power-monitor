"""自绘深色卡片弹出菜单 —— 托盘 / 长条右键菜单的渲染层。

为什么自绘：原生 ``TrackPopupMenu`` 是灰色经典样式，连深色都做不到，更别说
圆角和柔影。参考 codex meter 的 popover：**深色半透明卡片 + 大圆角 + 柔影 +
蓝点白勾 + hover 整条蓝高亮**，右键菜单照着这个气质画。

结构：

  * 入口是 ``show(entries, x, y, on_command)``，``entries`` 由
    ``tray.MenuBuilder`` 在拼原生菜单时顺带记录（app 的菜单构建逻辑一行不用改）；
  * **非模态**：建完窗口立即返回，命令经回调异步分发 —— 不用嵌套消息泵，
    没有 TrackPopupMenu 那种模态重入问题；
  * 子菜单向右叠开（右边不够就翻到左边），父菜单销毁时子菜单作为 owned
    窗口被系统连带销毁；
  * 「点别处关闭」靠 ``WM_KILLFOCUS``：菜单是**可激活**的 popup（不带
    ``WS_EX_NOACTIVATE``），弹出时抢前台；``AttachThreadInput`` 是非前台
    进程抢前台的兜底。托盘图标点击不产生焦点事件，所以托盘收到任何点击
    消息时也要主动 ``close_all()``。

性能：hover 切换**不全量重画**。半透明卡片（``shape_alpha`` < 255）的 alpha
合成是逐像素的，整张十几万像素一跑就是上百毫秒，鼠标划过会明显卡。所以
「无 hover 的整图」只渲染一次缓存起来，hover 变化时把缓存拷进新 DIB、
只补画那一行的高亮条 + 行内容，再对条带那 1 万多像素补一次 alpha ——
一次切换 ~5ms。

渲染底座复用 ``roundwin``：32bpp DIB + SDF 圆角 alpha 合成 +
``UpdateLayeredWindow``，与长条 / 详情面板同一套质感。
"""

from __future__ import annotations

import ctypes
import time
import traceback

from . import debug, frost
from .roundwin import compose_shape_alpha, dib_section, present_layered, round_rect_sdf
from .w32 import (
    CLEARTYPE_QUALITY,
    DEFAULT_CHARSET,
    DT_LEFT,
    DT_NOPREFIX,
    DT_RIGHT,
    DT_SINGLELINE,
    DT_VCENTER,
    NULL_BRUSH,
    NULL_PEN,
    SIZE,
    SW_SHOW,
    TME_LEAVE,
    TRACKMOUSEEVENT,
    TRANSPARENT,
    VK_ESCAPE,
    WM_DESTROY,
    WM_KEYDOWN,
    WM_KILLFOCUS,
    WM_LBUTTONUP,
    WM_MENU_TAKEFOCUS,
    WM_MOUSELEAVE,
    WM_MOUSEMOVE,
    WM_TIMER,
    WNDCLASSEXW,
    WNDPROC,
    WS_EX_LAYERED,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_POPUP,
    gdi32,
    kernel32,
    mouse_button_down,
    user32,
    wintypes,
)

# ------------------------------------------------------------------ 样式
# 固定深色卡片：codex 那种「浮在桌面上」的质感靠的就是不管桌面深浅都一致的
# 深色底 + 柔影，跟着桌面变浅反而会失去分层感。
_BG = (30, 32, 40)
_BG_RGB = (30, 32, 40)     # 毛玻璃的色调层（和 _BG 同色，写成元组给 frost 用）
_BG_FROST = 205            # 毛玻璃浓度：桌面糊掉后还留一点透光，才是真·亚克力
_BG_ALPHA = 234            # 半透明：背后桌面隐隐透出来，卡片才有「浮起」的感觉
_ACCENT = (10, 132, 255)   # hover 条 / 勾选圆点的蓝（macOS system blue）
_INK = (240, 242, 246)     # 主文字
_DIM = (150, 156, 168)     # 分组小标题 / 次级文字
_DISABLED = (112, 118, 130)
_SEP = (56, 60, 70)        # 分隔线

# 尺寸（设计基准像素，×DPI 缩放）
_ITEM_H = 30.0
_LABEL_H = 21.0
_SEP_H = 10.0
_CARD_PAD = 6.0            # 卡片内边距（第一行上方 / 最后一行下方 / 行内容左右）
_ROW_PAD_X = 8.0           # 行内容相对卡片边缘再退一点
_CHECK_W = 24.0            # 勾选位宽
_ARROW_W = 18.0            # 子菜单箭头位宽
_RADIUS = 10.0
_SHADOW = 10.0
_SHADOW_DY = 4.0
_SHADOW_ALPHA = 110
_MIN_W = 200.0
_MAX_W = 440.0
_FONT_SZ = 13.0
_LABEL_SZ = 11.0
_CHECK_D = 15.0            # 勾选圆点直径
_HOVER_INSET = 4.0         # hover 条相对卡片边缘的内缩
_HOVER_RADIUS = 6.0
_SUB_DELAY_MS = 300        # hover 多久展开子菜单

_CLASS_NAME = "PowerMonitorCtxMenu"
_TIMER_SUB = 1

_windows: dict[int, "CtxMenu"] = {}
_chain: list["CtxMenu"] = []          # 当前打开着的菜单（根在前、子孙在后）
_wndproc_ref: WNDPROC | None = None
_closing = False                      # close_all 重入保护（销毁会再触发 KILLFOCUS）

# 刚弹出后多久算「还在落座」。这段时间里收到的 WM_KILLFOCUS 不当「点别处」——
# 打开菜单的那一次点击本身会带得焦点晃一下（explorer 处理完点击可能把前台还给
# 任务栏），同步抢前台时这几乎是必然发生的。实测：关掉详情卡片之后紧接着右键，
# 约 1/3 次菜单会刚出现几十毫秒就消失，用户看到的是「右键弹不出来」。
#
# 🔴 宽限**不是无条件的**：另外还要「此刻没有鼠标键按着」才重抢（见 _on_message
# 里 WM_KILLFOCUS 那段）。用户真去点别处时键是按下的，宽限必须马上让位，
# 否则会变成「落座期内点别处菜单关不掉」—— 把毛病从一头换到另一头。
_POPUP_SETTLE = 0.35
_REFOCUS_TRIES = 3                    # 落座期内最多重抢几次，别和系统死磕


def _dbg(msg: str) -> None:
    debug.log("ctxmenu", msg)


def _colorref(rgb) -> int:
    r, g, b = rgb
    return int(r) | (int(g) << 8) | (int(b) << 16)


# ------------------------------------------------------------------ 布局

_ROW_H = {"item": _ITEM_H, "sub": _ITEM_H, "label": _LABEL_H, "sep": _SEP_H}


def _make_font(size_px: float, bold: bool = False):
    """size_px 是**实际像素**（已含 DPI 缩放）。"""
    return gdi32.CreateFontW(
        -max(1, int(round(size_px))), 0, 0, 0,
        700 if bold else 400,
        0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0,
        "Microsoft YaHei UI",
    )


def _text_width(dc, text: str) -> int:
    size = SIZE()
    gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
    return int(size.cx)


def _clickable(e) -> bool:
    return e.get("type") in ("item", "sub") and e.get("enabled", True)


class _Geom:
    """一张菜单的全部几何信息（渲染与命中共用）。"""

    def __init__(self, entries, rows, shape_w, shape_h, margin, scale):
        self.entries = entries
        self.rows = rows              # [(entry, y, h)]，y 相对窗口客户区
        self.shape_w = shape_w
        self.shape_h = shape_h
        self.margin = margin
        self.scale = scale
        self.pad = int(round(_CARD_PAD * scale))
        self.w = shape_w + margin * 2
        self.h = shape_h + margin * 2

    def bar_rect(self, index: int) -> tuple[int, int, int, int]:
        """hover 条的窗口客户区矩形 (x0, y0, x1, y1)。"""
        _e, ry, rh = self.rows[index]
        inset = int(round(_HOVER_INSET * self.scale))
        return (self.margin + inset,
                self.margin + ry + 1,
                self.margin + self.shape_w - inset,
                self.margin + ry + rh - 1)

    def text_x(self) -> int:
        return self.margin + self.pad + int(round(_CHECK_W * self.scale))

    def right_x(self) -> int:
        return (self.margin + self.shape_w - self.pad
                - int(round(_ROW_PAD_X * self.scale)))

    def row_y0(self, index: int) -> int:
        """行内容的窗口客户区顶 y。"""
        _e, ry, _rh = self.rows[index]
        return self.margin + ry


def _layout(entries, scale: float, dc) -> _Geom:
    """算每行位置与卡片尺寸。``dc`` 只用来量字宽。"""
    font_item = _make_font(_FONT_SZ * scale)
    font_label = _make_font(_LABEL_SZ * scale)
    old = gdi32.SelectObject(dc, font_item)
    rows = []
    y = 0
    need = _MIN_W
    for e in entries:
        t = e.get("type", "item")
        h = int(round(_ROW_H.get(t, _ITEM_H) * scale))
        rows.append((e, h, y))
        y += h
        if t in ("item", "sub"):
            w = _text_width(dc, e.get("text", ""))
            w += _CHECK_W + _ROW_PAD_X * 2
            if t == "sub":
                w += _ARROW_W
            need = max(need, w)
        elif t == "label":
            gdi32.SelectObject(dc, font_label)
            w = _text_width(dc, e.get("text", ""))
            value = e.get("value", "")
            if value:
                w += _text_width(dc, value) + 14
            gdi32.SelectObject(dc, font_item)
            w += _CHECK_W + _ROW_PAD_X * 2
            need = max(need, w)
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(font_item)
    gdi32.DeleteObject(font_label)
    content_w = int(round(min(_MAX_W, need) * scale))
    pad = int(round(_CARD_PAD * scale))
    shape_w = content_w + pad * 2
    shape_h = int(round(y)) + pad * 2
    shadow = int(round(_SHADOW * scale))
    margin = shadow + 2
    # rows 换成 (entry, 行高, 相对卡片的 y) → 存成 (entry, y_client, h)
    client_rows = [
        (e, pad + ry, rh) for e, rh, ry in rows
    ]
    return _Geom(entries, client_rows, shape_w, shape_h, margin, scale)


# ------------------------------------------------------------------ 行内容绘制


class _GdiBag:
    """一把 GDI 资源，退出时统一释放。"""

    def __init__(self) -> None:
        self.brushes: list[int] = []
        self.pens: list[int] = []
        self.fonts: list[int] = []

    def brush(self, rgb) -> int:
        b = gdi32.CreateSolidBrush(_colorref(rgb))
        self.brushes.append(b)
        return b

    def pen(self, rgb, width: int = 1) -> int:
        p = gdi32.CreatePen(0, max(1, int(round(width))), _colorref(rgb))
        self.pens.append(p)
        return p

    def font(self, size_px: float, bold: bool = False) -> int:
        f = _make_font(size_px, bold)
        self.fonts.append(f)
        return f

    def dispose(self) -> None:
        for f in self.fonts:
            gdi32.DeleteObject(f)
        for b in self.brushes:
            gdi32.DeleteObject(b)
        for p in self.pens:
            gdi32.DeleteObject(p)


def _draw_check(dc, bag: _GdiBag, x: int, y0: int, rh: int, scale: float,
                radio: bool, hot: bool) -> None:
    """勾选标记：蓝圆 + 白勾（item）或蓝圆 + 白心点（radio）。

    hover 在蓝条上时圆点改成白底蓝勾 / 白底蓝心，不然蓝圆消失在蓝条里。
    """
    d = int(round(_CHECK_D * scale))
    cx = x + int(round(6 * scale)) + d // 2
    cy = y0 + rh // 2
    gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_PEN))
    gdi32.SelectObject(dc, bag.brush((255, 255, 255) if hot else _ACCENT))
    gdi32.Ellipse(dc, cx - d // 2, cy - d // 2, cx + (d - d // 2), cy + (d - d // 2))

    mark = _ACCENT if hot else (255, 255, 255)
    if radio:
        r = max(2, int(round(2.5 * scale)))
        gdi32.SelectObject(dc, bag.brush(mark))
        gdi32.Ellipse(dc, cx - r, cy - r, cx + r, cy + r)
        return
    pen = bag.pen(mark, 1.8 * scale)
    gdi32.SelectObject(dc, pen)
    gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_BRUSH))
    u = scale
    pts = (wintypes.POINT * 3)(
        wintypes.POINT(int(cx - 3.2 * u), int(cy + 0.2 * u)),
        wintypes.POINT(int(cx - 0.8 * u), int(cy + 2.6 * u)),
        wintypes.POINT(int(cx + 3.6 * u), int(cy - 2.6 * u)),
    )
    gdi32.Polyline(dc, pts, 3)


def _draw_arrow(dc, bag: _GdiBag, x: int, y0: int, rh: int, scale: float,
                rgb) -> None:
    """子菜单箭头：实心小三角。"""
    gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_PEN))
    gdi32.SelectObject(dc, bag.brush(rgb))
    cy = y0 + rh // 2
    hw = int(round(4 * scale))
    hh = int(round(5 * scale))
    pts = (wintypes.POINT * 3)(
        wintypes.POINT(x, cy - hh),
        wintypes.POINT(x, cy + hh),
        wintypes.POINT(x + hw, cy),
    )
    gdi32.Polygon(dc, pts, 3)


def _paint_row(dc, bag: _GdiBag, geom: _Geom, index: int,
               font_item, font_label, hot: bool) -> None:
    """画一行的内容（图标 / 文字 / 箭头）。``hot=True`` 用 hover 配色（白字）。"""
    e, ry, rh = geom.rows[index]
    t = e.get("type", "item")
    y0 = geom.row_y0(index)
    text_x = geom.text_x()
    right_x = geom.right_x()
    scale = geom.scale

    if t == "sep":
        line = wintypes.RECT(text_x - int(round(6 * scale)), y0 + rh // 2,
                             right_x, y0 + rh // 2 + 1)
        user32.FillRect(dc, ctypes.byref(line), bag.brush(_SEP))
        return

    if t == "label":
        gdi32.SelectObject(dc, font_label)
        gdi32.SetTextColor(dc, _colorref(_DIM))
        r = wintypes.RECT(text_x, y0, right_x, y0 + rh)
        user32.DrawTextW(dc, e.get("text", ""), -1, ctypes.byref(r),
                         DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
        value = e.get("value", "")
        if value:
            user32.DrawTextW(dc, value, -1, ctypes.byref(r),
                             DT_RIGHT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
        gdi32.SelectObject(dc, font_item)
        return

    enabled = e.get("enabled", True)
    ink = (255, 255, 255) if hot else (_INK if enabled else _DISABLED)

    if e.get("checked"):
        _draw_check(dc, bag, geom.margin + geom.pad + int(round(4 * scale)),
                    y0, rh, scale, radio=e.get("radio", False), hot=hot)

    gdi32.SetTextColor(dc, _colorref(ink))
    text_right = right_x - (int(round(_ARROW_W * scale)) if t == "sub" else 0)
    r = wintypes.RECT(text_x, y0, text_right, y0 + rh)
    user32.DrawTextW(dc, e.get("text", ""), -1, ctypes.byref(r),
                     DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
    if t == "sub":
        _draw_arrow(dc, bag, right_x + int(round(4 * scale)), y0, rh, scale,
                    (255, 255, 255) if hot else _DIM)


def _draw_hover_bar(dc, bag: _GdiBag, geom: _Geom, index: int) -> None:
    """hover 高亮条：圆角蓝条（不透明）。"""
    x0, y0, x1, y1 = geom.bar_rect(index)
    rad = int(round(_HOVER_RADIUS * geom.scale))
    gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_PEN))
    gdi32.SelectObject(dc, bag.brush(_ACCENT))
    gdi32.RoundRect(dc, x0, y0, x1, y1, rad * 2, rad * 2)


# ------------------------------------------------------------------ 整图渲染


def render_base(entries, scale: float, place=None):
    """渲染「无 hover」的整张菜单。返回 ``(bgra_bytes, geom)``，失败返回 (None, None)。

    ``place(w, h)`` 是可选的「定位置」回调：毛玻璃要抓**卡片背后那块屏幕**，
    所以位置必须在铺底之前就定下来。传了它就一定会在铺底前被调用一次（调用方
    用它把算出来的 ``(x, y)`` 存起来，免得再算一遍）；不传就退化成实色底
    （离屏测试走这条路 —— 离屏没有真实窗口，抓屏没有意义）。
    """
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    user32.ReleaseDC(None, screen)

    geom = _layout(entries, scale, dc)
    at = place(geom.w, geom.h) if place is not None else None
    bmp, view = dib_section(dc, geom.w, geom.h)
    if not bmp:
        gdi32.DeleteDC(dc)
        return None, None
    old_bmp = gdi32.SelectObject(dc, bmp)
    bag = _GdiBag()
    font_item = bag.font(_FONT_SZ * scale)
    font_label = bag.font(_LABEL_SZ * scale)
    try:
        # 画布清零（新 DIB 内存是脏的），再铺卡片底色
        r_all = wintypes.RECT(0, 0, geom.w, geom.h)
        user32.FillRect(dc, ctypes.byref(r_all), bag.brush((0, 0, 0)))
        r_card = wintypes.RECT(geom.margin, geom.margin,
                               geom.margin + geom.shape_w,
                               geom.margin + geom.shape_h)
        # 实色底先铺：毛玻璃抓不到（离屏 / 屏幕被挡）时它就是最终底色
        user32.FillRect(dc, ctypes.byref(r_card), bag.brush(_BG))
        if at is not None:
            # 窗口这会儿还没 CreateWindowEx / ShowWindow，抓进来的是干净的桌面，
            # 不会把菜单自己上一帧糊进去 —— 所以这里不需要 hide_hwnd。
            frost.blit(dc, "ctxmenu",
                       at[0] + geom.margin, at[1] + geom.margin,
                       geom.margin, geom.margin,
                       geom.shape_w, geom.shape_h,
                       _BG_RGB, _BG_FROST)

        gdi32.SetBkMode(dc, TRANSPARENT)
        gdi32.SelectObject(dc, font_item)
        for i in range(len(geom.rows)):
            _paint_row(dc, bag, geom, i, font_item, font_label, hot=False)

        compose_shape_alpha(
            view, geom.w, geom.h, geom.margin, _RADIUS * scale,
            geom.shape_w, geom.shape_h,
            shadow=int(round(_SHADOW * scale)), shadow_rgb=(8, 10, 16),
            shadow_alpha=int(_SHADOW_ALPHA),
            shadow_dy=int(round(_SHADOW_DY * scale)),
            shape_alpha=_BG_ALPHA,
        )
        data = bytes(view)
    finally:
        gdi32.SelectObject(dc, old_bmp)
        bag.dispose()
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
    return data, geom


def with_hover(base: bytes, geom: _Geom, hover: int):
    """在缓存的底图上叠加 hover 条。返回整图 bytes；hover < 0 时原样返回 base。

    只重画那一行 + 只对条带区域补 alpha（约一万像素），是 hover 切换不卡的
    关键 —— 全量重画半透明卡片要跑十几万次逐像素合成。
    """
    if hover < 0 or hover >= len(geom.rows) or not _clickable(geom.rows[hover][0]):
        return base
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    user32.ReleaseDC(None, screen)
    bmp, view = dib_section(dc, geom.w, geom.h)
    if not bmp:
        gdi32.DeleteDC(dc)
        return base
    ctypes.memmove(view, base, len(base))
    old_bmp = gdi32.SelectObject(dc, bmp)
    bag = _GdiBag()
    font_item = bag.font(_FONT_SZ * geom.scale)
    font_label = bag.font(_LABEL_SZ * geom.scale)
    try:
        gdi32.SetBkMode(dc, TRANSPARENT)
        gdi32.SelectObject(dc, font_item)
        _draw_hover_bar(dc, bag, geom, hover)
        _paint_row(dc, bag, geom, hover, font_item, font_label, hot=True)

        # GDI 画过的区域 alpha 被清零了 —— 对 hover 条（圆角矩形）重补：
        # 内部不透明（hover 条是实心的），边缘 1px 按 SDF 渐变并预乘。
        x0, y0, x1, y1 = geom.bar_rect(hover)
        rad = _HOVER_RADIUS * geom.scale
        bw, bh = x1 - x0, y1 - y0
        for y in range(max(0, y0 - 1), min(geom.h, y1 + 1)):
            py = y + 0.5 - y0
            for x in range(max(0, x0 - 1), min(geom.w, x1 + 1)):
                d = round_rect_sdf(x + 0.5 - x0, py, bw, bh, rad)
                idx = (y * geom.w + x) * 4
                if d <= -0.5:
                    view[idx + 3] = 255
                elif d < 0.5:
                    a = int(255 * (0.5 - d))
                    k = a / 255.0
                    view[idx] = int(view[idx] * k)
                    view[idx + 1] = int(view[idx + 1] * k)
                    view[idx + 2] = int(view[idx + 2] * k)
                    view[idx + 3] = a
        data = bytes(view)
    finally:
        gdi32.SelectObject(dc, old_bmp)
        bag.dispose()
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
    return data


def render(entries, scale: float, hover: int = -1):
    """一步到位渲染（测试 / 离线用）。返回 ``(bytes, w, h, rows, margin)``。"""
    base, geom = render_base(entries, scale)
    if base is None:
        return None, 0, 0, [], 0
    data = with_hover(base, geom, hover)
    return data, geom.w, geom.h, geom.rows, geom.margin


# ------------------------------------------------------------------ 窗口


@WNDPROC
def _menu_proc(hwnd, msg, wparam, lparam):
    menu = _windows.get(hwnd)
    if menu is not None:
        try:
            handled, result = menu._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里绝不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_class() -> bool:
    global _wndproc_ref
    _wndproc_ref = _menu_proc
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _menu_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = _CLASS_NAME
    if user32.RegisterClassExW(ctypes.byref(wc)):
        return True
    return ctypes.get_last_error() == 1410


def _workarea() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # GETWORKAREA
        return (r.left, r.top, r.right, r.bottom)
    return (0, 0, 2560, 1440)


def _force_foreground(hwnd) -> None:
    """让菜单拿到前台 + 焦点（收 KILLFOCUS 的前提）。

    非前台进程直接 SetForegroundWindow 会被系统拒（菜单不算前台），
    AttachThreadInput 附着到当前前台线程是标准兜底。
    """
    fore = user32.GetForegroundWindow()
    tid_cur = kernel32.GetCurrentThreadId()
    tid_fore = user32.GetWindowThreadProcessId(fore, None) if fore else 0
    if fore and tid_fore and tid_fore != tid_cur:
        user32.AttachThreadInput(tid_cur, tid_fore, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(tid_cur, tid_fore, False)
    else:
        user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)


def close_all() -> None:
    """关掉整条菜单链（如果有）。"""
    global _closing
    if _closing:
        return
    _closing = True
    try:
        roots = [m for m in list(_chain) if m.parent is None]
        for r in roots:
            r._destroy_chain()
        _chain.clear()
    finally:
        _closing = False


def show(entries, x: int, y: int, on_command,
         scale: float | None = None) -> "CtxMenu | None":
    """在屏幕 (x, y) 处弹出一级菜单（先关掉还在开着的旧菜单）。

    ``on_command(cmd)`` 在用户点中某个可点项后被调用；菜单会在调用**之前**
    全部关闭，回调里可以放心立刻再弹新菜单（「点完不关」靠这个）。
    """
    close_all()
    if not entries:
        _dbg("show 放弃：entries 为空")
        return None
    if not _ensure_class():
        _dbg(f"show 放弃：注册窗口类失败 err={ctypes.get_last_error()}")
        return None
    # 这一路是**窗口过程里同步调用**的，异常会被 _wnd_proc 的 except 吞掉，
    # 表现成「右键没反应」，从外面完全看不出原因 —— 所以这里必须自己记一笔。
    try:
        menu = CtxMenu(entries, (x, y), on_command, scale=scale, is_sub=False)
    except Exception as exc:  # noqa: BLE001
        _dbg(f"show 抛异常：{exc!r}")
        for line in traceback.format_exc().splitlines():
            _dbg(f"  | {line}")
        return None
    if not menu.hwnd:
        _dbg("show 返回了菜单对象但 hwnd 为空")
        return None
    return menu


class CtxMenu:
    """一级弹出菜单（子菜单也是它，parent 非空）。"""

    def __init__(self, entries, origin, on_command,
                 scale: float | None = None, parent: "CtxMenu | None" = None,
                 is_sub: bool = True) -> None:
        self.entries = entries
        self.on_command = on_command
        self.parent = parent
        self.child: CtxMenu | None = None
        self._parent_index = -1
        self.hover = -1
        self._leave_tracked = False
        self._timer_on = False
        self._hwnd = None
        self._scale = scale or self._taskbar_scale()
        self._is_sub = is_sub
        self._geom = None
        self._base = None
        # 抢前台的时刻 + 已重抢次数（落座期宽限用，见 _POPUP_SETTLE）
        self._focus_at = 0.0
        self._refocus_tries = 0
        # 弹出这一刻有没有鼠标键已经按着。两条弹菜单的路都是在 BUTTONUP 上弹的
        # （托盘 WM_RBUTTONUP / 长条自接 WM_RBUTTONUP），所以这里正常恒为 False。
        # 留这个记录是为了堵一类将来才可能出现的误判：万一哪条路改成「按下就弹」，
        # 落座期内那次「假 KILLFOCUS」就会带着按下的键，被当成用户点了别处秒关。
        self._btn_at_show = mouse_button_down()

        # 先定位置、再渲染：毛玻璃要抓卡片背后那块屏幕，铺底之前得知道卡片落在哪。
        # ``_place`` 只认 geom 的宽高 + origin（子菜单那支还读父菜单的 geom，那个
        # 早就有了），所以在这里回调是安全的。
        box: dict = {}

        def _resolve(w: int, h: int):
            at = self._place(origin, w, h)
            box["at"] = at
            return at

        base, geom = render_base(entries, self._scale, place=_resolve)
        if base is None:
            return
        self._base = base
        self._geom = geom

        x, y = box["at"]
        self._pos = (x, y)
        owner = parent.hwnd if parent is not None else None
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            _CLASS_NAME, "PowerMonitorMenu", WS_POPUP,
            x, y, geom.w, geom.h, owner, None,
            kernel32.GetModuleHandleW(None), None,
        )
        if not self._hwnd:
            _dbg(f"CreateWindowEx 失败 err={ctypes.get_last_error()}")
            return
        _windows[self._hwnd] = self
        _chain.append(self)
        self._redraw()
        user32.ShowWindow(self._hwnd, SW_SHOW)
        # 🔴 抢前台**推到当前消息处理完之后**再做，别同步抢。
        #
        # ``show()`` 是在调用方的窗口过程里同步跑的：托盘那条路是 explorer 用
        # ``SendMessage`` 把点击转过来，此刻 explorer **还没处理完这次点击** ——
        # 它返回后会把前台还给任务栏，于是我们刚抢到手的前台被顶掉，菜单立刻收到
        # ``WM_KILLFOCUS`` 被关掉。
        #
        # 实测（``_menuflakeprobe.py``，20 轮）：
        #   * 直接右键托盘图标          → 20/20 正常（同步抢也来得及）；
        #   * 关掉详情卡片之后紧接着右键 → 约 1/3 次菜单**刚出现几十毫秒就消失**，
        #     用户看到的就是「右键弹不出来」。
        # 推后一条消息再做就轮到最后抢，站稳。配合 ``_POPUP_SETTLE`` 的落座宽限，
        # 这条次序也能 20/20。
        user32.PostMessageW(self._hwnd, WM_MENU_TAKEFOCUS, 0, 0)
        _dbg(f"弹出 rect=({x},{y},{geom.w},{geom.h}) 项数={len(entries)} sub={is_sub}")

    # ------------------------------------------------------------- 基础

    @staticmethod
    def _taskbar_scale() -> float:
        from . import taskbar
        info = taskbar.taskbar()
        if info is None:
            return 1.0
        return max(0.5, info[2] / 96.0)

    @property
    def hwnd(self):
        return self._hwnd

    def _place(self, origin, w: int, h: int) -> tuple[int, int]:
        """定位。根菜单：底边贴 origin（向上弹）；子菜单：向右展开，不够就翻左。"""
        wa = _workarea()
        ox, oy = origin
        if self._is_sub:
            x = ox
            if x + w > wa[2] and self.parent is not None:
                # 翻到父菜单左边：子的右缘贴父卡片左缘
                x = self.parent._pos[0] + self.parent._geom.margin - w + 2
            y = oy
            if y + h > wa[3]:
                y = max(wa[1], wa[3] - h)
            return int(x), int(y)
        x = ox
        if x + w > wa[2]:
            x = max(wa[0], ox - w)
        y = oy - h
        if y < wa[1]:
            y = min(oy, wa[3] - h)
        return int(x), int(y)

    def _present(self, data: bytes) -> None:
        geom = self._geom
        screen = user32.GetDC(None)
        dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmp, view = dib_section(dc, geom.w, geom.h)
        if not bmp:
            gdi32.DeleteDC(dc)
            return
        ctypes.memmove(view, data, len(data))
        # 必须先把位图选进 DC 再 present —— UpdateLayeredWindow 读的是 DC 里
        # 当前选中的位图，顺序反了它读到的就是默认那块 1x1 单色位图（整窗透明）。
        old = gdi32.SelectObject(dc, bmp)
        ok = present_layered(self._hwnd, dc, geom.w, geom.h,
                             self._pos[0], self._pos[1])
        if not ok:
            _dbg(f"UpdateLayeredWindow 失败 err={ctypes.get_last_error()}")
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)

    def _redraw(self) -> None:
        if not self._hwnd or self._base is None:
            return
        data = with_hover(self._base, self._geom, self.hover)
        self._present(data)

    def _destroy(self) -> None:
        if self._timer_on and self._hwnd:
            self._timer_on = False
            user32.KillTimer(self._hwnd, _TIMER_SUB)
        if self._hwnd:
            _windows.pop(self._hwnd, None)
            hwnd = self._hwnd
            self._hwnd = None
            user32.DestroyWindow(hwnd)   # owned 子菜单会被系统连带销毁

    def _destroy_chain(self) -> None:
        if self.child is not None:
            child = self.child
            self.child = None
            child._destroy_chain()
        if self in _chain:
            _chain.remove(self)
        self._destroy()

    # ------------------------------------------------------------- 命中

    def _hit(self, y: int) -> int:
        # rows 里的 ry 是相对**卡片**的，消息的 y 是窗口客户区坐标 ——
        # 差一个 margin（阴影边），不补上的话命中永远偏上十几像素。
        cy = y - self._geom.margin
        for i, (e, ry, rh) in enumerate(self._geom.rows):
            if ry <= cy < ry + rh:
                if e.get("type") in ("item", "sub"):
                    return i
                return -1
        return -1

    def _set_hover(self, index: int) -> None:
        if index == self.hover:
            return
        self.hover = index
        self._redraw()
        # 子菜单联动：hover 移走就关旧子菜单；移到带子菜单的项就延时展开
        if self.child is not None and self.child._parent_index != index:
            child = self.child
            self.child = None
            child._destroy_chain()
        if self._timer_on:
            self._timer_on = False
            user32.KillTimer(self._hwnd, _TIMER_SUB)
        if 0 <= index < len(self.entries) \
                and self.entries[index].get("type") == "sub" \
                and (self.child is None or self.child._parent_index != index):
            self._timer_on = True
            user32.SetTimer(self._hwnd, _TIMER_SUB, _SUB_DELAY_MS, None)

    def _open_child(self, index: int) -> None:
        if not (0 <= index < len(self.entries)):
            return
        e = self.entries[index]
        if e.get("type") != "sub":
            return
        if self.child is not None and self.child._parent_index == index:
            return
        if self.child is not None:
            child = self.child
            self.child = None
            child._destroy_chain()
        sub_entries = e.get("entries") or []
        if not sub_entries:
            return
        _e, ry, _rh = self._geom.rows[index]
        # 原点：卡片右缘；让子菜单的**第一行**与父项该行对齐 ——
        # 子窗口 y + margin + pad = 父行顶（屏幕），反解出 y。
        ox = self._pos[0] + self._geom.w - self._geom.margin - 2
        oy = self._pos[1] + self._geom.margin + ry \
            - self._geom.margin - self._geom.pad
        child = CtxMenu(sub_entries, (ox, oy), self.on_command,
                        scale=self._scale, parent=self, is_sub=True)
        if child.hwnd:
            child._parent_index = index
            self.child = child

    # ------------------------------------------------------------- 消息

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_MENU_TAKEFOCUS:
            # 落座：把前台抢到手，并记下时刻（KILLFOCUS 的宽限从这一刻算起）
            if self._hwnd and user32.IsWindow(self._hwnd):
                _force_foreground(self._hwnd)
                self._focus_at = time.monotonic()
            return True, 0

        if msg == WM_DESTROY:
            if self in _chain:
                _chain.remove(self)
            _windows.pop(self._hwnd, None)
            self._hwnd = None
            return True, 0

        if msg == WM_MOUSEMOVE:
            if not self._leave_tracked:
                tme = TRACKMOUSEEVENT()
                tme.cbSize = ctypes.sizeof(TRACKMOUSEEVENT)
                tme.dwFlags = TME_LEAVE
                tme.hwndTrack = self._hwnd
                if user32.TrackMouseEvent(ctypes.byref(tme)):
                    self._leave_tracked = True
            y = (int(lparam) >> 16) & 0xFFFF
            if y >= 0x8000:
                y -= 0x10000
            self._set_hover(self._hit(y))
            return True, 0

        if msg == WM_MOUSELEAVE:
            self._leave_tracked = False
            # 光标可能移进了子菜单：属于链上窗口就不算「离开菜单」
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            over = user32.WindowFromPoint(pt)
            if over not in _windows:
                self._set_hover(-1)
                if self.child is not None:
                    child = self.child
                    self.child = None
                    child._destroy_chain()
            return True, 0

        if msg == WM_LBUTTONUP:
            y = (int(lparam) >> 16) & 0xFFFF
            if y >= 0x8000:
                y -= 0x10000
            index = self._hit(y)
            if index >= 0:
                self._activate(index)
            return True, 0

        if msg == WM_TIMER:
            if self._timer_on:
                self._timer_on = False
                user32.KillTimer(self._hwnd, _TIMER_SUB)
            if 0 <= self.hover < len(self.entries):
                self._open_child(self.hover)
            return True, 0

        if msg == WM_KEYDOWN:
            if int(wparam) == VK_ESCAPE:
                close_all()
            return True, 0

        if msg == WM_KILLFOCUS:
            # 焦点去了链上别的窗口（比如刚展开的子菜单）不算「点别处」
            if not _closing:
                focus = user32.GetFocus()
                if focus not in _windows:
                    # 落座期内（刚弹出这几百毫秒）收到的 KILLFOCUS 多半是
                    # 「打开菜单的那次点击」把焦点带得晃了一下，不是用户点了别处。
                    # 直接关掉的话用户看到的是「右键弹不出来」—— 重抢一次前台再说，
                    # 抢不回来（或超出宽限）才真的当成「点别处」关掉。
                    #
                    # 🔴 宽限必须让位于「用户真的按了鼠标」。判据是**此刻有没有键按着**：
                    # 用户点别处时，激活变更由那一下按下触发，KILLFOCUS 是在按下
                    # 那条消息里同步送来的，读 GetAsyncKeyState 一定是按下；
                    # 而 explorer 处理完点击、把前台还给任务栏那一下早松手了。
                    # 少了这一条，宽限期内用户点别处菜单会「抢回来」，看着像关不掉。
                    # （弹出那一刻就已经按着的键不算「这一次点击」，见 _btn_at_show。）
                    settling = (self._focus_at
                                and time.monotonic() - self._focus_at <= _POPUP_SETTLE)
                    user_clicked = mouse_button_down() and not self._btn_at_show
                    if settling and not user_clicked and self._refocus_tries < _REFOCUS_TRIES:
                        self._refocus_tries += 1
                        _dbg(f"落座期收到 KILLFOCUS（无键按下），"
                             f"重抢前台（第 {self._refocus_tries} 次）")
                        if self._hwnd and user32.IsWindow(self._hwnd):
                            _force_foreground(self._hwnd)
                    else:
                        if settling and user_clicked:
                            _dbg("落座期收到 KILLFOCUS，但鼠标键按着 → 当点别处，关")
                        close_all()
            return True, 0

        return False, 0

    def _activate(self, index: int) -> None:
        e = self.entries[index]
        t = e.get("type")
        if t == "sub":
            self._open_child(index)
            return
        if t != "item" or not e.get("enabled", True):
            return
        cmd = int(e.get("cmd", 0))
        cb = self.on_command
        # 先全部关掉再回调：回调里可能立刻再弹（「点完不关」），
        # 顺序反了会把新弹出来的菜单一起关掉。
        close_all()
        try:
            cb(cmd)
        except Exception:  # noqa: BLE001 - 命令失败不该带崩消息循环
            pass
