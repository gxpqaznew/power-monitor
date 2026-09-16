"""真机验证：把长条真的建出来、显示、截图，然后销毁。

为什么要在同一个进程里截图：agent 沙箱会把命令结束时还活着的进程一起清掉，
所以「先启动、再另起一个进程截图」是截不到的。这里全程在一个进程内完成：
建窗口 → 显示 → BitBlt 抓屏 → 写 PNG → 销毁，一步都不落。

用法：
    python _striplive.py [宽度] [x0] [out_name]
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
import zlib
from pathlib import Path

os.environ.setdefault("POWERMON_DEBUG", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import debug, stripopts, taskbar, w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.meter import Snapshot, rate_at  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            raw += bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4]))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def grab(x0: int, y0: int, w: int, h: int, name: str) -> None:
    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)
    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem_dc, bmp)
    gdi32.BitBlt(mem_dc, 0, 0, w, h, screen, x0, y0, SRCCOPY)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    OUT_DIR.mkdir(exist_ok=True)
    write_png(OUT_DIR / name, buf, w, h)
    print(f"已写出 {OUT_DIR / name}  ({x0},{y0}) {w}x{h}")


def make_snapshot(cfg, now: float, **overrides) -> Snapshot:
    segment, rate = rate_at(cfg)
    base = dict(
        session_wh=428.3, session_cost=0.32, covered_seconds=7350.0,
        average_w=118.0, peak_w=386.0, samples=3675, peak_wh=353.0,
        valley_wh=75.3, current_w=132.0, cpu_w=62.0, gpu_w=35.0, base_w=35.0,
        cpu_util=41.0, in_valley=False, today_wh=1240.0, today_cost=0.86,
        total_wh=45678.9, total_sessions=12,
        power_on_ts=now - 17700.0, power_on_source="电源事件",
        first_seen_ts=now - 7350.0,
        cpu_source="PDH 负载模型", gpu_source="nvml", cpu_estimated=True,
        gpu_measured=True, gpu_names=["NVIDIA GeForce RTX 3080"],
        gpu_limits=[320.0], segment=segment, rate=rate,
    )
    base.update(overrides)
    return Snapshot(**base)


def check_alignment(strip) -> int:
    """在**真实渲染出来的像素**上验「两排对齐」和「拖动把手」。

    为什么还要在像素上验一遍：`_stripfields_test.py` 验的是「绘制调用的 x」，
    那是源码层的账；这里是渲染结果本身 —— 如果 alpha 合成、抠色、或者别的什么
    把画面挪了位，只有看像素才发现。分隔线是纯色实心 1px 竖条，正好当标尺用。
    """
    bad = 0
    view, pal = strip._view, strip._pal
    if view is None or pal is None:
        print("[SKIP] 没有渲染结果，跳过像素级校验")
        return 0
    w, h = strip._w, strip._h
    # ⚠️ pal 里存的是 COLORREF（低字节是 **R**，不是 B）；DIB 是 BGRA。
    # 这两个顺序搞反过一次，结果「一条分隔线都没找到」，而空集合让对齐断言
    # 恒真 —— 看着 PASS，其实什么都没验。所以下面还专门挡一句「一个都没找到」。
    div = pal["div"]
    dr, dg, db = div & 0xFF, (div >> 8) & 0xFF, (div >> 16) & 0xFF
    rows = strip._plan_rows
    content_h = strip._plan_height
    band_top = (h - content_h) / 2.0
    row_h = content_h / max(1, rows)

    def is_div(x, y):
        i = (y * w + x) * 4
        return view[i] == db and view[i + 1] == dg and view[i + 2] == dr

    found = []
    for i in range(rows):
        y0 = int(band_top + row_h * i + row_h * 0.28)
        y1 = max(y0 + 2, int(band_top + row_h * i + row_h * 0.72))
        found.append([x for x in range(w)
                      if all(is_div(x, y) for y in range(y0, y1))])

    print(f"像素级：{rows} 行，分隔线 x = {found}")
    if not any(found):
        bad += 1
        print("[FAIL] 一条分隔线都没在像素里找到（判据本身失效，别当成通过）")
    elif rows < 2:
        print("[SKIP] 只有一行，没有「两排」可比")
    elif all(f == found[0][:len(f)] for f in found[1:]):
        print("[PASS] 两行的分隔线在同一批 x 上（像素级对齐）")
    else:
        bad += 1
        print("[FAIL] 两行的分隔线不在同一批 x 上")

    # 拖动：不再有把手（两端那两列小圆点被否掉了），整块胶囊自己就是拖动面。
    # 能拖的前提是窗口能收到鼠标消息 —— 分层窗口的命中测试按像素 alpha 走，
    # 所以胶囊内部必须命中，圆角外那点必须穿过去（穿了才不挡任务栏的点击）。
    if strip._interactive is not True:
        bad += 1
        print("[FAIL] 长条默认不能拖（_interactive 应为 True）")
    else:
        print("[PASS] 长条默认可拖（没有「锁定位置」）")

    rect = wintypes_rect(strip.hwnd)
    mid_x = (rect[0] + rect[2]) // 2
    mid_y = (rect[1] + rect[3]) // 2
    hit = user32.WindowFromPoint(w32.wintypes.POINT(mid_x, mid_y))
    if hit == strip.hwnd:
        print("[PASS] 胶囊正中命中的就是长条（能抓）")
    else:
        bad += 1
        print(f"[FAIL] 胶囊正中命中的不是长条（{hit:#x} vs {strip.hwnd:#x}）")

    # 圆角外：贴着窗口左上角那一像素 —— alpha=0，鼠标应该穿过去
    corner = user32.WindowFromPoint(w32.wintypes.POINT(rect[0], rect[1]))
    if corner == strip.hwnd:
        bad += 1
        print("[FAIL] 圆角外面那点被长条吃了（会挡住任务栏点击）")
    else:
        print(f"[PASS] 圆角外面那点仍然穿透（命中 {corner:#x}）")
    return bad


def main() -> int:
    enable_dpi_awareness()
    argv = sys.argv[1:]
    crop_w = int(argv[0]) if argv else 1300
    x0 = int(argv[1]) if len(argv) > 1 else 0
    name = argv[2] if len(argv) > 2 else "strip_live.png"
    # 第 4 个参数：要显示哪些字段。all = 全部勾上（用来验折行）。
    fields = argv[3] if len(argv) > 3 else ""

    cfg = Config()
    cfg.strip_enabled = True
    # 像素级校验要拿分隔线的**原色**去比对，所以固定用不透明质感
    # （玻璃/深色卡片会把 RGB 按 alpha 预乘，颜色对不上就假报错误）
    cfg.strip_theme = "auto"
    if fields == "all":
        cfg.strip_fields = list(stripopts.FIELD_KEYS)
    elif fields:
        cfg.strip_fields = [f for f in fields.split(",") if f]
    print(taskbar.describe())
    print(f"strip_position={cfg.strip_position} 字段={cfg.strip_fields}")

    snap = make_snapshot(cfg, time.time())
    strip = TaskbarStrip(cfg)
    ok = strip.create(snap)
    print(f"create()={ok} hwnd={strip.hwnd} 可见={strip.visible}")
    if not ok:
        return 1

    for _ in range(3):
        strip.tick(snap)
        time.sleep(0.25)

    info = taskbar.taskbar()
    top = info[1][1] - 8
    # 抓之前再把自己抬到兄弟窗口最顶层并重画一次：
    # 本机通常还跑着一个正式版的 PowerMonitor，它也有长条、也在定期抢 HWND_TOP。
    # 不重申一次的话抓到的可能是**它**（两张长条锚点相同、互相盖住），
    # 于是「我的长条画错了」这种假警报就来了。
    strip._ensure_above_siblings(force=True)
    strip._render_if_needed(snap)
    time.sleep(0.2)
    grab(x0, top, crop_w, info[1][3] - info[1][1] + 12, name)

    if strip.hwnd:
        r = wintypes_rect(strip.hwnd)
        print(f"长条实际窗口矩形 = {r}  排了 {strip._plan_rows} 行 "
              f"（计划高 {strip._plan_height}px，圆角 {strip._radius:.1f}）")
    bad = check_alignment(strip)
    strip.destroy()
    print("已销毁")
    return 1 if bad else 0


def wintypes_rect(hwnd):
    r = w32.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


if __name__ == "__main__":
    raise SystemExit(main())
