"""功耗数据采集。

三层数据源，全部零驱动、零管理员权限：

1. **CPU 负载** —— 直接调用 Windows PDH（性能数据助手）读取
   ``\\Processor Information(_Total)\\% Processor Utility``。这个计数器是
   Windows 8 引入的"实际有用功"指标，比 ``% Processor Time`` 更贴近真实功耗
   强度：空闲时贴近 0，满载时可短暂超过 100（turbo 超发）。走 ctypes 直连
   pdh.dll，不需要每次采样都起一个进程。

2. **GPU 功耗** —— NVIDIA NVML（``nvidia-ml-py``）进程内读取，毫瓦精度。
   NVML 不可用时回退到 ``nvidia-smi``。

3. **整机补足** —— 主板、内存、NVMe、风扇等没有传感器可读，用经验常量。

全程只用 ctypes 直连系统 DLL，不引入 psutil 这类编译扩展。
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass

# NVML 可选：没有 N 卡时整个模块依然要能 import
try:  # pragma: no cover - 取决于机器
    import pynvml
except Exception:  # noqa: BLE001
    pynvml = None  # type: ignore[assignment]


# ----------------------------------------------------------------- 系统 CPU 占用


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class SystemCpuLoad:
    """整机 CPU 占用率（0~100），PDH 不可用时的兜底。

    直接读 ``GetSystemTimes`` 的差值。注意它的 KernelTime **包含** IdleTime，
    所以要减掉才是真正的忙时。返回值按「全部核心合计」归一化到 0~100，
    与 PDH 的 ``% Processor Utility`` 同量纲——
    psutil 那种 0~100×核心数 的口径在这里会把满载判成 800%，反而算错功耗。
    """

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetSystemTimes.argtypes = [
            ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        self._kernel32.GetSystemTimes.restype = wintypes.BOOL
        self._last: tuple[int, int, int] | None = None

    @staticmethod
    def _ticks(ft: _FileTime) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def read(self) -> float | None:
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not self._kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        current = (self._ticks(idle), self._ticks(kernel), self._ticks(user))
        previous = self._last
        self._last = current
        if previous is None:
            return None

        d_idle = current[0] - previous[0]
        d_kernel = current[1] - previous[1]
        d_user = current[2] - previous[2]
        total = d_kernel + d_user
        if total <= 0:
            return None
        busy = total - d_idle
        return max(0.0, min(busy / total * 100.0, 100.0))


# --------------------------------------------------------------------------- PDH


class _PdhFmtCounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class ProcessorUtilityCounter:
    """``\\Processor Information(_Total)\\% Processor Utility`` 读取器。"""

    _PDH_FMT_DOUBLE = 0x00000200
    _PDH_FMT_NOSCALE = 0x00001000
    _PDH_FMT_NOCAP100 = 0x00008000

    _PATH = r"\Processor Information(_Total)\% Processor Utility"

    def __init__(self) -> None:
        self._pdh = None
        self._query = wintypes.HANDLE()
        self._counter = wintypes.HANDLE()
        self._ok = False
        try:
            self._bind()
        except (OSError, AttributeError):
            self._ok = False

    def _bind(self) -> None:
        pdh = ctypes.WinDLL("pdh.dll")
        pdh.PdhOpenQueryW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        pdh.PdhOpenQueryW.restype = wintypes.DWORD

        pdh.PdhAddEnglishCounterW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD

        pdh.PdhAddCounterW.argtypes = pdh.PdhAddEnglishCounterW.argtypes
        pdh.PdhAddCounterW.restype = wintypes.DWORD

        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhCollectQueryData.restype = wintypes.DWORD

        pdh.PdhGetFormattedCounterValue.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_PdhFmtCounterValue),
        ]
        pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD

        pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        pdh.PdhCloseQuery.restype = wintypes.DWORD

        if pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)) != 0:
            return

        # 英文计数器名在中文系统上同样可用；失败时退回按本地名解析
        rc = pdh.PdhAddEnglishCounterW(
            self._query, self._PATH, 0, ctypes.byref(self._counter)
        )
        if rc != 0:
            rc = pdh.PdhAddCounterW(
                self._query, self._PATH, 0, ctypes.byref(self._counter)
            )
        if rc != 0:
            pdh.PdhCloseQuery(self._query)
            return

        # 第一次采集只建立基线，拿不到有效值
        pdh.PdhCollectQueryData(self._query)
        self._pdh = pdh
        self._ok = True

    @property
    def available(self) -> bool:
        return self._ok

    def read(self) -> float | None:
        """返回利用率百分比（可 >100）；不可用或读数失败返回 None。"""
        if not self._ok or self._pdh is None:
            return None
        pdh = self._pdh
        if pdh.PdhCollectQueryData(self._query) != 0:
            return None
        val = _PdhFmtCounterValue()
        flags = self._PDH_FMT_DOUBLE | self._PDH_FMT_NOSCALE | self._PDH_FMT_NOCAP100
        if pdh.PdhGetFormattedCounterValue(
            self._counter, flags, None, ctypes.byref(val)
        ) != 0:
            return None
        return float(val.doubleValue)

    def close(self) -> None:
        if self._ok and self._pdh is not None:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except OSError:
                pass
        self._ok = False


# --------------------------------------------------------------------------- GPU


class NvidiaGpu:
    """NVIDIA 显卡功耗读取（NVML 优先，nvidia-smi 兜底）。"""

    def __init__(self) -> None:
        self._handles: list = []
        self._names: list[str] = []
        self._limits: list[float] = []
        self._backend = "none"
        self._init_nvml()

    def _init_nvml(self) -> None:
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            for idx in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                try:
                    limit = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
                except Exception:  # noqa: BLE001
                    limit = 0.0
                # 预热：第一次调用会做驱动侧初始化，避免首帧异常
                pynvml.nvmlDeviceGetPowerUsage(handle)
                self._handles.append(handle)
                self._names.append(name)
                self._limits.append(limit)
            if self._handles:
                self._backend = "nvml"
        except Exception:  # noqa: BLE001
            self._handles = []
            self._names = []
            self._limits = []
            self._backend = "none"

    @property
    def available(self) -> bool:
        return bool(self._handles) or self._backend == "smi"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def names(self) -> list[str]:
        return list(self._names)

    @property
    def limits(self) -> list[float]:
        return list(self._limits)

    def read(self) -> tuple[float | None, list[float]]:
        """返回 (合计功耗 W, 每卡功耗 W)。失败返回 (None, [])。"""
        if self._handles:
            per: list[float] = []
            for handle in self._handles:
                try:
                    per.append(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    continue
            if per:
                return sum(per), per
            return None, []
        return self._read_smi()

    def _read_smi(self) -> tuple[float | None, list[float]]:
        """nvidia-smi 兜底（约 60~120ms，仅在 NVML 不可用时使用）。"""
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None, []
        if out.returncode != 0:
            return None, []
        per = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                per.append(float(line))
            except ValueError:
                continue
        if not per:
            return None, []
        return sum(per), per

    def close(self) -> None:
        if pynvml is not None and self._handles:
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- CPU


class CpuPowerEstimator:
    """CPU 功耗模型。

    ``P = idle + (ppt - idle) * (util/100) ** exponent``

    以 PDH 的 ``% Processor Utility`` 为负载输入；取不到时退化为
    ``GetSystemTimes`` 的整机占用率。输出是**估算值**，典型误差 ±15~20%。
    """

    def __init__(
        self,
        ppt: float,
        idle: float,
        exponent: float,
    ) -> None:
        self._ppt = ppt
        self._idle = idle
        self._exponent = exponent
        self._pdh = ProcessorUtilityCounter()
        self._fallback = SystemCpuLoad()
        # 非阻塞预热：差值法需要两次采样才有结果
        self._fallback.read()

    @property
    def precise_source(self) -> bool:
        """True 表示负载取自 PDH 的实际有用功计数器。"""
        return self._pdh.available

    def read(self) -> tuple[float, float]:
        """返回 (估算功耗 W, 负载百分比)。"""
        util = self._pdh.read()
        if util is None:
            util = self._fallback.read()
        if util is None:
            util = 0.0
        util = max(0.0, util)
        normalized = min(util / 100.0, 1.0)
        fraction = normalized**self._exponent
        watts = self._idle + (self._ppt - self._idle) * fraction
        return watts, util

    def update(self, ppt: float, idle: float, exponent: float) -> None:
        self._ppt = ppt
        self._idle = idle
        self._exponent = exponent

    def close(self) -> None:
        self._pdh.close()


# --------------------------------------------------------------------------- 汇总


@dataclass
class Reading:
    """一次采样结果。"""

    ts: float
    total_w: float
    cpu_w: float
    gpu_w: float
    base_w: float
    cpu_util: float
    cpu_estimated: bool
    gpu_measured: bool
    gpu_count: int
    cpu_source: str
    gpu_source: str


class SensorHub:
    """把三个数据源合成一个整机功率读数。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._gpu = NvidiaGpu()
        self._cpu = CpuPowerEstimator(
            ppt=cfg.cpu_ppt,
            idle=cfg.cpu_idle,
            exponent=cfg.cpu_load_exponent,
        )

    @property
    def gpu(self) -> NvidiaGpu:
        return self._gpu

    @property
    def cpu(self) -> CpuPowerEstimator:
        return self._cpu

    def reload(self, cfg) -> None:
        self._cfg = cfg
        self._cpu.update(cfg.cpu_ppt, cfg.cpu_idle, cfg.cpu_load_exponent)

    def read(self) -> Reading:
        gpu_w, per_gpu = self._gpu.read()
        gpu_measured = gpu_w is not None
        if gpu_w is None:
            gpu_w = 0.0

        cpu_w, cpu_util = self._cpu.read()

        base_w = self._cfg.baseline_watts
        if self._cfg.include_monitor:
            base_w += self._cfg.monitor_watts

        return Reading(
            ts=time.time(),
            total_w=cpu_w + gpu_w + base_w,
            cpu_w=cpu_w,
            gpu_w=gpu_w,
            base_w=base_w,
            cpu_util=cpu_util,
            cpu_estimated=True,
            gpu_measured=gpu_measured,
            gpu_count=len(per_gpu),
            cpu_source="PDH 负载模型" if self._cpu.precise_source else "系统占用率模型",
            gpu_source=self._gpu.backend if gpu_measured else "无",
        )

    def close(self) -> None:
        self._gpu.close()
        self._cpu.close()
