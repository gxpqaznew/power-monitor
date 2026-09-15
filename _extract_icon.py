from PIL import Image

SRC = r"C:\Users\gxp\WorkBuddy\2026-09-14-11-38-51\power-monitor\app.ico"
DST = r"C:\Users\gxp\WorkBuddy\2026-09-12-15-43-31\remotion-videos\public\promo\appicon.png"

im = Image.open(SRC)
sizes = sorted(im.ico.sizes()) if hasattr(im, "ico") and im.ico else [im.size]
print("sizes in ico:", sizes)
im.size = sizes[-1]
im = im.convert("RGBA")
print("chosen", im.size)
im.save(DST)
print("saved", DST)
