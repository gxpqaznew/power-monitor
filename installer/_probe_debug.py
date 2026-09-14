"""带调试日志的端到端验证。

流程：结束旧实例 -> 部署新 exe -> 清空标记 -> 带 POWERMON_DEBUG=1 启动
      -> 45s 内逐 0.5s 盯注册表 -> 打印 debug.log -> 收尾结束进程。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / "dist" / "能耗统计.exe"
INST = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PowerMonitor"
EXE = INST / "能耗统计.exe"
LOG = INST / "debug.log"
KEY = r"Software\PowerMonitor"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def all_procs() -> list[tuple[int, int, str]]:
    """[(pid, ppid, 映像路径)]"""
    TH32CS_SNAPPROCESS = 0x00000002
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    rows: list[tuple[int, int, str]] = []
    if snap == -1:
        return rows
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            pid, ppid = e.th32ProcessID, e.th32ParentProcessID
            h = k32.OpenProcess(0x1000, False, pid)
            path = ""
            if h:
                try:
                    b = ctypes.create_unicode_buffer(1024)
                    n = wintypes.DWORD(len(b))
                    if k32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(n)):
                        path = b.value
                finally:
                    k32.CloseHandle(h)
            rows.append((pid, ppid, path))
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return rows


def kill_tree(root_pid: int) -> list[int]:
    """结束 root_pid 及其所有后代。"""
    procs = all_procs()
    kids: dict[int, list[int]] = {}
    for pid, ppid, _ in procs:
        kids.setdefault(ppid, []).append(pid)
    order: list[int] = []

    def walk(p: int) -> None:
        for c in kids.get(p, []):
            walk(c)
        order.append(p)

    walk(root_pid)
    killed = []
    for pid in order:
        h = k32.OpenProcess(0x0001, False, pid)
        if h:
            if k32.TerminateProcess(h, 0):
                killed.append(pid)
            k32.CloseHandle(h)
    return killed


def related_pids() -> list[tuple[int, int, str]]:
    out = []
    for pid, ppid, path in all_procs():
        base = os.path.basename(path)
        if base == os.path.basename(EXE) or ("_MEI" in path and base.endswith(".exe") and "能耗" in base):
            out.append((pid, ppid, path))
    return out


def snapshot() -> tuple[bool, str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            return True, str(winreg.QueryValueEx(k, "AutoPinnedExe")[0])
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY):
            return True, ""
    except OSError:
        return False, ""


def main() -> int:
    print("== 1. 结束旧实例 ==", flush=True)
    for pid, ppid, path in related_pids():
        print(f"  杀掉 pid={pid} {path}", flush=True)
        print(f"    结果: {kill_tree(pid)}", flush=True)
    time.sleep(2)

    print("\n== 2. 部署新 exe ==", flush=True)
    print(f"  源: {DIST} ({DIST.stat().st_size} 字节)", flush=True)
    shutil.copy2(DIST, EXE)
    print(f"  已部署到: {EXE} ({EXE.stat().st_size} 字节)", flush=True)

    if LOG.exists():
        LOG.unlink()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
        print("  已删除 Software\\PowerMonitor", flush=True)
    except OSError:
        print("  Software\\PowerMonitor 本就不存在", flush=True)

    print("\n== 3. 带调试开关启动 ==", flush=True)
    env = dict(os.environ, POWERMON_DEBUG="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([str(EXE)], cwd=str(INST), env=env, creationflags=flags)
    print(f"  父 pid={proc.pid}", flush=True)

    t0 = time.time()
    last = None
    while time.time() - t0 < 45:
        cur = snapshot()
        if cur != last:
            print(
                f"  t={time.time() - t0:5.1f}s 键={cur[0]!s:5} AutoPinnedExe={cur[1] or '（无）'}",
                flush=True,
            )
            last = cur
        time.sleep(0.5)

    print("\n== 4. 进程状态 ==", flush=True)
    for pid, ppid, path in related_pids():
        print(f"  pid={pid} ppid={ppid} {path}", flush=True)

    print("\n== 4b. 托盘注册表条目 ==", flush=True)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as r:
            i = 0
            hits = 0
            while True:
                try:
                    sub = winreg.EnumKey(r, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(r, sub) as sk:
                        exe_v = str(winreg.QueryValueEx(sk, "ExecutablePath")[0])
                        try:
                            promoted = winreg.QueryValueEx(sk, "IsPromoted")[0]
                        except OSError:
                            promoted = "（无此值）"
                except OSError:
                    continue
                if "能耗" in exe_v or "PowerMonitor" in exe_v:
                    hits += 1
                    print(f"  IsPromoted={promoted}  {exe_v}", flush=True)
            if not hits:
                print("  （没有本程序的托盘条目）", flush=True)
    except OSError as e:
        print(f"  读取失败: {e}", flush=True)

    print("\n== 5. debug.log ==", flush=True)
    if LOG.exists():
        print(LOG.read_text(encoding="utf-8", errors="replace"), flush=True)
    else:
        print("  （没有生成 debug.log！说明 exe 里没有调试代码，或 app_dir 不是安装目录）", flush=True)

    print("\n== 5b. 程序窗口 / 对话框 ==", flush=True)
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, c, 256)
        cls = c.value
        if cls.startswith("PowerMonitor") or cls == "#32770":
            t = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, t, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            print(
                f"  pid={pid.value} class={cls:24} "
                f"visible={bool(user32.IsWindowVisible(hwnd))} text={t.value}",
                flush=True,
            )
        return True

    user32.EnumWindows(CB(_cb), 0)

    print("\n== 6. 收尾：结束测试实例 ==", flush=True)
    for pid, _ppid, _p in related_pids():
        print(f"  结束 pid={pid} -> {kill_tree(pid)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
