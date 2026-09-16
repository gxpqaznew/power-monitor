"""探针：对**已经注册过**的托盘图标再发 NIM_ADD，Shell 会怎么答？代价多大？

动机：v1.0.15 修完 uFlags 那个坑之后，真机走查还剩一项 FAIL ——
「发 TaskbarCreated 重注册之后，1~2 秒内右键弹不出菜单，2 秒后恢复」。

第一轮结论（已实测）：对**已存在**的图标重复 NIM_ADD **立刻**返回 FALSE，
err=0x80004005(E_FAIL)，耗时 0.6~0.8 ms。也就是说这不是「Shell 忙」，而是
「图标已存在，别重复注册」。

那就顺出一个可怕的推论：`tray._add(retries=3)` 在**失败**路径上会
`time.sleep(NIM_ADD_DELAY)` = 2.0 秒，重试 3 次 = **在窗口过程里阻塞 4 秒**。
而 TaskbarCreated 是在窗口过程里**同步**处理的 —— 这 4 秒里托盘窗口收不到
任何消息，右键当然弹不出菜单。观察到的「2 秒后恢复」正是这段 sleep。

本探针量三件事：
  1. 图标已存在时，`_add(retries=3)` 到底阻塞多久（预期 ≈ 4.0 s）；
  2. 这种「冗余 ADD」失败之后，NIM_MODIFY（带完整 flags）能不能成功 ——
     能的话，这就是一条**不需要 sleep 的修复路径**；
  3. 失败之后图标还在不在（Shell_NotifyIconGetRect）。

用法：python _trayaddprobe.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.tray import (  # noqa: E402
    NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_MODIFY, TrayIcon,
)
from powermon.w32 import shell32, user32  # noqa: E402


class _NII(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]


def rect_of(hwnd, uid=1):
    nii = _NII(cbSize=ctypes.sizeof(_NII), hWnd=hwnd, uID=uid)
    rect = wintypes.RECT()
    hr = shell32.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(rect))
    if hr != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def main() -> int:
    enable_dpi_awareness()
    if not user32.FindWindowW("Shell_TrayWnd", None):
        print("explorer 没在跑，本探针无意义")
        return 1

    ico = Path(__file__).resolve().parent / "app.ico"
    hicon = user32.LoadImageW(None, str(ico), 1, 0, 0, 0x10 | 0x40)
    if not hicon:
        print("加载 app.ico 失败")
        return 1

    icon = TrayIcon(lambda anchor=None: None, lambda cmd: None)
    print(f"首次 create -> {icon.create(hicon, 'PowerMonitor 探针')}")
    time.sleep(0.6)
    print(f"图标矩形 = {rect_of(icon.hwnd)}")

    # ---- 1. 冗余 ADD 的代价 ----
    print("\n--- 图标已存在时 _add(retries=3) 的实际耗时 ---")
    t0 = time.perf_counter()
    ok = icon._add(retries=3)
    dt = time.perf_counter() - t0
    print(f"_add(retries=3) -> {ok}  用时 {dt:.2f} s   "
          f"（期望 ≈ 4.0 s = 2 次 sleep × NIM_ADD_DELAY）")
    print(f"    → 定点在窗口过程里就是「托盘 4 秒不响应」")

    # ---- 2. 失败之后 NIM_MODIFY 能不能修好 ----
    print("\n--- 冗余 ADD 失败后，NIM_MODIFY（完整 flags）能不能成功 ---")
    icon._sync_nid()
    nid = icon._copy_nid()
    nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
    ctypes.set_last_error(0)
    ret = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
    print(f"NIM_MODIFY(全 flags) -> {bool(ret)}  err={ctypes.get_last_error()}")

    # ---- 3. 图标还在不在 ----
    print("\n--- 冗余 ADD 失败后图标是否仍在 ---")
    print(f"图标矩形 = {rect_of(icon.hwnd)}")

    icon.destroy()
    user32.DestroyIcon(hicon)
    print("\n探针结束，图标已删。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
