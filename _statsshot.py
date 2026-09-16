"""统计窗口实机验证：造一份假账本 → 开真窗口 → 五个分页各截图 → 把表格内容读回来断言。

为什么要读回来：原生 ListView / Tab 的文字是**通过指针传给控件的**，指针写错
时控件显示的是野内存里的垃圾字符，而列数、行数、选中下标全是好的 —— 只看
「表格有几行」发现不了。必须用 LVM_GETITEMTEXTW 把每个格子读回来核对。

跑法：python _statsshot.py
产出：_preview/stats_*.png 五张分页截图 + _preview/stats_sheet.png 拼图
"""

from __future__ import annotations

import ctypes
import json
import struct
import zlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import meter as meter_mod  # noqa: E402
from powermon import stats as stats_mod  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.w32 import (  # noqa: E402
    BITMAPINFO,
    BITMAPINFOHEADER,
    gdi32,
    user32,
    wintypes,
)

OUT_DIR = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
PM_REMOVE = 0x0001

LVM_GETITEMTEXTW = stats_mod.LVM_FIRST + 115
LVM_GETITEMCOUNT = stats_mod.LVM_FIRST + 4
TCM_GETITEMW = stats_mod.TCM_FIRST + 60

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


# ------------------------------------------------------------------ 截图

def write_bmp_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    import zlib
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


def grab_window(hwnd, name: str) -> None:
    """抓窗口在屏幕上的那一块（含边框标题栏），不用 PrintWindow —— 任务栏
    分层窗口那套在本机不可靠，直接 BitBlt 屏幕更稳。"""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    x0, y0 = rect.left, rect.top
    w, h = rect.right - rect.left, rect.bottom - rect.top
    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                 ctypes.byref(bits), None, 0)
    if not bmp or not bits:
        print("  DIB 创建失败")
        return
    old = gdi32.SelectObject(mem_dc, bmp)
    ok = gdi32.BitBlt(mem_dc, 0, 0, w, h, screen, x0, y0, SRCCOPY)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    if not ok:
        print("  BitBlt 失败")
        return
    OUT_DIR.mkdir(exist_ok=True)
    write_bmp_png(OUT_DIR / name, buf, w, h)
    print(f"  截图 {name}  {w}x{h}")


# ------------------------------------------------------------------ 控件读回

def pump(seconds: float = 0.35) -> None:
    """抽消息 + 等一会儿，让控件把内容真正画上去。"""
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def list_row_text(listview, index: int, cols: int) -> list[str]:
    """把第 index 行的每个格子读回来。"""
    out = []
    for sub in range(cols):
        buf = ctypes.create_unicode_buffer(256)
        item = stats_mod.LVITEMW()
        item.mask = stats_mod.LVIF_TEXT
        item.iItem = index
        item.iSubItem = sub
        item.pszText = ctypes.cast(buf, ctypes.c_wchar_p)
        item.cchTextMax = 256
        user32.SendMessageW(listview, LVM_GETITEMTEXTW, index,
                            ctypes.addressof(item))
        out.append(buf.value)
    return out


