"""毛玻璃底（acrylic）：抓屏幕 → 糊掉 → 叠色调。全走系统 DLL，没有 Python 逐像素。

为什么自己糊而不是用系统的 ``DWMWA_SYSTEMBACKDROP_TYPE``：三块 UI 里长条和菜单
都是**分层窗口**，系统材质对分层窗口无效（DWM 不管 WS_EX_LAYERED 的窗口）；
面板虽然是普通窗口，但 Win11 之前没有这套 API。所以统一自己抓一次背后的屏幕
内容、糊掉、再叠一层色调 —— 三处质感才能完全一样。

糊的做法（**缩小用 GDI 的块平均，放大必须用 GDI+ 双线性**）：

  1. 把窗口矩形背后的屏幕内容 BitBlt 下来；
  2. ``StretchBlt`` + ``HALFTONE`` 缩到 1/8 —— HALFTONE 在**缩小**时对每个目标
     像素做源块平均，正好就是一次半径 8px 的方框模糊；
  3. 再放大回原尺寸 —— ⚠️ **这一步不能用 StretchBlt**：实测 HALFTONE 只对
     缩小做块平均，**放大的时候退化成最近邻复制**（出一个个 8px 方块，看着像
     马赛克不像毛玻璃）。所以放大改走 GDI+ 的
     ``InterpolationModeHighQualityBilinear``；
  4. 整条再走一遍（缩小 + 放大），等效半径加倍；
  5. ``AlphaBlend`` 叠一层实色（色调 + 浓度），再铺一层极淡的噪点
     （真·亚克力是有颗粒的，纯色糊出来会显得「塑料」）。

**自拍陷阱**：抓屏会把窗口自己上一帧的内容也抓进来（模糊会逐帧累积成一团糊）。
调用方要么在窗口还没画过东西时抓（菜单：ShowWindow 之前），要么先藏起来再抓。
``hide_hwnd`` 参数就是干这个的。代价是藏/显会闪一下 —— 所以结果**按 key 缓存**，
只有 key 变了才重新抓（长条拖动 / 缩放时不重抓，任务栏本来就是均匀一条）。

缓存上限六张，FIFO 淘汰；``invalidate(prefix)`` 手动失效。
"""

from __future__ import annotations

import ctypes

from .w32 import (
    AC_SRC_OVER,
    BLENDFUNCTION,
    gdi32,
    msimg32,
    user32,
    wintypes,
)

__all__ = [
    "blit",
    "blur_into",
    "invalidate",
    "reset",
    "has",
    "available",
    "MAX_ENTRIES",
]

_SRCCOPY = 0x00CC0020
_HALFTONE = 4
_SW_HIDE = 0
_SW_SHOWNA = 8          # 显示但不激活：抓完底把窗口放回去，别抢焦点
_NOISE = 96             # 噪点图边长（一次性生成后平铺）
_NOISE_ALPHA = 12       # 噪点浓度（再浓就成了脏）
_DOWNSCALE = 8          # 缩小倍数（= 模糊半径）
MAX_ENTRIES = 6

gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetStretchBltMode.restype = ctypes.c_int
gdi32.StretchBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD,
]
gdi32.StretchBlt.restype = wintypes.BOOL
msimg32.AlphaBlend.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    BLENDFUNCTION,
]
msimg32.AlphaBlend.restype = wintypes.BOOL


# ================================================================ GDI+ 放大
#
# GDI 的 StretchBlt 放大不插值（见模块开头）。GDI+ 的 Graphics 给一个 HDC 就能
# 按双线性把图放上去。gdiplus.dll 是系统组件（XP 起就有），不用额外依赖；
# 万一加载不到就 _gp = False，调用方退回 StretchBlt（质量降级但不崩）。

_INTERP_HQ_BILINEAR = 7     # InterpolationModeHighQualityBilinear
_PIXEL_OFFSET_HALF = 4      # PixelOffsetModeHalf
_FLUSH_SYNC = 1             # FlushIntentionSync


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


