import os
import sys
from PIL import Image

P = r"C:\Users\gxp\WorkBuddy\2026-09-14-11-38-51\power-monitor\_preview"
names = [
    "panel_live_after.png",
    "fee_sichuan_screen.png",
    "icon_modes.png",
    "panel_state_verify.png",
    "panel_state_3chips.png",
    "panel_state_125pct.png",
    "panel_live.png",
    "panel.png",
    "strip_v104.png",
    "panel_corners_live_exe.png",
]
tiles = []
for n in names:
    p = os.path.join(P, n)
    ok = os.path.exists(p)
    if ok:
        im = Image.open(p)
        print(f"{n:34s} {im.size} {im.mode}")
        tiles.append((n, im.convert("RGB")))
    else:
        print(f"{n:34s} MISSING")

if tiles:
    # 缩放到统一宽度 640，纵向拼接
    W = 640
    rows = []
    for n, im in tiles:
        s = W / im.width
        rows.append((n, im.resize((W, max(1, int(im.height * s))), Image.LANCZOS)))
    H = sum(r.height + 8 for _, r in rows)
    sheet = Image.new("RGB", (W, H), (255, 0, 255))
    y = 0
    for _, r in rows:
        sheet.paste(r, (0, y))
        y += r.height + 8
    out = os.path.join(P, "_contact_sheet.png")
    sheet.save(out)
    print("saved", out, sheet.size)