def tab_label(tab, index: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    item = stats_mod.TCITEMW()
    item.mask = stats_mod.TCIF_TEXT
    item.pszText = ctypes.cast(buf, ctypes.c_wchar_p)
    item.cchTextMax = 128
    user32.SendMessageW(tab, TCM_GETITEMW, index, ctypes.addressof(item))
    return buf.value


# ------------------------------------------------------------------ 假账本

def fake_ledger() -> dict:
    now = time.time()
    days = {}
    # 45 天，功率随工作日/周末变化，数值各不相同（一眼看得出是不是每行都一样）
    for back in range(45):
        ts = now - back * 86400
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        weekend = time.localtime(ts).tm_wday >= 5
        base = 1450.0 if weekend else 980.0
        days[day] = [round(base + back * 37.0, 3),
                     round((base + back * 37.0) / 1000 * 0.56, 4),
                     round(3600.0 * (6.5 if weekend else 4.2), 1)]
    sessions = {}
    for k in range(9):
        boot = int(now - 86400 * k - 30000)
        sessions[str(boot)] = [round(1100.0 + k * 61.0, 3),
                               round((1100.0 + k * 61.0) / 1000 * 0.56, 4),
                               round(9000.0 - k * 210.0, 1),
                               boot + 9000 - k * 210]
    hours = [round(20.0 + (h - 9) ** 2 * 2.4, 2) if 7 <= h <= 23 else 8.0
             for h in range(24)]
    return {
        "power_on_ts": now - 9000,
        "first_seen_ts": now - 8940,
        "total_wh": sum(v[0] for v in days.values()),
        "total_cost": sum(v[1] for v in days.values()),
        "today_date": time.strftime("%Y-%m-%d"),
        "today_wh": days[time.strftime("%Y-%m-%d")][0],
        "today_cost": days[time.strftime("%Y-%m-%d")][1],
        "days": days,
        "sessions": sessions,
        "hours": hours,
        "saved_at": now,
    }


def main() -> int:
    enable_dpi_awareness()
    tmp = Path(tempfile.mkdtemp(prefix="pm_statsshot_")) / "state.json"
    tmp.write_text(json.dumps(fake_ledger(), ensure_ascii=False),
                   encoding="utf-8")
    meter_mod.STATE_PATH = tmp

    cfg = Config()
    meter = meter_mod.EnergyMeter(cfg)
    window = stats_mod.StatsWindow(meter, cfg)
    check("统计窗口能创建", window.create(), "create() 返回真")
    # 必须真的 show 出来再截图：create() 建的窗口没有 WS_VISIBLE，
    # 不上屏的话 BitBlt 抓到的是窗口位置**下面**的那个窗口（第一次就踩了，
    # 截回来一张 IDE 的图，还以为是截图代码错了）。
    window.show()
    pump(0.5)

    tab = window._controls.get(stats_mod.IDC_TAB)
    listview = window._controls.get(stats_mod.IDC_LIST)
    check("Tab 与 ListView 都建出来了", bool(tab) and bool(listview),
          f"tab={tab} list={listview}")

    labels = [tab_label(tab, i) for i in range(len(stats_mod.TABS))]
    expect = [label for _k, label in stats_mod.TABS]
    check("五个分页标题能原样读回（没有乱码）", labels == expect,
          f"{labels}")

    cols = len(stats_mod.COLUMNS)
    shots = []
    for index, (kind, label) in enumerate(stats_mod.TABS):
        user32.SendMessageW(tab, stats_mod.TCM_SETCURSEL, index, 0)
        window.refresh()
        pump(0.4)
        count = user32.SendMessageW(listview, LVM_GETITEMCOUNT, 0, 0)
        rows = meter.stats_rows(kind, stats_mod.MAX_ROWS)
        check(f"「{label}」行数与账本一致", count == len(rows),
              f"控件 {count} 行 / 账本 {len(rows)} 行")
        text_rows = [list_row_text(listview, i, cols)
                     for i in range(min(3, count))]
        cells_ok = all(all(c and c.strip() for c in r) for r in text_rows)
        check(f"「{label}」前三行的每个格子都读得回文字", cells_ok,
              str(text_rows[0] if text_rows else []))
        if text_rows:
            print("      " + " | ".join(text_rows[0]))
        name = f"stats_{index + 1}_{kind}.png"
        grab_window(window._hwnd, name)
        shots.append(name)

    summary = window._controls.get(stats_mod.IDC_SUMMARY)
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(summary, buf, 512)
    check("汇总行有内容且含「累计」", "累计" in buf.value, buf.value[:80])
    hint = window._controls.get(stats_mod.IDC_HINT)
    hbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hint, hbuf, 512)
    check("底部说明了「点一行就能看明细」（不再黑箱的入口）",
          "下面就是那一条" in hbuf.value, hbuf.value[:60])
    check("底部有账本口径说明", "账本保留" in hbuf.value, hbuf.value[:80])

    # ---- 拼图 ----
    try:
        from PIL import Image
        tiles = [Image.open(OUT_DIR / n).convert("RGB") for n in shots
                 if (OUT_DIR / n).exists()]
        if tiles:
            W = 620
            scaled = [t.resize((W, max(1, int(t.height * W / t.width))))
                      for t in tiles]
            H = sum(t.height + 6 for t in scaled)
            sheet = Image.new("RGB", (W, H), (28, 28, 32))
            y = 0
            for t in scaled:
                sheet.paste(t, (0, y))
                y += t.height + 6
            out = OUT_DIR / "stats_sheet.png"
            sheet.save(out)
            print(f"拼图 {out} {sheet.size}")
    except Exception as exc:  # noqa: BLE001
        print(f"拼图失败（不影响判定）: {exc!r}")

    window.destroy()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
