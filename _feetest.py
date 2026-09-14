"""电价设置窗口的功能自检（不需要人工点击）。

建窗口 → 读下拉框 → 换省份 → 换方案 → 改数字 → 保存，
全程直接调窗口自己的方法，只把「文件写哪儿」换成临时目录。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import config as config_mod  # noqa: E402
from powermon import tariffs  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.fee_dialog import CUSTOM_LABEL, CUSTOM_PLAN_LABEL, FeeSettingsDialog  # noqa: E402
from powermon.w32 import user32  # noqa: E402

IDC = {
    "region": 100,
    "plan": 101,
    "peak": 110,
    "flat": 111,
    "dry": 112,
    "wet": 113,
    "peak_hours": 120,
    "valley_hours": 121,
    "months": 122,
    "note": 130,
    "source": 131,
    "base": 162,
    "monitor": 163,
    "calib": 164,
}

CB_GETCOUNT = 0x0146
CB_GETLBTEXT = 0x0148
CB_GETLBTEXTLEN = 0x0149

failures: list[str] = []
checks = 0


def check(name: str, got, want) -> None:
    global checks
    checks += 1
    if got == want:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        failures.append(name)


def combo_item(dlg: FeeSettingsDialog, cid: int, index: int) -> str:
    """直接问控件要第 index 项的文字。

    这一条专门盯住「字符串指针活不过调用」那类坑：下拉框的选中下标可能
    全对，但里面存的其实是野内存里的垃圾字符。
    """
    handle = dlg._controls[cid]
    length = user32.SendMessageW(handle, CB_GETLBTEXTLEN, index, 0)
    if length < 0:
        return "<无此项>"
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.SendMessageW(
        handle, CB_GETLBTEXT, index, ctypes.cast(buf, ctypes.c_void_p).value or 0
    )
    return buf.value


def combo_count(dlg: FeeSettingsDialog, cid: int) -> int:
    return int(user32.SendMessageW(dlg._controls[cid], CB_GETCOUNT, 0, 0))


def combo_text(dlg: FeeSettingsDialog, cid: int) -> str:
    """下拉框当前显示的那一行。"""
    handle = dlg._controls[cid]
    length = user32.GetWindowTextLengthW(handle) + 2
    buf = ctypes.create_unicode_buffer(length)
    user32.GetWindowTextW(handle, buf, length)
    return buf.value


def main() -> int:
    enable_dpi_awareness()
    tmp = Path(tempfile.mkdtemp(prefix="feetest-"))
    config_mod.CONFIG_PATH = tmp / "config.json"

    cfg = Config()
    cfg.save()
    print(f"临时配置：{config_mod.CONFIG_PATH}")

    saved_calls: list[int] = []
    dlg = FeeSettingsDialog(cfg, on_saved=lambda: saved_calls.append(1))

    print("\n[1] 建窗口")
    check("窗口创建成功", dlg.create(), True)
    check("拿到窗口句柄", bool(dlg.hwnd), True)
    check("控件都建出来了", len(dlg._controls), 34)

    print("\n[2] 打开时定位到当前配置（默认四川·第一档）")
    check("省份下拉框选中四川", dlg._selected_region().name, "四川")
    check("方案下拉框选中第一档", dlg._selected_plan().label, "一户一表 第一档")
    check("峰段电价填入", dlg._text(IDC["peak"]), "0.5224")
    check("平段电价填入", dlg._text(IDC["flat"]), "0.5224")
    check("谷段枯水填入", dlg._text(IDC["dry"]), "0.2535")
    check("谷段丰水填入", dlg._text(IDC["wet"]), "0.1750")
    check("谷段时段填入", dlg._text(IDC["valley_hours"]), "23-7")
    check("峰段时段为空", dlg._text(IDC["peak_hours"]), "")
    check("丰水期月份填入", dlg._text(IDC["months"]), "6,7,8,9,10")
    check("其他功耗填入", dlg._text(IDC["base"]), "35")
    check("显示器功耗默认 0（未计入）", dlg._text(IDC["monitor"]), "0")
    check("校准系数默认 1.00", dlg._text(IDC["calib"]), "1.00")

    print("\n[2b] 下拉框里存的确实是中文（防野指针）")
    check("省份项数 = 30 省 + 1 自定义", combo_count(dlg, IDC["region"]), len(tariffs.REGIONS) + 1)
    check("省份第 0 项 = 自定义", combo_item(dlg, IDC["region"], 0), CUSTOM_LABEL)
    check("省份第 1 项 = 四川", combo_item(dlg, IDC["region"], 1), "四川")
    check("省份末项 = 最后一个省", combo_item(dlg, IDC["region"], len(tariffs.REGIONS)),
          tariffs.REGIONS[-1].name)
    check("省份当前显示 = 四川", combo_text(dlg, IDC["region"]), "四川")
    check("方案第 0 项 = 一户一表 第一档", combo_item(dlg, IDC["plan"], 0), "一户一表 第一档")
    check("方案当前显示 = 一户一表 第一档", combo_text(dlg, IDC["plan"]), "一户一表 第一档")

    print("\n[3] 换到浙江 → 应变成峰谷两段价")
    zj = tariffs.region_names().index("浙江") + 1
    dlg._combo_fill(IDC["region"], [CUSTOM_LABEL] + tariffs.region_names(), zj)
    dlg._on_region_changed()
    check("省份=浙江", dlg._selected_region().name, "浙江")
    check("方案数=4", len(dlg._plan_index), 4)
    check("默认取第一档峰谷", dlg._selected_plan().label, "一户一表 第一档 · 峰谷")
    check("峰段 0.5680", dlg._text(IDC["peak"]), "0.5680")
    check("平段 0.5380", dlg._text(IDC["flat"]), "0.5380")
    check("谷段 0.2880", dlg._text(IDC["dry"]), "0.2880")
    check("谷丰 = 谷枯（无季节差）", dlg._text(IDC["wet"]), "0.2880")
    check("峰时段 8-22", dlg._text(IDC["peak_hours"]), "8-22")
    check("谷时段 22-8", dlg._text(IDC["valley_hours"]), "22-8")
    check("丰水期清空", dlg._text(IDC["months"]), "")
    check("方案下拉框显示也跟着换", combo_text(dlg, IDC["plan"]), "一户一表 第一档 · 峰谷")
    check("来源提示已更新", "浙江" in dlg._text(IDC["source"]) or "浙江" in dlg._text(IDC["source"]) + dlg._text(IDC["note"]), True)

    print("\n[4] 换方案 → 不分时，三个价应相等")
    dlg._combo_fill(IDC["plan"], tariffs.plan_labels(dlg._selected_region()), 3)
    dlg._on_plan_changed()
    check("方案=不分时", dlg._selected_plan().label, "一户一表 不分时")
    check("峰=平=谷", (dlg._text(IDC["peak"]), dlg._text(IDC["flat"]), dlg._text(IDC["dry"])),
          ("0.5380", "0.5380", "0.5380"))

    print("\n[5] 手工改一个数字后保存 → 应标注「已手动调整」")
    dlg._combo_fill(IDC["region"], [CUSTOM_LABEL] + tariffs.region_names(),
                    tariffs.region_names().index("浙江") + 1)
    dlg._on_region_changed()
    dlg._set_text(IDC["peak"], "0.6100")
    dlg._save()
    check("保存回调被调用", saved_calls, [1])
    check("保存后窗口关闭", dlg.is_open, False)
    check("地区=浙江", cfg.tariff_region, "浙江")
    check("方案带已调整标记", cfg.tariff_plan, "一户一表 第一档 · 峰谷（已手动调整）")
    check("峰段=0.61", cfg.price_peak, 0.61)
    check("平段=0.538", cfg.price_flat, 0.538)
    check("峰时段=8-22", cfg.peak_hours, "8-22")

    print("\n[6] 配置真的落盘了")
    text = config_mod.CONFIG_PATH.read_text(encoding="utf-8")
    check("config.json 含 0.61", '"price_peak": 0.61' in text, True)
    check("config.json 含地区", '"tariff_region": "浙江"' in text, True)

    print("\n[7] 重开窗口 → 应还原上次保存的值")
    dlg2 = FeeSettingsDialog(cfg, on_saved=None)
    check("重新创建", dlg2.create(), True)
    check("省份还原=浙江", dlg2._selected_region().name, "浙江")
    check("峰段还原=0.6100", dlg2._text(IDC["peak"]), "0.6100")

    print("\n[8] 选「自定义」保存 → 标记为手动填写")
    dlg2._combo_fill(IDC["region"], [CUSTOM_LABEL] + tariffs.region_names(), 0)
    dlg2._on_region_changed()
    check("自定义时方案只有一项", len(dlg2._plan_index), 1)
    check("方案=手动填写", dlg2._text(IDC["note"]) != "", True)
    dlg2._save()
    check("地区=自定义", cfg.tariff_region, "自定义")
    check("方案=手动填写", cfg.tariff_plan, CUSTOM_PLAN_LABEL)
    check("价格保留不动", cfg.price_peak, 0.61)
    check("来源=用户手动设置", cfg.tariff_source, "用户手动设置")

    print("\n[9] 功耗模型：其他功耗 / 显示器 / 校准系数 可调")
    dlg3 = FeeSettingsDialog(cfg, on_saved=None)
    check("重新创建", dlg3.create(), True)
    dlg3._set_text(IDC["base"], "45")
    dlg3._set_text(IDC["monitor"], "32")
    dlg3._set_text(IDC["calib"], "1.15")
    dlg3._save()
    check("其他功耗=45", cfg.baseline_watts, 45.0)
    check("显示器功耗=32", cfg.monitor_watts, 32.0)
    check("计入显示器=True", cfg.include_monitor, True)
    check("校准系数=1.15", cfg.calibration, 1.15)
    check("config.json 含校准", '"calibration": 1.15' in
          config_mod.CONFIG_PATH.read_text(encoding="utf-8"), True)

    print("\n[10] 重开窗口 → 功耗模型还原；显示器填 0 = 不计入")
    dlg4 = FeeSettingsDialog(cfg, on_saved=None)
    check("重新创建", dlg4.create(), True)
    check("其他功耗还原", dlg4._text(IDC["base"]), "45")
    check("显示器还原", dlg4._text(IDC["monitor"]), "32")
    check("校准还原", dlg4._text(IDC["calib"]), "1.15")
    dlg4._set_text(IDC["monitor"], "0")
    dlg4._save()
    check("显示器=0 视为不计入", cfg.include_monitor, False)
    check("原瓦数保留备用", cfg.monitor_watts, 32.0)
    dlg4.destroy()

    dlg2.destroy()
    print(f"\n共 {checks} 项，失败 {len(failures)} 项")
    for name in failures:
        print(f"  - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
