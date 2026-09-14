"""最小托盘对照实验。

目的：区分「托盘代码有 bug」和「当前执行环境没有 shell 通知区域」。
检查项：
  1. Shell_TrayWnd / Shell_SecondaryTrayWnd 是否存在（任务栏本体）
  2. explorer.exe 是否在跑
  3. 用 powermon.w32 的 NOTIFYICONDATAW 做一次 NIM_ADD，打印结果与错误码
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from powermon.w32 import (  # noqa: E402
    NIF_ICON,
    NIF_MESSAGE,
    NIF_TIP,
    NIM_ADD,
    NIM_DELETE,
    NOTIFYICONDATAW,
    WM_TRAYICON,
    WNDCLASSEXW,
    WNDPROC,
    kernel32,
    shell32,
    user32,
    wintypes,
)

print("sizeof(NOTIFYICONDATAW) =", ctypes.sizeof(NOTIFYICONDATAW))
print("Python:", sys.version.split()[0])

# --- 1. 任务栏本体 ---
for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "TrayNotifyWnd", "Progman"):
    h = user32.FindWindowW(cls, None)
    print(f"FindWindow({cls!r}) -> {h}")

# --- 2. explorer ---
import subprocess  # noqa: E402

flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
try:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "(Get-Process explorer -ErrorAction SilentlyContinue).Id -join ','"],
        capture_output=True, text=True, timeout=15, creationflags=flags,
    )
    print("explorer.exe PID:", (r.stdout or "").strip() or "（未运行）")
except Exception as e:  # noqa: BLE001
    print("查 explorer 失败:", e)

# --- 3. 真实 NIM_ADD ---
CLASS = "PowermonTrayProbe"


@WNDPROC
def _proc(h, m, w, l):
    return user32.DefWindowProcW(h, m, w, l)


hinst = kernel32.GetModuleHandleW(None)

wc = WNDCLASSEXW()
wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
wc.lpfnWndProc = _proc
wc.hInstance = hinst
wc.lpszClassName = CLASS
atom = user32.RegisterClassExW(ctypes.byref(wc))
print("RegisterClassExW ->", atom, "(1410=已存在)")

hwnd = user32.CreateWindowExW(0, CLASS, "probe", 0, 0, 0, 0, 0, None, None, hinst, None)
print("CreateWindowExW ->", hwnd)

hicon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION
print("LoadIconW ->", hicon)

nid = NOTIFYICONDATAW()
nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
nid.hWnd = hwnd
nid.uID = 1
nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
nid.uCallbackMessage = WM_TRAYICON
nid.hIcon = hicon
nid.szTip = "probe"

ctypes.set_last_error(0)
ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
err = ctypes.get_last_error()
print(f"\nNIM_ADD -> {ok}  err={err} ({err & 0xFFFFFFFF:#010x})")

if ok:
    print("成功：当前环境可以注册托盘图标")
    time.sleep(1)
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    print("已 NIM_DELETE")
else:
    print("失败：当前环境无法注册托盘图标")
    # 换个 uID / 换个 hWnd 再试，排除冲突
    for uid in (2, 3, 1001):
        nid.uID = uid
        ctypes.set_last_error(0)
        ok2 = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        print(f"  换 uID={uid} 重试 -> {ok2} err={ctypes.get_last_error()}")
        if ok2:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            break

if hwnd:
    user32.DestroyWindow(hwnd)