_gp = None                  # None=还没试过；False=加载失败；否则是 dll 对象
_gp_token = ctypes.c_ulong(0)


def _gdiplus():
    """懒加载 + 初始化 GDI+，失败返回 None。"""
    global _gp, _gp_token
    if _gp is not None:
        return _gp or None
    try:
        lib = ctypes.WinDLL("gdiplus", use_last_error=True)
        lib.GdiplusStartup.argtypes = [
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(_GdiplusStartupInput),
            ctypes.c_void_p,
        ]
        lib.GdiplusStartup.restype = ctypes.c_int
        lib.GdipCreateFromHDC.argtypes = [wintypes.HDC, ctypes.POINTER(ctypes.c_void_p)]
        lib.GdipCreateFromHDC.restype = ctypes.c_int
        lib.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipSetInterpolationMode.restype = ctypes.c_int
        lib.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipSetPixelOffsetMode.restype = ctypes.c_int
        lib.GdipCreateBitmapFromHBITMAP.argtypes = [
            wintypes.HBITMAP, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipCreateBitmapFromHBITMAP.restype = ctypes.c_int
        lib.GdipDrawImageRectI.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.GdipDrawImageRectI.restype = ctypes.c_int
        lib.GdipFlush.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipFlush.restype = ctypes.c_int
        lib.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        lib.GdipDisposeImage.restype = ctypes.c_int
        lib.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        lib.GdipDeleteGraphics.restype = ctypes.c_int
        # ---- 图片解码（给长条的背景图用，见 images.py）----
        # GDI 自带的 LoadImage 只认 BMP/ICO，PNG/JPG/WebP 都得走 GDI+。
        # 这里顺手把那条链的函数也注册上，省得 images 那边再开关一次 GDI+。
        lib.GdipCreateBitmapFromFile.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipCreateBitmapFromFile.restype = ctypes.c_int
        lib.GdipGetImageWidth.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.GdipGetImageWidth.restype = ctypes.c_int
        lib.GdipGetImageHeight.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
        ]
        lib.GdipGetImageHeight.restype = ctypes.c_int
        lib.GdipCreateHBITMAPFromBitmap.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.HBITMAP), ctypes.c_uint,
        ]
        lib.GdipCreateHBITMAPFromBitmap.restype = ctypes.c_int
        token = ctypes.c_ulong(0)
        inp = _GdiplusStartupInput(1, None, False, False)
        if lib.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) != 0:
            _gp = False
        else:
            _gp_token = token
            _gp = lib
    except Exception:
        _gp = False
    return _gp or None


def gdiplus():
    """给别的模块用的 GDI+ 句柄（``None`` = 这台机器上没有 / 起不来）。

    ``images.py`` 加载 PNG/JPG 背景图要用它 —— GDI+ 在这里统一初始化一次，
    别的地方不要再 ``GdiplusStartup`` 一遍（重复初始化要配套 Shutdown，
    忘了就在进程退出时留下一个悬着的 token）。
    """
    return _gdiplus()


def available() -> bool:
    """GDI+ 可用？（不可用时放大退化成最近邻，只是糊得更方块）"""
    return _gdiplus() is not None


def _magnify(dst_dc, dw: int, dh: int, src_bmp) -> bool:
    """把 ``src_bmp`` 双线性放大到 (dw, dh) 画进 ``dst_dc``。"""
    gp = _gdiplus()
    if gp is None or not src_bmp or dw <= 0 or dh <= 0:
        return False
    g = ctypes.c_void_p()
    if gp.GdipCreateFromHDC(dst_dc, ctypes.byref(g)) != 0 or not g.value:
        return False
    img = ctypes.c_void_p()
    ok = False
    try:
        gp.GdipSetInterpolationMode(g, _INTERP_HQ_BILINEAR)
        gp.GdipSetPixelOffsetMode(g, _PIXEL_OFFSET_HALF)
        if gp.GdipCreateBitmapFromHBITMAP(src_bmp, None, ctypes.byref(img)) == 0:
            gp.GdipDrawImageRectI(g, img, 0, 0, int(dw), int(dh))
            # GDI+ 和 GDI 混用同一个 DC：换手之前必须 flush，否则后续 GDI
            # BitBlt 可能读到还没落盘的内容
            gp.GdipFlush(g, _FLUSH_SYNC)
            ok = True
    finally:
        if img.value:
            gp.GdipDisposeImage(img)
        gp.GdipDeleteGraphics(g)
    return ok


