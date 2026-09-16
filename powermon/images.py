"""把本地图片文件读成 HBITMAP —— 给长条的「背景图」用。

为什么不用 GDI 自带的 ``LoadImage(LR_LOADFROMFILE)``：它只认 BMP / ICO / CUR，
用户随手挑一张壁纸多半是 JPG 或 PNG，会直接失败。所以走 GDI+ 的
``GdipCreateBitmapFromFile``（PNG / JPG / GIF / BMP / TIFF / WebP 都认），
再 ``GdipCreateHBITMAPFromBitmap`` 转成 GDI 位图 —— 之后就能用
``AlphaBlend`` 按不透明度贴到长条上了。

三个刻意的设计：

* **按「路径 + mtime + size」缓存**：长条每帧都可能要这张图，解码一次几十毫秒，
  不能每帧都解。文件被换掉（mtime / 大小变了）会自动重新解码，不用手动清缓存。
* **失败一律返回 None**：路径不存在、格式不认识、GDI+ 起不来 —— 都只是
  「没有背景图」，调用方什么都不用做（长条照样显示读数，绝不能因为一张图崩）。
* **上限 4 张，FIFO 淘汰**：用户只会配一张，多留几张是为了让他来回比对时不用
  反复解码。淘汰时把 HBITMAP 释放掉（内存里一张 4K 壁纸的解码结果有几十兆）。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from . import frost
from .debug import log as _log
from .w32 import gdi32, wintypes

MAX_ENTRIES = 4


class Picture:
    """一张已经解码好的图。``bmp`` 是 GDI 位图句柄，用完由缓存统一释放。"""

    __slots__ = ("bmp", "w", "h", "path")

    def __init__(self, bmp, w: int, h: int, path: str) -> None:
        self.bmp = bmp
        self.w = w
        self.h = h
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - 只用于排障
        return f"<Picture {self.w}x{self.h} {self.path!r}>"


_cache: dict[tuple, Picture] = {}
_order: list[tuple] = []


def _dbg(msg: str) -> None:
    _log("images", msg)


def _decode(path: str) -> Picture | None:
    lib = frost.gdiplus()
    if lib is None:
        _dbg("GDI+ 不可用，背景图跳过")
        return None
    image = ctypes.c_void_p()
    if lib.GdipCreateBitmapFromFile(path, ctypes.byref(image)) != 0 or not image.value:
        _dbg(f"解码失败 path={path}")
        return None
    bmp = wintypes.HBITMAP()
    # 第三个参数是「透明区域填充色」——HBITMAP 没有 alpha 通道，
    # 带透明的 PNG 那块会落成这个颜色。给不透明黑，深色长条上最不显眼。
    color = 0xFF000000
    try:
        width = ctypes.c_uint(0)
        height = ctypes.c_uint(0)
        lib.GdipGetImageWidth(image, ctypes.byref(width))
        lib.GdipGetImageHeight(image, ctypes.byref(height))
        if lib.GdipCreateHBITMAPFromBitmap(image, ctypes.byref(bmp), color) != 0 \
                or not bmp.value:
            _dbg(f"转 HBITMAP 失败 path={path}")
            return None
    finally:
        lib.GdipDisposeImage(image)
    if not width.value or not height.value:
        _dbg(f"尺寸为 0 path={path}")
        return None
    return Picture(bmp.value, int(width.value), int(height.value), path)


def load(path: str) -> Picture | None:
    """读一张图（带缓存）。拿不到就返回 None。"""
    if not path:
        return None
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    if not p.is_file():
        return None
    key = (str(p).lower(), int(st.st_mtime), int(st.st_size))
    hit = _cache.get(key)
    if hit is not None:
        return hit

    pic = _decode(str(p))
    if pic is None:
        return None
    _cache[key] = pic
    _order.append(key)
    _evict()
    _dbg(f"已加载 {pic.w}x{pic.h} {path}")
    return pic


def _evict() -> None:
    while len(_order) > MAX_ENTRIES:
        old = _order.pop(0)
        entry = _cache.pop(old, None)
        if entry is not None:
            _destroy(entry)


def _destroy(pic: Picture) -> None:
    try:
        gdi32.DeleteObject(pic.bmp)
    except Exception:  # noqa: BLE001 - 释放失败无所谓，别让它打断绘制
        pass


def release() -> None:
    """释放全部缓存（进程退出 / 测试收尾用）。"""
    for pic in list(_cache.values()):
        _destroy(pic)
    _cache.clear()
    _order.clear()


def cached() -> int:
    return len(_cache)
