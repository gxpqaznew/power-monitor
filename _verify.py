"""实机验证：启动 exe，测量资源占用、枚举窗口（判断有没有崩溃弹窗）、截屏。"""

import ctypes
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import w32  # noqa: E402
from powermon.w32 import gdi32, user32, wintypes  # noqa: E402

gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(w32.BITMAPINFO), wintypes.UINT,
]

WNDENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "能耗统计.exe"
OUT = ROOT / "_preview"
OUT.mkdir(exist_ok=True)
LINES: list[str] = []


def say(text: str) -> None:
    LINES.append(text)
    print(text, flush=True)


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            b, g, r = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
            raw += bytes((r, g, b))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def grab_screen(path: Path) -> tuple[int, int]:
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    screen = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, screen, 0, 0, 0x00CC0020)
    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = w32.BI_RGB
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(memdc, bmp, 0, h, ctypes.byref(buf),
                    ctypes.byref(info), w32.DIB_RGB_COLORS)
    write_png(path, bytes(buf), w, h)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, screen)
    return w, h


def windows_of(pids: set[int]) -> list[str]:
    found: list[str] = []

    @WNDENUMPROC
    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            txt = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, txt, 256)
            visible = bool(user32.IsWindowVisible(hwnd))
            found.append(
                f"class={cls.value!r} title={txt.value!r} visible={visible}"
            )
        return True

    user32.EnumWindows(callback, 0)
    return found


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 先恢复成「首次运行」状态，才能验证首次运行的完整流程（移走而不是删除）
import tempfile  # noqa: E402

for name in ("config.json", "state.json"):
    f = EXE.parent / name
    if f.exists():
        os.replace(f, os.path.join(tempfile.gettempdir(), "pm_verify_" + name))
        say(f"移走 {name}（等价于首次运行）")
say("dist 内容: " + str(sorted(p.name for p in EXE.parent.iterdir())))

proc = subprocess.Popen(
    [str(EXE)],
    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    close_fds=True,
)
say(f"已启动 {EXE.name}，pid={proc.pid}")

import psutil  # noqa: E402

time.sleep(10)

kids = [p for p in psutil.process_iter(["name"])
        if p.info["name"] and "能耗统计" in p.info["name"]]
if not kids:
    say("!! 进程不见了")
pids = {p.pid for p in kids}
for p in kids:
    mem = p.memory_info()
    full = p.memory_full_info()
    say(f"pid={p.pid} RSS={mem.rss / 1048576:.1f}MB "
        f"私有提交={mem.private / 1048576:.1f}MB "
        f"USS≈任务管理器内存={full.uss / 1048576:.1f}MB "
        f"线程={p.num_threads()} 句柄={p.num_handles()}")

for p in kids:
    p.cpu_percent(None)
time.sleep(8)
for p in kids:
    say(f"pid={p.pid} 8 秒平均 CPU={p.cpu_percent(None) / 8:.2f}%")

say("--- 属于本程序的顶层窗口 ---")
for line in windows_of(pids):
    say("  " + line)

say("--- 首次运行产生的文件 ---")
for name in ("config.json", "state.json"):
    f = EXE.parent / name
    say(f"  {name}: {'有' if f.exists() else '无'}")

w, h = grab_screen(OUT / "desktop.png")
say(f"截屏 {w}x{h} -> _preview/desktop.png")

(OUT / "verify.txt").write_text("\n".join(LINES), encoding="utf-8")

for p in kids:
    try:
        p.kill()
    except Exception:
        pass
say("已结束被测进程")
