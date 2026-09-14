"""还原「本次开机」的真实时刻。

Windows 并没有一个直接可用的「用户按下电源键」时间戳：

* ``psutil.boot_time()`` / ``GetTickCount64`` 把睡眠和休眠的时间也算进去，
  实测某机报 72.7 小时（三天），可用户其实每天早上开机、晚上关机；
* ``QueryUnbiasedInterruptTime`` 扣掉了睡眠，但「快速启动（Fast Startup）」
  会把内核会话跨天延续下去，同样偏大（实测 55.5 小时）。

可靠的是 System 日志里的 ``Microsoft-Windows-Kernel-Boot`` 事件：
每完成一次启动（含快速启动的休眠恢复）记一条，所以**最新一条就是本次开机**。
读一次大约几十毫秒，只在程序启动时做一次。
"""

from __future__ import annotations

import calendar
import ctypes
import re
import time

from .w32 import kernel32

try:  # wevtapi 在 vista 以后都有，理论上不会失败
    wevtapi = ctypes.WinDLL("wevtapi", use_last_error=True)
except OSError:  # pragma: no cover - 非 Windows / 精简系统
    wevtapi = None  # type: ignore[assignment]

EVT_QUERY_CHANNEL_PATH = 0x1
EVT_QUERY_REVERSE_DIRECTION = 0x200
EVT_RENDER_EVENT_XML = 1

# Kernel-Boot 30：每次启动（冷启动 / 快速启动的休眠恢复）各记一条
_EVENT_QUERY = (
    "*[System[(Provider[@Name='Microsoft-Windows-Kernel-Boot']) and (EventID=30)]]"
)
_TIME_RE = re.compile(
    r"SystemTime='(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
)

_bind_done = False


def _bind() -> None:
    """声明 wevtapi 的函数签名。"""
    global _bind_done
    if _bind_done or wevtapi is None:
        return
    from ctypes import wintypes

    wevtapi.EvtQuery.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD
    ]
    wevtapi.EvtQuery.restype = wintypes.HANDLE
    wevtapi.EvtNext.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    wevtapi.EvtNext.restype = wintypes.BOOL
    wevtapi.EvtRender.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wevtapi.EvtRender.restype = wintypes.BOOL
    wevtapi.EvtClose.argtypes = [wintypes.HANDLE]
    wevtapi.EvtClose.restype = wintypes.BOOL
    _bind_done = True


def _parse_iso_utc(xml: str) -> float | None:
    """从事件 XML 里抠出 SystemTime（UTC）并转成 epoch 秒。"""
    match = _TIME_RE.search(xml)
    if not match:
        return None
    parts = [int(v) for v in match.groups()]
    try:
        return float(calendar.timegm((*parts, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def newest_power_on() -> float | None:
    """读 System 日志，返回最近一次启动的 epoch 秒；读不到返回 None。"""
    if wevtapi is None:
        return None
    _bind()
    result = wevtapi.EvtQuery(
        None, "System", _EVENT_QUERY,
        EVT_QUERY_CHANNEL_PATH | EVT_QUERY_REVERSE_DIRECTION,
    )
    if not result:
        return None
    try:
        from ctypes import wintypes

        events = (wintypes.HANDLE * 1)()
        returned = wintypes.DWORD(0)
        if not wevtapi.EvtNext(
            result, 1, events, 0, 0, ctypes.byref(returned)
        ):
            return None
        if returned.value < 1:
            return None

        handle = events[0]
        try:
            used = wintypes.DWORD(0)
            props = wintypes.DWORD(0)
            # 先问需要多大缓冲
            wevtapi.EvtRender(
                None, handle, EVT_RENDER_EVENT_XML, 0, None,
                ctypes.byref(used), ctypes.byref(props),
            )
            if used.value == 0:
                return None
            buffer = ctypes.create_unicode_buffer(used.value)
            if not wevtapi.EvtRender(
                None, handle, EVT_RENDER_EVENT_XML, used.value, buffer,
                ctypes.byref(used), ctypes.byref(props),
            ):
                return None
            return _parse_iso_utc(buffer.value)
        finally:
            wevtapi.EvtClose(handle)
    except Exception:  # noqa: BLE001 - 探测失败不能影响主流程
        return None
    finally:
        wevtapi.EvtClose(result)


def _unbiased_awake_seconds() -> float:
    """自上次真实内核启动以来的「清醒」秒数（不含睡眠）。"""
    counter = ctypes.c_ulonglong()
    try:
        if kernel32.QueryUnbiasedInterruptTime(ctypes.byref(counter)):
            return counter.value / 1e7
    except AttributeError:
        pass
    return 0.0


def session_start() -> tuple[float, str]:
    """返回 (本次开机的 epoch 秒, 口径说明)。

    依次尝试：日志事件 → 系统清醒时长 → psutil 启动时间 → 当前时刻。
    """
    now = time.time()
    up_wall = kernel32.GetTickCount64() / 1000.0
    up_awake = _unbiased_awake_seconds()

    candidate = newest_power_on()
    # 事件时间必须落在本次内核会话之内，否则说明日志读岔了
    if candidate and 0.0 <= now - candidate <= up_wall + 300.0:
        return candidate, "电源事件"

    if 0.0 < up_awake <= up_wall + 300.0:
        return now - up_awake, "系统清醒时长"

    try:  # psutil 只是最后一道保险，没装就算了
        import psutil

        boot = psutil.boot_time()
        if 0.0 < boot < now:
            return boot, "系统启动时间"
    except Exception:  # noqa: BLE001
        pass

    return now, "程序启动"


__all__ = ["session_start", "newest_power_on"]
