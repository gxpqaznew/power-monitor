"""生成 exe 用的 app.ico（多尺寸，纯标准库，不依赖 Qt / Pillow）。

用法：
    python make_icon.py
"""

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.iconmake import build_pixels  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(bgra: bytes, size: int) -> bytes:
    """BGRA → PNG（真彩，无 alpha 通道压缩损失）。"""
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter: none
        row = bgra[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            b, g, r, a = row[x * 4:x * 4 + 4]
            # 预乘会让半透明边缘发暗，这里直接输出未预乘的 RGBA
            if a == 0:
                r = g = b = 0
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += chunk(b"IEND", b"")
    return out


def build_ico(target: Path) -> None:
    frames = []
    for size in SIZES:
        # 24 及以上显示瓦数，16 太小只放纯色块
        text = "" if size <= 16 else "W"
        frames.append((size, png_bytes(build_pixels(text, 0.45, size), size)))

    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = b""
    offset = len(header) + 16 * len(frames)
    body = b""
    for size, data in frames:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        body += data
        offset += len(data)

    target.write_bytes(header + entries + body)
    print(f"{target.name} 已生成：{len(frames)} 个尺寸 "
          f"({', '.join(str(s) for s in SIZES)})，{target.stat().st_size} 字节")


if __name__ == "__main__":
    build_ico(Path(__file__).resolve().parent / "app.ico")
