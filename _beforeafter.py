r"""拼一张「改造前 / 改造后」的长条对比图，用来一眼看出把手删掉了。

两张源图是**同一套抓法、同一坐标**取出来的：都是 `_striplive.py 1300 0 xxx.png auto`
（`crop_w=1300`, `x0=0`），快照数据也一样 —— 所以两张图的长条逐像素落在同一个位置，
可以直接叠着比。

- `strip_live.png`：当前代码抓的。
- `strip_old.png`：把 v1.0.10（最后一个带把手的版本）的代码放进临时 git worktree 跑
  同一条命令抓的，抓完归档到 `_preview/` 里留着复现：
  ```bat
  git worktree add %TEMP%\pm_old 27e6376
  cd /d %TEMP%\pm_old && <python> _striplive.py 1300 0 strip_old.png auto
  copy %TEMP%\pm_old\_preview\strip_old.png ..\_preview\
  ```

用法：python _beforeafter.py
输出：_preview/before_after.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_preview" / "before_after.png"
NEW = ROOT / "_preview" / "strip_live.png"
OLD = ROOT / "_preview" / "strip_old.png"

# 只看长条那一块（左边界贴开始按钮左侧，右边留一点余量），整幅抓图会把两边图标带进来
BOX = (108, 0, 900, 72)
SCALE = 1.6
PAD = 18
LABEL_H = 30

ROWS = [
    ("改造前 v1.0.10 ｜ 两端各一列小圆点当「拖动把手」（用户：太丑了）", OLD),
    ("改造后 v1.0.11 ｜ 干净读数条，整块直接按住拖（悬停才出强调描边）", NEW),
]


def load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def main() -> int:
    missing = [str(p) for _, p in ROWS if not p.exists()]
    if missing:
        print("缺图：")
        for m in missing:
            print("  ", m)
        return 1

    tiles = []
    for label, path in ROWS:
        im = Image.open(path).convert("RGB").crop(BOX)
        im = im.resize((int(im.width * SCALE), int(im.height * SCALE)), Image.LANCZOS)
        print(f"{path.name}: 裁出 {BOX} → {im.size}")
        tiles.append((label, im))

    W = max(im.width for _l, im in tiles) + PAD * 2
    H = PAD + sum(LABEL_H + im.height + PAD for _l, im in tiles)
    sheet = Image.new("RGB", (W, H), (245, 246, 248))
    draw = ImageDraw.Draw(sheet)
    font = load_font(19)

    y = PAD
    for label, im in tiles:
        draw.text((PAD, y + 4), label, font=font, fill=(26, 30, 36))
        y += LABEL_H
        sheet.paste(im, (PAD, y))
        draw.rectangle([PAD - 1, y - 1, PAD + im.width, y + im.height],
                       outline=(210, 214, 220))
        y += im.height + PAD

    sheet.save(OUT)
    print(f"已写出 {OUT}  {sheet.size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
