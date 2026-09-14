import struct, sys, zlib
from pathlib import Path

def read_png(path):
    d = Path(path).read_bytes()
    i = 8; idat = b''; w = h = 0
    while i < len(d):
        ln = struct.unpack('>I', d[i:i+4])[0]; tag = d[i+4:i+8]
        data = d[i+8:i+8+ln]; i += 12 + ln
        if tag == b'IHDR':
            w, h, bd, ct = struct.unpack('>IIBB', data[:10])
        elif tag == b'IDAT':
            idat += data
    raw = zlib.decompress(idat); stride = w * 3; rows = []; pos = 0
    for _ in range(h):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos+stride]); pos += stride
        prev = rows[-1] if rows else bytearray(stride)
        if f == 1:
            for x in range(3, stride): line[x] = (line[x] + line[x-3]) & 255
        elif f == 2:
            for x in range(stride): line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x-3] if x >= 3 else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x-3] if x >= 3 else 0; b = prev[x]; c = prev[x-3] if x >= 3 else 0
                p = a + b - c; pa, pb, pc = abs(p-a), abs(p-b), abs(p-c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        rows.append(line)
    return w, h, rows

def chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)

src, dst, x0, y0, x1, y1, z = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:8])
w, h, rows = read_png(src)
ow, oh = x1 - x0, y1 - y0
out = bytearray()
for y in range(oh):
    out.append(0)
    row = rows[y0 + y]
    for x in range(ow):
        out += row[(x0 + x) * 3:(x0 + x) * 3 + 3]
if z != 1:
    big = bytearray()
    for y in range(oh * z):
        big.append(0)
        row = out[1 + (y // z) * ow * 3: 1 + (y // z) * ow * 3 + ow * 3]
        for x in range(ow * z):
            big += row[(x // z) * 3:(x // z) * 3 + 3]
    out = big; ow *= z; oh *= z
png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', ow, oh, 8, 2, 0, 0, 0))
       + chunk(b'IDAT', zlib.compress(bytes(out), 6)) + chunk(b'IEND', b''))
Path(dst).write_bytes(png)
print(dst, f'{ow}x{oh}')
