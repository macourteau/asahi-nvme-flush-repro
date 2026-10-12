#!/usr/bin/env python3
"""Small write+sync latency probe (stdlib only).

usage: fsync_bench.py DIR [--n N] [--mode fdatasync|fsync|odsync|direct_dsync] [--gap SECONDS]

Writes 4 KiB at successive offsets of a 4 MiB preallocated file in DIR and
times each write+sync. --gap sleeps between iterations (to observe flushes
issued more than nvme_apple.flush_interval apart). Deletes the file at exit.
"""
import argparse, mmap, os, statistics, sys, time

p = argparse.ArgumentParser()
p.add_argument('dir')
p.add_argument('--n', type=int, default=500)
p.add_argument('--mode', default='fdatasync', choices=['fdatasync', 'fsync', 'odsync', 'direct_dsync'])
p.add_argument('--gap', type=float, default=0.0)
p.add_argument('--size', type=int, default=4 << 20)
a = p.parse_args()

path = os.path.join(a.dir, '.fsync_bench.%d' % os.getpid())
flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
if a.mode in ('odsync', 'direct_dsync'):
    flags |= os.O_DSYNC
if a.mode == 'direct_dsync':
    flags |= os.O_DIRECT
fd = os.open(path, flags, 0o600)
try:
    os.posix_fallocate(fd, 0, a.size)
    os.fsync(fd)
    buf = mmap.mmap(-1, 4096)  # page-aligned buffer, valid for O_DIRECT
    lat = []
    for i in range(a.n):
        buf.seek(0)
        buf.write(i.to_bytes(8, 'little') * 512)
        off = (i * 4096) % a.size
        t0 = time.perf_counter_ns()
        os.pwrite(fd, buf, off)
        if a.mode == 'fdatasync':
            os.fdatasync(fd)
        elif a.mode == 'fsync':
            os.fsync(fd)
        lat.append((time.perf_counter_ns() - t0) / 1000.0)
        if a.gap:
            time.sleep(a.gap)
finally:
    os.close(fd)
    os.unlink(path)

lat_sorted = sorted(lat)
q = lambda f: lat_sorted[min(len(lat_sorted) - 1, int(f * len(lat_sorted)))]
slow = sum(1 for x in lat if x > 2000)
total_s = sum(lat) / 1e6
print('dir=%s mode=%s n=%d gap=%.2fs  p10=%.0fus p50=%.0fus p90=%.0fus p99=%.0fus max=%.0fus mean=%.0fus  >2ms: %d (%.1f%%)  busy-time=%.2fs'
      % (a.dir, a.mode, a.n, a.gap, q(.10), q(.5), q(.9), q(.99), lat_sorted[-1], statistics.mean(lat), slow, 100.0 * slow / len(lat), total_s))
