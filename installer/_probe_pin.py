"""实测：干净路径下自动常驻任务栏 + AutoPinnedExe 标记是否写入。

排查要点
--------
1. pin_when_ready 最长要 8*2.5=20s，之前只等 14s 就下结论，必须等够。
2. onefile 打包后 sys.executable 可能指向 _MEIxxxx 临时目录，
   若 explorer 记录的 ExecutablePath 是临时路径，find_entry 就永远匹配不上。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

BASE = r"Control Panel\NotifyIconSettings"
PIN_KEY = r"Software\PowerMonitor"
EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PowerMonitor" / "能耗统计.exe"
APP_NAME = "能耗统计.exe"


def out(label: str, value: object) -> None:
    print(f"{label}: {value}", flush=True)


def kill_running() -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    PROCESS_TERMINATE = 0x0001
    killed = 0
    pids = (wintypes.DWORD * 4096)()
    need = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(need)):
        return 0
    count = need.value // ctypes.sizeof(wintypes.DWORD)
    for i in range(count):
        pid = pids[i]
        if pid == 0:
            continue
        h = k32.OpenProcess(PROCESS_TERMINATE | 0x0400, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.normcase(buf.value) == os.path.normcase(str(EXE)):
                    if k32.TerminateProcess(h, 0):
                        killed += 1
        finally:
            k32.CloseHandle(h)
    return killed


def dump_entries(tag: str) -> list[dict]:
    rows: list[dict] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, BASE)
    except OSError:
        out(f"[{tag}] NotifyIconSettings", "不存在")
        return rows
    try:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as sk:
                    vals = {}
                    j = 0
                    while True:
                        try:
                            n, v, _t = winreg.EnumValue(sk, j)
                        except OSError:
                            break
                        vals[n] = v
                        j += 1
            except OSError:
                continue
            exe = str(vals.get("ExecutablePath", ""))
            if "能耗" in exe or "PowerMonitor" in exe or "_MEI" in exe:
                rows.append({"sub": sub, "exe": exe, "promoted": vals.get("IsPromoted")})
    finally:
        winreg.CloseKey(root)
    out(f"[{tag}] 相关托盘条目数", len(rows))
    for r in rows:
        out(f"    {tag}", f"IsPromoted={r['promoted']} exe={r['exe']}")
    return rows


def read_marker(tag: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PIN_KEY) as k:
            v = str(winreg.QueryValueEx(k, "AutoPinnedExe")[0])
    except OSError:
        v = ""
    out(f"[{tag}] AutoPinnedExe", v or "（无）")
    return v


def clear_marker() -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, PIN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.DeleteValue(k, "AutoPinnedExe")
    except OSError:
        pass


def enum_windows() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    found: list[str] = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value
        if cls.startswith("PowerMonitor"):
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, tbuf, 256)
            found.append(
                f"pid={pid.value} class={cls} visible={bool(user32.IsWindowVisible(hwnd))} text={tbuf.value}"
            )
        return True

    user32.EnumWindows(CB(cb), 0)
    out("程序窗口", len(found))
    for f in found:
        out("    ", f)


def main() -> int:
    out("目标 exe", EXE)
    out("exe 存在", EXE.exists())
    if not EXE.exists():
        return 2

    killed = kill_running()
    out("已结束运行实例", killed)
    time.sleep(2)

    out("---- 启动前 ----", "")
    dump_entries("前")
    read_marker("前")

    clear_marker()
    out("已清空 AutoPinnedExe 标记（模拟全新安装路径）", "")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), creationflags=flags)
    out("已启动，父 pid", proc.pid)

    # 分段等待并逐点采样，避免一次睡到底看不到中间态
    marks = [3, 8, 14, 21, 27, 33]
    prev = 0
    samples: list[tuple[int, str, list]] = []
    for m in marks:
        time.sleep(max(0, m - prev))
        prev = m
        tag = f"t={m}s"
        marker = read_marker(tag)
        rows = dump_entries(tag)
        promoted = [r["promoted"] for r in rows]
        samples.append((m, marker, promoted))

    print("\n==== 采样汇总 ====", flush=True)
    for m, marker, promoted in samples:
        print(f"t={m:>2}s  标记={'有' if marker else '无'}  IsPromoted={promoted}", flush=True)

    enum_windows()

    final_marker = read_marker("最终")
    print("\n==== 结论 ====", flush=True)
    print(f"AutoPinnedExe 写入: {'是' if final_marker else '否'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
