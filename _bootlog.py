"""核对「本次开机」判据：把 Kernel-Boot 的全部事件类型和历史摊开看。

顺便验证：EventID 30 是否只在「启动」时记，而不会在关机/休眠时也记一次
（如果关机时也记，那"最新一条"就可能指向昨晚的关机时刻，判据就不可靠）。
"""

import ctypes
import re
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

wevtapi = ctypes.WinDLL("wevtapi", use_last_error=True)
wevtapi.EvtQuery.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
wevtapi.EvtQuery.restype = wintypes.HANDLE
wevtapi.EvtNext.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
wevtapi.EvtNext.restype = wintypes.BOOL
wevtapi.EvtRender.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
wevtapi.EvtRender.restype = wintypes.BOOL
wevtapi.EvtClose.argtypes = [wintypes.HANDLE]

import calendar  # noqa: E402

TIME_RE = re.compile(r"SystemTime='(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")
ID_RE = re.compile(r"<EventID[^>]*>(\d+)</EventID>")
DATA_RE = re.compile(r"<Data Name='([^']+)'>([^<]*)</Data>")


def fetch(query: str, count: int) -> list[dict]:
    result = wevtapi.EvtQuery(None, "System", query, 0x1 | 0x200)
    if not result:
        return []
    out = []
    try:
        handles = (wintypes.HANDLE * count)()
        returned = wintypes.DWORD(0)
        if not wevtapi.EvtNext(result, count, handles, 0, 0, ctypes.byref(returned)):
            return []
        for i in range(returned.value):
            h = handles[i]
            try:
                used = wintypes.DWORD(0)
                props = wintypes.DWORD(0)
                wevtapi.EvtRender(None, h, 1, 0, None,
                                  ctypes.byref(used), ctypes.byref(props))
                if not used.value:
                    continue
                buf = ctypes.create_unicode_buffer(used.value)
                if not wevtapi.EvtRender(None, h, 1, used.value, buf,
                                         ctypes.byref(used), ctypes.byref(props)):
                    continue
                xml = buf.value
            finally:
                wevtapi.EvtClose(h)
            tm = TIME_RE.search(xml)
            ident = ID_RE.search(xml)
            ts = 0.0
            if tm:
                ts = float(calendar.timegm((*[int(v) for v in tm.groups()], 0, 0, 0)))
            out.append({
                "id": int(ident.group(1)) if ident else 0,
                "ts": ts,
                "data": dict(DATA_RE.findall(xml)),
            })
    finally:
        wevtapi.EvtClose(result)
    return out


print("=== Kernel-Boot 全部事件（近 24 条，本地时间） ===")
events = fetch("*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot']]]", 24)
for e in events:
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
    extra = " ".join(f"{k}={v}" for k, v in e["data"].items())
    print(f"  {stamp}  ID={e['id']:<4} {extra}")

print()
print("=== Kernel-Power 关键事件（关机/睡眠/唤醒） ===")
pw = fetch(
    "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and "
    "(EventID=42 or EventID=107 or EventID=109 or EventID=41 or EventID=187)]]", 20)
for e in pw:
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
    print(f"  {stamp}  ID={e['id']}")

print()
now = time.time()
print("=== 当前会话判据 ===")
print("现在时间          :", time.strftime("%Y-%m-%d %H:%M:%S"))
newest30 = next((e for e in events if e["id"] == 30), None)
if newest30:
    print("最新 ID=30 事件于  :",
          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest30["ts"])),
          f"（{((now - newest30['ts']) / 3600):.2f} 小时前）")

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
print("GetTickCount64     : %.2f 小时" % (kernel32.GetTickCount64() / 1000 / 3600))
t = ctypes.c_ulonglong()
kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t))
print("清醒时长           : %.2f 小时" % (t.value / 1e7 / 3600))

print()
print("=== 开机自启注册表项 ===")
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        index = 0
        found = False
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            print(f"  {name} = {value}")
            found = found or name == "PowerMonitor"
        if not found:
            print("  （没有 PowerMonitor 项 —— 开机自启尚未开启）")
except FileNotFoundError:
    print("  Run 键不存在")