# ================================================================ 内存 DC

class _Entry:
    __slots__ = ("dc", "bmp", "old", "w", "h", "sig")

    def __init__(self, dc, bmp, old, w, h, sig):
        self.dc = dc
        self.bmp = bmp
        self.old = old
        self.w = w
        self.h = h
        self.sig = sig

    def dispose(self) -> None:
        if self.dc:
            if self.old:
                gdi32.SelectObject(self.dc, self.old)
            gdi32.DeleteDC(self.dc)
        if self.bmp:
            gdi32.DeleteObject(self.bmp)
        self.dc = self.bmp = self.old = None


_entries: dict[str, _Entry] = {}
_order: list[str] = []
_noise_dc = None
_noise_bmp = None
_noise_old = None


def _colorref(rgb) -> int:
    r, g, b = rgb
    return int(r) | (int(g) << 8) | (int(b) << 16)


def _dib(dc, w: int, h: int):
    """建一张 32bpp DIB，返回 ``(HBITMAP, ctypes 数组视图)``。"""
    from .roundwin import dib_section
    return dib_section(dc, w, h)


# ------------------------------------------------------------------ 噪点

def _ensure_noise() -> int | None:
    """生成一张 96×96 的细噪点图（半透明，用来平铺）。

    用确定性的伪随机（LCG）而不是 ``random`` —— 每次启动颗粒位置一样，截图
    对比 / 像素断言才可复现。
    """
    global _noise_dc, _noise_bmp, _noise_old
    if _noise_dc:
        return _noise_dc
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    user32.ReleaseDC(None, screen)
    if not dc:
        return None
    bmp, view = _dib(dc, _NOISE, _NOISE)
    if not bmp:
        gdi32.DeleteDC(dc)
        return None
    seed = 20240916
    for i in range(_NOISE * _NOISE):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        v = (seed >> 16) & 0x1F           # 0..31，颗粒很淡
        view[i * 4] = v
        view[i * 4 + 1] = v
        view[i * 4 + 2] = v
        view[i * 4 + 3] = 255
    _noise_old = gdi32.SelectObject(dc, bmp)
    _noise_bmp = bmp
    _noise_dc = dc
    return dc


# ------------------------------------------------------------------ 抓取

def _capture(sx: int, sy: int, w: int, h: int, hide_hwnd=None):
    """抓屏幕 (sx, sy, w, h)。返回内存 DC（含已选入的位图），失败返回 None。"""
    if w <= 0 or h <= 0:
        return None
    screen = user32.GetDC(None)
    if not screen:
        return None
    cap_dc = gdi32.CreateCompatibleDC(screen)
    cap_bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    if not cap_dc or not cap_bmp:
        if cap_dc:
            gdi32.DeleteDC(cap_dc)
        if cap_bmp:
            gdi32.DeleteObject(cap_bmp)
        user32.ReleaseDC(None, screen)
        return None
    cap_old = gdi32.SelectObject(cap_dc, cap_bmp)
    hidden = False
    if hide_hwnd and user32.IsWindowVisible(hide_hwnd):
        user32.ShowWindow(hide_hwnd, _SW_HIDE)
        hidden = True
    ok = gdi32.BitBlt(cap_dc, 0, 0, w, h, screen, sx, sy, _SRCCOPY)
    if hidden:
        user32.ShowWindow(hide_hwnd, _SW_SHOWNA)
    user32.ReleaseDC(None, screen)
    if not ok:
        gdi32.SelectObject(cap_dc, cap_old)
        gdi32.DeleteObject(cap_bmp)
        gdi32.DeleteDC(cap_dc)
        return None
    return _Entry(cap_dc, cap_bmp, cap_old, w, h, "")


