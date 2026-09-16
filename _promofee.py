"""宣传片素材：把「电价设置」窗口渲染成干净的 PNG。

不直接用 ``_feepreview.py`` 那两张：一张是屏幕抓取（旁边别人的窗口会漏进来），
另一张被脚本切成了浙江。宣传片要的是**用户自己的配置**（四川 / 一户一表第三档），
而且要和面板那张截图口径一致，所以这里用 ``Config.load()`` 重新渲染。

输出：``_preview/fee_promo.png``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _feepreview import capture, pump  # noqa: E402

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.fee_dialog import FeeSettingsDialog  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"


def main() -> int:
    enable_dpi_awareness()
    OUT.mkdir(exist_ok=True)

    cfg = Config.load()
    print(f"电价配置：{cfg.tariff_region} / {cfg.tariff_plan} / 平段 {cfg.price_flat}")

    dlg = FeeSettingsDialog(cfg)
    if not dlg.create():
        print("窗口创建失败")
        return 1
    dlg.show()
    pump(1.2)

    out = OUT / "fee_promo.png"
    capture(dlg.hwnd, out, "print")     # PrintWindow：只画窗口自己，不带桌面背景
    dlg.destroy()
    print(f"已写出 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
