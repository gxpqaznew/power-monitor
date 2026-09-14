"""高分辨率观测：删除整个 Software\\PowerMonitor 键后启动程序，
逐 0.5s 盯住注册表，精确判断自动常驻分支的执行情况。

三种可能的结果要区分开：
  * 键始终不出现     -> _maybe_pin_taskbar 根本没被调用
  * 键出现但无值     -> CreateKeyEx 跑了，SetValueEx 失败（被 pass 吞掉）
  * 键和值都出现     -> 逻辑正常
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

KEY = r"Software\PowerMonitor"
EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PowerMonitor" / "能耗统计.exe"


def snapshot() -> tuple[bool, str]:
    """返回 (键是否存在, AutoPinnedExe 的值)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            pass
    except OSError:
        return False, ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            return True, str(winreg.QueryValueEx(k, "AutoPinnedExe")[0])
    except OSError:
        return True, ""


def dump_key(label: str) -> None:
    ex = snapshot()[0]
    print(f"  {label}: 键存在={ex} AutoPinnedExe={snapshot()[1] or '（无）'}")

    # 同时列出键里所有值，确认没有别的写入方式
    if ex:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
                i = 0
                names = []
                while True:
                    try:
                        n, v, _t = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    names.append(f"{n}={v!r}")
                    i += 1
                print(f"     值列表: {names or '（空）'}")
        except OSError as e:
            print(f"     值列表读取失败: {e}")


def kill_app() -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    PROCESS_TERMINATE, PROCESS_QUERY = 0x0001, 0x0400
    killed = 0
    buf = (wintypes.DWORD * 4096)()
    need = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(need)):
        return 0
    for i in range(need.value // ctypes.sizeof(wintypes.DWORD)):
        pid = buf[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY, False, pid)
        if not h:
            continue
        try:
            pbuf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(pbuf))
            if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                if os.path.normcase(pbuf.value) == os.path.normcase(str(EXE)):
                    if k32.TerminateProcess(h, 0):
                        killed += 1
        finally:
            k32.CloseHandle(h)
    return killed


def main() -> int:
    print("目标:", EXE, flush=True)
    print("已结束实例:", kill_app(), flush=True)
    time.sleep(2)

    print("\n-- 清空前 --", flush=True)
    dump_key("初始")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
        print("  已彻底删除 Software\\PowerMonitor", flush=True)
    except OSError as e:
        print(f"  删除失败（可能不存在）: {e}", flush=True)
    print("  删除后 键存在 =", snapshot()[0], flush=True)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), creationflags=flags)
    t0 = time.time()
    print(f"\n-- 已启动 pid={proc.pid}，开始 0.5s 粒度观测 45s --", flush=True)

    last: tuple[bool, str] | None = None
    events: list[str] = []
    while time.time() - t0 < 45:
        cur = snapshot()
        if cur != last:
            el = time.time() - t0
            print(
                f"  t={el:5.1f}s  键存在={cur[0]!s:5} AutoPinnedExe={cur[1] or '（无）'}",
                flush=True,
            )
            events.append(f"t={el:.1f}s {cur}")
            last = cur
        time.sleep(0.5)

    print("\n==== 事件时间线 ====", flush=True)
    for e in events:
        print("  " + e, flush=True)

    print("\n==== 最终判定 ====", flush=True)
    exists, value = snapshot()
    if not exists:
        verdict = "键始终未创建 -> _maybe_pin_taskbar 未被调用（或 exe 未含新代码）"
    elif not value:
        verdict = "键已创建但值为空 -> SetValueEx 失败被静默吞掉"
    else:
        verdict = f"写入成功 -> 自动常驻逻辑正常（{value}）"
    print("  " + verdict, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