def _stretch(dst_dc, dst_w: int, dst_h: int, src_dc, src_w: int, src_h: int,
             src_bmp=None) -> bool:
    """缩放拷贝。

    **缩小**走 GDI 的 ``HALFTONE``（对每个目标像素做源块平均 = 方框模糊）；
    **放大**必须走 GDI+ 双线性 —— HALFTONE 放大时退化成最近邻，出方块。
    """
    if dst_w >= src_w and dst_h >= src_h and _magnify(dst_dc, dst_w, dst_h, src_bmp):
        return True
    old = gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
    ok = gdi32.StretchBlt(dst_dc, 0, 0, dst_w, dst_h,
                          src_dc, 0, 0, src_w, src_h, _SRCCOPY)
    gdi32.SetStretchBltMode(dst_dc, old)
    return bool(ok)


def _temp_dc(w: int, h: int):
    screen = user32.GetDC(None)
    if not screen:
        return None
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h) if dc else None
    user32.ReleaseDC(None, screen)
    if not dc:
        return None
    if not bmp:
        gdi32.DeleteDC(dc)
        return None
    old = gdi32.SelectObject(dc, bmp)
    return _Entry(dc, bmp, old, w, h, "")


def _build(sx: int, sy: int, w: int, h: int, tint, strength: int,
           hide_hwnd=None, noise: bool = True, src=None):
    """抓 + 两轮「缩小→放大」模糊 + 叠色调 + 噪点。返回 ``_Entry``（含成品 DC）。

    ``src`` 是给**离屏测试**用的后门：直接指定源 DC（``(hdc, w, h[, hbmp])``），
    跳过抓屏 —— 合成一张黑白条纹图就能验证「是不是真的糊了」，不用动真实桌面。
    """
    if src is not None:
        src_dc, sw0, sh0 = src[0], src[1], src[2]
        src_bmp = src[3] if len(src) > 3 else None
        cap = _temp_dc(w, h)
        if cap is None:
            return None
        if not _stretch(cap.dc, w, h, src_dc, sw0, sh0, src_bmp):
            cap.dispose()
            return None
    else:
        cap = _capture(sx, sy, w, h, hide_hwnd)
    if cap is None:
        return None

    out = _temp_dc(w, h)
    if out is None:
        cap.dispose()
        return None

    sw, sh = max(1, w // _DOWNSCALE), max(1, h // _DOWNSCALE)
    small = _temp_dc(sw, sh)
    if small is None:
        out.dispose()
        cap.dispose()
        return None
    # 第一轮：缩小（块平均）→ 放大（双线性）
    _stretch(small.dc, sw, sh, cap.dc, w, h, cap.bmp)
    _stretch(out.dc, w, h, small.dc, sw, sh, small.bmp)
    # 第二轮：再走一遍，半径翻倍，顺手把第一轮的插值痕迹抹匀
    _stretch(small.dc, sw, sh, out.dc, w, h, out.bmp)
    _stretch(out.dc, w, h, small.dc, sw, sh, small.bmp)
    small.dispose()
    cap.dispose()

    # ---- 色调：AlphaBlend 一层实色 ----
    tint_dc = _temp_dc(w, h)
    if tint_dc is not None:
        rect = wintypes.RECT(0, 0, w, h)
        brush = gdi32.CreateSolidBrush(_colorref(tint))
        user32.FillRect(tint_dc.dc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)
        blend = BLENDFUNCTION()
        blend.BlendOp = AC_SRC_OVER
        blend.BlendFlags = 0
        blend.SourceConstantAlpha = max(0, min(255, int(strength)))
        blend.AlphaFormat = 0
        msimg32.AlphaBlend(out.dc, 0, 0, w, h, tint_dc.dc, 0, 0, w, h, blend)
        tint_dc.dispose()

    # ---- 颗粒：真亚克力都有一层极淡的噪点 ----
    if noise and strength < 250:
        nd = _ensure_noise()
        if nd:
            blend = BLENDFUNCTION()
            blend.BlendOp = AC_SRC_OVER
            blend.BlendFlags = 0
            blend.SourceConstantAlpha = _NOISE_ALPHA
            blend.AlphaFormat = 0
            for ty in range(0, h, _NOISE):
                for tx in range(0, w, _NOISE):
                    cw = min(_NOISE, w - tx)
                    ch = min(_NOISE, h - ty)
                    msimg32.AlphaBlend(out.dc, tx, ty, cw, ch,
                                       nd, 0, 0, cw, ch, blend)
    return out


def _evict() -> None:
    while len(_order) > MAX_ENTRIES:
        key = _order.pop(0)
        entry = _entries.pop(key, None)
        if entry:
            entry.dispose()


def has(key: str) -> bool:
    return key in _entries


def invalidate(prefix: str = "") -> None:
    """按前缀清缓存（``""`` = 全清）。长条改尺寸 / 面板换位置时用。"""
    for key in list(_entries):
        if not prefix or key.startswith(prefix):
            entry = _entries.pop(key, None)
            if key in _order:
                _order.remove(key)
            if entry:
                entry.dispose()


def reset() -> None:
    invalidate("")


def blur_into(dst, src_dc, w: int, h: int, tint, strength: int = 150,
              noise: bool = True, src_bmp=None) -> bool:
    """离屏版：把 ``src_dc`` 里的图糊掉 + 叠色调后画进 ``dst``（离屏测试用）。"""
    entry = _build(0, 0, w, h, tint, strength, None, noise,
                   src=(src_dc, w, h, src_bmp))
    if entry is None:
        return False
    ok = bool(gdi32.BitBlt(dst, 0, 0, w, h, entry.dc, 0, 0, _SRCCOPY))
    entry.dispose()
    return ok


def blit(dst, key: str, sx: int, sy: int, dx: int, dy: int, w: int, h: int,
         tint, strength: int = 150, hide_hwnd=None, noise: bool = True,
         hold: bool = False) -> bool:
    """把「毛玻璃底」画到 ``dst`` 的 (dx, dy, w, h)。

    ``(sx, sy)`` 是这块区域在**屏幕**上的位置（要抓哪）。
    key 命中缓存就直接复用（不重抓、不闪）；key 变了才抓。

    ``hold=True``（拖动中）**只吃缓存、绝不重抓**：重抓要先 ``ShowWindow`` 藏起
    自己再 BitBlt，拖动时每帧一藏一显会闪成一片；拖动期间尺寸/位置一秒变几十次，
    干脆拿旧图凑合。尺寸对不上时**拉伸**成目标尺寸而不是裁剪 —— 裁剪会在长高的
    那一条留出没盖到的边（调用方的实色底会露出来，成一条色带）；拉伸对糊过的
    低频图完全看不出来。
    """
    if w <= 0 or h <= 0:
        return False
    entry = _entries.get(key)
    if hold and entry is not None:
        if entry.w == w and entry.h == h:
            return bool(gdi32.BitBlt(dst, dx, dy, w, h, entry.dc, 0, 0, _SRCCOPY))
        old_mode = gdi32.SetStretchBltMode(dst, _HALFTONE)
        ok = gdi32.StretchBlt(dst, dx, dy, w, h,
                              entry.dc, 0, 0, entry.w, entry.h, _SRCCOPY)
        gdi32.SetStretchBltMode(dst, old_mode)
        return bool(ok)
    sig = (int(sx), int(sy), int(w), int(h), int(strength), tuple(tint),
           bool(noise))
    if entry is None or entry.sig != sig or entry.w != w or entry.h != h:
        if entry is not None:
            entry.dispose()
            _entries.pop(key, None)
            if key in _order:
                _order.remove(key)
        entry = _build(sx, sy, w, h, tint, strength, hide_hwnd, noise)
        if entry is None:
            return False
        entry.sig = sig
        _entries[key] = entry
        _order.append(key)
        _evict()
    return bool(gdi32.BitBlt(dst, dx, dy, w, h, entry.dc, 0, 0, _SRCCOPY))
