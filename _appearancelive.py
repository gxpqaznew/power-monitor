"""「长条外观」窗口的**真机走查**：真显示、真截图、真鼠标点击。

离屏测试（``_appearanceshot.py``）已经证明了结构契约：控件齐不齐、``_hits``
有没有越界、按登记区域派发点击能不能改配置。但它用的是 ``SendMessage`` 合成消息，
而且窗口**没有真的显示**。两件事只有真机能验：

  1. **窗口真的显示在屏幕上之后长什么样** —— 毛玻璃有没有上、圆角描边对不对、
     底部按钮有没有被切掉。离屏那一帧走的是同一段 ``_draw``，但 ``ShowWindow``
     之后系统会再擦一次背景，这一段只有真机能走通。
  2. **真鼠标点得动** —— 自绘窗口的命中测试只有真输入才会经过
     ``WM_NCHITTEST`` / 光标命中那条路；合成 ``WM_LBUTTONDOWN`` 是直接调
     ``_on_press``，绕过了「点得到还是点不到」。

做法：建窗 → ``show()`` → 从**屏幕**上把它那一块抓下来（不是抓内部缓冲）→
用 ``SendInput`` 真的点几个控件 → 再抓一张。最后拼成
``_preview/appearance_live.png``。

用法：python _appearancelive.py
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appearancepng import write_png  # noqa: E402
from _appearanceshot import build, hits_of, rect_of  # noqa: E402
from powermon import stripopts  # noqa: E402
from powermon.app import APPEARANCE_KINDS  # noqa: E402
from powermon.roundwin import dib_section  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


# ------------------------------------------------------------------ 真输入

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]


def real_click(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    for flag in (0x0002, 0x0004):          # LEFTDOWN / LEFTUP
        inp = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, flag, 0, None))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.04)


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


# ------------------------------------------------------------------ 抓屏

def grab_screen(l, t, r, b) -> tuple[bytes, int, int]:
    """从**屏幕**上抓一块（不是窗口内部缓冲）。"""
    w, h = int(r - l), int(b - t)
    screen = user32.GetDC(None)
    tmp = gdi32.CreateCompatibleDC(screen)
    try:
        bmp, view = dib_section(tmp, w, h)
        old = gdi32.SelectObject(tmp, bmp)
        try:
            gdi32.BitBlt(tmp, 0, 0, w, h, screen, int(l), int(t), SRCCOPY)
            return bytes(view), w, h
        finally:
            gdi32.SelectObject(tmp, old)
            gdi32.DeleteObject(bmp)
    finally:
        gdi32.DeleteDC(tmp)
        user32.ReleaseDC(None, screen)


def diff_count(x: bytes, y: bytes) -> int:
    """两张（等长）BGRA 抓屏里有多少像素不一样。

    别拿 ``==`` 比两帧：「窗口圆角外会透出桌面」+ 毛玻璃/反锯齿噪声，两帧永远
    会有几百个像素不同。要比的是「差异量级」——同一个档位应该只差零头，
    换了个档位则是整块预览条在变（三千像素起）。
    """
    n = min(len(x), len(y))
    return sum(1 for i in range(0, n, 4) if x[i:i + 4] != y[i:i + 4])


def negligible(counts: tuple[int, int]) -> bool:
    """探针色少到可以忽略（判据阈值是 600，基线得远低于它才有意义）。"""
    return max(counts) < 600


def stats(buf: bytes, w: int, h: int) -> dict:
    colors: dict[tuple, int] = {}
    for i in range(0, len(buf), 4):
        px = (buf[i + 2], buf[i + 1], buf[i])
        colors[px] = colors.get(px, 0) + 1
    top = max(colors.values())
    return {"distinct": len(colors), "dominant": top / (w * h)}


def vivid_count(buf: bytes) -> tuple[int, int]:
    """数「一眼假不了」的洋红 / 青色像素。

    🔴 专为背景图判据准备：拿一张**洋红→青**的探针图当背景，只要它真的铺上去了，
    窗口里就必然出现这两种在浅色/深色配色里都绝不可能出现的颜色。比「两张图
    不一样」确定得多 —— 后者随便一点 hover 高亮也成立。
    护栏：找不到就判 FAIL（本项目踩过「找不到目标却 PASS」的像素判据）。
    """
    magenta = cyan = 0
    for i in range(0, len(buf), 4):
        b, g, r = buf[i], buf[i + 1], buf[i + 2]
        if r > 190 and b > 190 and g < 110:
            magenta += 1
        elif g > 170 and b > 190 and r < 110:
            cyan += 1
    return magenta, cyan


def read_rgb_png(path: Path):
    """读回我们自己 ``write_png`` 产出的 PNG。

    只支持 filter=0 —— 那是 ``write_png`` 唯一会写的形式，这里够用。
    专门用来**自检探针图**：探针图一旦写歪，后面所有「背景图铺上去了没有」的
    判据都是在验一张随机图（踩过：多写一个 filter 字节，每行错位 1~120 字节，
    PNG 照样能打开、颜色也照样是彩色，肉眼看不出）。
    """
    data = Path(path).read_bytes()
    i, idat, w, h = 8, b"", 0, 0
    while i < len(data):
        ln = int.from_bytes(data[i:i + 4], "big")
        tag = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        i += 12 + ln
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        off = y * (1 + w * 3) + 1
        rows.append(raw[off:off + w * 3])
    return rows, w, h


def make_probe_image(path: Path, w: int = 320, h: int = 120) -> Path:
    """洋红 → 青 的横向渐变，纯 stdlib 写 PNG。

    🔴 ``write_png`` 吃的是**纯像素 BGRA**，每行的 filter 字节由它自己补 ——
    这里千万不能再自己塞 ``raw.append(0)``：多出来那一个字节会让每行的像素
    整体错位 1 字节（第 n 行错 n 字节），PNG 看着「能打开」但内容是花的，
    横向渐变被错位成一行一个颜色的横条。踩过：于是「背景图铺上去了没有」
    这个判据等于在验一张随机图，怎么调阈值都不对。
    """
    px = bytearray()
    for _y in range(h):
        for x in range(w):
            t = x / max(1, w - 1)
            # BGRA：B 恒 255、G 递增、R 递减 = 洋红 → 青
            px += bytes((255, int(255 * t), int(255 * (1 - t)), 255))
    write_png(path, bytes(px), w, h)
    return path


# ------------------------------------------------------------------ 主流程

def state(name: str, over: dict, clicks: list[tuple[str, object]] = ()):
    """建窗 → 显示 → 真点几下 → 从屏幕上抓下来。"""
    cfg, strip, win, apply = build(**over)
    win.show()
    pump(0.5)                       # 等它真的画出来（显示后系统还会擦一次背景）

    origin = win._screen_origin()
    check(f"[{name}] 窗口真的显示出来了（拿得到客户区屏幕坐标）",
          win.is_visible and origin is not None, f"origin={origin}")
    if origin is None:
        win.destroy()
        return None, None, None

    ox, oy, cw, ch = origin
    for action, payload in clicks:
        rect = rect_of(hits_of(win), action, payload)
        if rect is None:
            check(f"[{name}] 找得到控件 {action}/{payload}", False)
            continue
        cx = ox + int((rect[0] + rect[2]) // 2)
        cy = oy + int((rect[1] + rect[3]) // 2)
        before = (cfg.strip_rows, cfg.strip_palette, cfg.strip_font_scale)
        real_click(cx, cy)
        pump(0.35)
        after = (cfg.strip_rows, cfg.strip_palette, cfg.strip_font_scale)
        check(f"[{name}] ★ 真鼠标点 {action}={payload!r} → 配置真的变了",
              before != after, f"{before} → {after}")

    rect = wintypes.RECT()
    user32.GetWindowRect(win._hwnd, ctypes.byref(rect))
    buf, w, h = grab_screen(rect.left, rect.top, rect.right, rect.bottom)
    info = stats(buf, w, h)
    check(f"[{name}] 屏幕上抓到的确实是个画了东西的窗口（不是一片纯色）",
          info["distinct"] >= 40 and info["dominant"] < 0.9,
          f"{w}x{h} 颜色数={info['distinct']} 主色占比={info['dominant']:.2f}")

    png = OUT_DIR / f"appearance_live_{name}.png"
    write_png(png, buf, w, h)
    print(f"     -> {png.name}  ({w}x{h})")
    win.hide()
    win.destroy()
    user32.SetCursorPos(20, 20)
    pump(0.2)
    return buf, w, h, png


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    from powermon.app import enable_dpi_awareness
    enable_dpi_awareness()

    if not user32.FindWindowW("Shell_TrayWnd", None):
        print("explorer 没在跑 —— 没有桌面会话，这个走查做不了")
        return 1

    print("=== 1. 默认（跟随系统排数，自动配色）===")
    a = state("default", {})
    base_mag = base_cy = 0
    if a:
        base_mag, base_cy = vivid_count(a[0])
        check("没设背景图时窗口里几乎没有探针色（基线远低于判据阈值）",
              negligible((base_mag, base_cy)),
              f"洋红={base_mag} 青={base_cy}（阈值 600；这点青来自圆角外透出的桌面）")

    print("\n=== 2. 真点一下：排数=单排 / 配色=暗夜 ===")
    b = state("clicked", {}, clicks=[
        ("rows", "1"),
        ("palette", "midnight"),
    ])
    check("点完之后窗口自己重画了（第二次抓屏和第一次不是同一张）",
          a and b and a[0] != b[0], "")

    # 🔴 判据必须**同时**要洋红和青，而且阈值要在基线的 3 倍以上 —— 探针图是
    #    洋红→青的横向渐变，真铺上去了两边各会有上千像素。只数一种颜色、或者
    #    阈值贴着基线，会被窗口里本来就有的青色像素（实测基线 170~235 px，来自
    #    圆角外透出来的桌面）骗过去：第一版就写了 `>= 200`，于是「根本没铺上去」
    #    也 PASS。这类「找不到目标却 PASS」的像素判据本项目踩过，护栏是
    #    「数量不够就判 FAIL」。
    probe = make_probe_image(OUT_DIR / "bg_probe.png")
    rows, pw, ph = read_rgb_png(probe)
    check("探针图本身是干净的横向渐变（每行相同、B 通道恒 255）",
          len({bytes(r) for r in rows}) == 1
          and rows[0][2] == 255 and rows[0][(pw - 1) * 3 + 2] == 255
          and rows[0][0] > rows[0][(pw - 1) * 3],
          f"{pw}x{ph} 不同行数={len({bytes(r) for r in rows})}")

    def enough(counts, what: str) -> bool:
        mag, cy = counts
        need_m, need_c = max(600, base_mag * 3), max(600, base_cy * 3)
        ok = mag >= need_m and cy >= need_c
        print(f"     {what}: 洋红={mag}(需≥{need_m}) 青={cy}(需≥{need_c})")
        return ok

    print("\n=== 3. 背景图（洋红→青探针图）+ cover + 圆角裁剪 ===")
    c = state("bgimage", {"strip_bg_image": str(probe),
                          "strip_bg_fit": "cover", "strip_bg_opacity": 85})
    if c:
        check("★ 背景图真的铺到了长条上（洋红和青同时出现在真机截图里）",
              enough(vivid_count(c[0]), "cover 85%"))

    print("\n=== 4. 换 fill 方式：tile ===")
    d = state("bgtile", {"strip_bg_image": str(probe),
                         "strip_bg_fit": "tile", "strip_bg_opacity": 85})
    if d:
        check("tile 也铺得上", enough(vivid_count(d[0]), "tile 85%"))

    print("\n=== 5. 不透明度 0 = 不画（应和基线一样干净）===")
    e = state("bgoff", {"strip_bg_image": str(probe),
                        "strip_bg_fit": "cover", "strip_bg_opacity": 0})
    if e:
        mag, cy = vivid_count(e[0])
        check("不透明度 0 时背景图不出现（滑块拉到最左的语义）",
              negligible((mag, cy)), f"洋红={mag} 青={cy}（阈值 600）")

    print("\n=== 6. 手改 config 写成分数 0.85 也要认（不能静默不画）===")
    f = state("bgfrac", {"strip_bg_image": str(probe),
                         "strip_bg_fit": "cover", "strip_bg_opacity": 0.85})
    if f:
        check("★ 分数写法 0.85 被当成 85%（不是 int(0.85)=0 → 静默不画）",
              enough(vivid_count(f[0]), "cover 0.85（分数）"))
        if c and e:
            d_same = diff_count(c[0], f[0])      # 0.85 vs 85
            d_off = diff_count(c[0], e[0])       # 85 vs 不画
            check("0.85 与 85 渲染成同一档（差异只是毛玻璃噪声，不是另一张图）",
                  d_same < d_off // 10,
                  f"0.85↔85 差 {d_same} px；85↔不画 差 {d_off} px")

    frames = [x for x in (a, b, c, d, e, f) if x]
    if len(frames) >= 2:
        sheet_w = max(f[1] for f in frames) + 16
        sheet_h = sum(f[2] for f in frames) + 16 * (len(frames) + 1)
        canvas = bytearray(b"\x28\x28\x30\xff" * (sheet_w * sheet_h))
        for row, (buf, w, h, _png) in enumerate(frames):
            y0 = 16 + row * 16 + sum(f[2] for f in frames[:row])
            for y in range(h):
                src = y * w * 4
                dst = ((y0 + y) * sheet_w + 8) * 4
                canvas[dst:dst + w * 4] = buf[src:src + w * 4]
        sheet = OUT_DIR / "appearance_live.png"
        write_png(sheet, bytes(canvas), sheet_w, sheet_h)
        print(f"\n拼图 -> {sheet}  ({sheet_w}x{sheet_h})")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
