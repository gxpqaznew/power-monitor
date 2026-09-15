"""开机能耗统计 —— 常驻系统托盘，统计本次开机至今的整机能耗。

数据来源：
    GPU  NVIDIA NVML 实测（零驱动、零管理员权限）
    CPU  PDH ``% Processor Utility`` 驱动的功耗模型（估算）
    基础  主板 / 内存 / 存储 / 风扇 的经验常量
"""

__version__ = "1.0.7"
APP_NAME = "开机能耗统计"
APP_TITLE = "节能管家"
