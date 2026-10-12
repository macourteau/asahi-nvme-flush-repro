#!/usr/bin/env python3
"""Minimal etcd v3 WAL record walker (no deps). Prints record summary,
the last valid record offset, and what lies after it (zeros / data)."""
import struct, sys, zlib

def varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7f) << s; s += 7
        if c < 0x80:
            return r, i

def fields(b):
    i, out = 0, {}
    while i < len(b):
        k, i = varint(b, i)
        f, wt = k >> 3, k & 7
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 2:
            n, i = varint(b, i); v = b[i:i+n]; i += n
        elif wt == 5:
            v = struct.unpack_from('<I', b, i)[0]; i += 4
        elif wt == 1:
            v = struct.unpack_from('<Q', b, i)[0]; i += 8
        else:
            raise ValueError('wt %d' % wt)
        out.setdefault(f, []).append(v)
    return out

# Castagnoli CRC32 (etcd uses crc32.MakeTable(crc32.Castagnoli), chained)
T = []
for n in range(256):
    c = n
    for _ in range(8):
        c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
    T.append(c)
def crc32c(crc, data):
    crc ^= 0xffffffff
    for x in data:
        crc = T[(crc ^ x) & 0xff] ^ (crc >> 8)
    return crc ^ 0xffffffff

TYPES = {1: 'metadata', 2: 'entry', 3: 'state', 4: 'crc', 5: 'snapshot'}
data = open(sys.argv[1], 'rb').read()
verbose_tail = int(sys.argv[2]) if len(sys.argv) > 2 else 15
off, crc, recs = 0, 0, []
while off + 8 <= len(data):
    lf = struct.unpack_from('<Q', data, off)[0]
    if lf == 0:
        break
    ln = lf & ((1 << 56) - 1)
    pad = (lf >> 56) & 0x7 if lf >> 63 else 0
    rec = data[off+8: off+8+ln]
    if len(rec) < ln:
        print('TORN: frame at %d claims %d bytes, file ends' % (off, ln)); break
    f = fields(rec)
    typ = f.get(1, [0])[0]; rcrc = f.get(2, [0])[0]; body = f.get(3, [b''])[0]
    if typ == 4:
        crc = rcrc
        ok = True
    else:
        crc = crc32c(crc, body)
        ok = (crc == rcrc)
    info = ''
    if typ == 2:
        e = fields(body); info = 'term=%d index=%d etype=%d datalen=%d' % (e.get(2, [0])[0], e.get(3, [0])[0], e.get(1, [0])[0], len(e.get(4, [b''])[0]))
    elif typ == 3:
        h = fields(body); info = 'HardState term=%d vote=%x commit=%d' % (h.get(1, [0])[0], h.get(2, [0])[0], h.get(3, [0])[0])
    elif typ == 5:
        s = fields(body); info = 'snap index=%d term=%d' % (s.get(1, [0])[0], s.get(2, [0])[0])
    recs.append((off, 8 + ln + pad, TYPES.get(typ, typ), ok, info))
    if not ok:
        print('CRC MISMATCH at %d (%s)' % (off, info)); break
    off += 8 + ln + pad

print('records=%d' % len(recs))
for r in recs[:5]:
    print('  off=%-9d len=%-6d %-8s crc_ok=%s %s' % r)
print('  ...')
for r in recs[-verbose_tail:]:
    print('  off=%-9d len=%-6d %-8s crc_ok=%s %s' % r)
end = off
print('end of valid records at byte %d (0x%x); mod4096=%d mod16384=%d' % (end, end, end % 4096, end % 16384))
tail = data[end:]
nz = [i for i in range(0, len(tail), 4096) if any(tail[i:i+4096])]
print('bytes after end: %d; nonzero 4K blocks after end: %d' % (len(tail), len(nz)))
if nz:
    print('  first nonzero blocks at offsets:', [end + i for i in nz[:10]])
