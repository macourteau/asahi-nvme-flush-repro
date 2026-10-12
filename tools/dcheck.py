#!/usr/bin/env python3
"""dcheck: power-loss durability checker in the style of diskchecker.pl (stdlib only).

Three subcommands:

  listen  Runs on a second machine that keeps power. It records, for each run,
          the highest sequence number the writer reported as durable. It
          appends every report to a log and fsyncs that log on each new
          maximum, so a crash of the listener does not lose evidence.
  write   Runs on the node under test. It appends framed records to FILE.
          After each record's sync call (fdatasync, fsync, O_DSYNC or
          O_DIRECT|O_DSYNC) returns, it sends "A <seq>" to the listener.
          It sends "W <seq>" before the write, so the listener also knows the
          highest record that was in flight. With --marker it also writes
          "A <run> <variant> <seq>" to a tracefs trace_marker, so acks and
          NVMe flushes share one clock (see flushtrace.py analyze). Mode
          direct_nosync (O_DIRECT without O_DSYNC, acked when pwrite returns)
          is the no-durability control for the FUA test.
          The listener treats 2 s of silence as a disconnect, because a power
          cut sends no FIN, and prints a GO line once every live writer's ack
          count is advancing.
  verify  Runs on the node after it powers back up. It scans FILE, finds the
          last valid record, asks the listener for that run's highest
          acknowledged seq, and reports any acknowledged record that is
          missing (a durability violation). It exits 0 if none is missing,
          1 if some are, and 2 on a usage error.

Ordering argument: "A n" leaves the writer only after the sync for record n
has returned. If power dies before the listener receives it, the listener's
maximum is lower than the true one, which errs on the side of no violation.
So a reported violation is real. A missing "A" can only hide one.

Record frame (little-endian), laid out back to back from offset 0, the same
way etcd lays out its WAL:
  magic  8s  b'DCHK0002'
  run    Q   random run id, the same for every record in one file
  seq    Q   0, 1, 2, ...
  plen   I   payload length
  crc    I   zlib.crc32 over run, seq, plen and payload
  payload    plen bytes derived from seq
--record-size is the full frame size. In direct mode it must be a multiple of
4096.
"""
import argparse
import json
import mmap
import os
import random
import socket
import struct
import sys
import threading
import time
import zlib

MAGIC = b'DCHK0002'
IDLE_TIMEOUT = 2.0
HDR = struct.Struct('<8sQQII')
ALIGN = 4096


def utc(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts)) + ('%.3f' % (ts % 1))[1:]


def log(msg):
    sys.stderr.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------- listener

class Listener:
    def __init__(self, state_dir):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.lock = threading.Lock()
        self.runs = {}  # run_id(hex) -> dict(hello, ack, intent, last_ts, conn)
        self.logf = open(os.path.join(state_dir, 'events.log'), 'a')
        self._load()

    def _load(self):
        path = os.path.join(self.state_dir, 'events.log')
        with open(path) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                r = self.runs.setdefault(ev['run'], {'hello': {}, 'ack': -1, 'intent': -1, 'last_ts': 0, 'conn': False})
                if ev['t'] == 'hello':
                    r['hello'] = ev['hello']
                elif ev['t'] == 'ack':
                    r['ack'] = max(r['ack'], ev['seq'])
                elif ev['t'] == 'intent':
                    r['intent'] = max(r['intent'], ev['seq'])
        if self.runs:
            log('loaded %d runs from %s' % (len(self.runs), path))

    def _record(self, ev, sync):
        self.logf.write(json.dumps(ev) + '\n')
        self.logf.flush()
        if sync:
            os.fsync(self.logf.fileno())

    def handle(self, conn, addr):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # A power cut sends no FIN or RST, so without a timeout the
        # connection would look alive until TCP gives up minutes later.
        # Every writer sends several lines per second, so 2 s of silence
        # means the writer (or its node) is gone.
        conn.settimeout(IDLE_TIMEOUT)
        f = conn.makefile('rb')
        run = None
        last_intent_logged = 0.0
        try:
            for raw in f:
                parts = raw.decode('ascii', 'replace').split(None, 1)
                if not parts:
                    continue
                cmd = parts[0]
                if cmd == 'H':
                    hello = json.loads(parts[1])
                    run = hello['run']
                    with self.lock:
                        r = self.runs.setdefault(run, {'hello': hello, 'ack': -1, 'intent': -1, 'last_ts': 0, 'conn': True})
                        r['hello'] = hello
                        r['conn'] = True
                        self._record({'t': 'hello', 'run': run, 'hello': hello, 'ts': time.time()}, True)
                    log('HELLO %s from %s: %s' % (run, addr[0], json.dumps(hello, sort_keys=True)))
                elif cmd in ('A', 'W') and run:
                    seq = int(parts[1])
                    with self.lock:
                        r = self.runs[run]
                        r['last_ts'] = time.time()
                        if cmd == 'A' and seq > r['ack']:
                            r['ack'] = seq
                            # fsync per ack would throttle the writer; the
                            # summary line written on disconnect is fsynced,
                            # and the in-memory value serves verify.
                            self._record({'t': 'ack', 'run': run, 'seq': seq, 'ts': r['last_ts']}, False)
                        elif cmd == 'W' and seq > r['intent']:
                            r['intent'] = seq
                            now = r['last_ts']
                            if now - last_intent_logged > 0.5:
                                self._record({'t': 'intent', 'run': run, 'seq': seq, 'ts': now}, False)
                                last_intent_logged = now
                elif cmd == 'Q':
                    q = parts[1].strip()
                    with self.lock:
                        r = self.runs.get(q)
                        resp = {'run': q, 'known': r is not None}
                        if r:
                            resp.update(ack=r['ack'], intent=r['intent'], hello=r['hello'], last_ts=r['last_ts'], connected=r['conn'])
                    conn.sendall((json.dumps(resp) + '\n').encode())
                else:
                    log('ignoring line from %s: %r' % (addr[0], raw[:80]))
        except socket.timeout:
            log('connection from %s: no data for %.1fs, treating as disconnected' % (addr[0], IDLE_TIMEOUT))
        except (ConnectionError, OSError) as e:
            log('connection from %s error: %s' % (addr[0], e))
        finally:
            if run:
                with self.lock:
                    r = self.runs[run]
                    r['conn'] = False
                    # Record the final intent as well: the throttled intent
                    # log may lag behind, and verify reads it after a restart.
                    self._record({'t': 'intent', 'run': run, 'seq': r['intent'], 'ts': time.time()}, False)
                    self._record({'t': 'ack', 'run': run, 'seq': r['ack'], 'ts': time.time()}, True)
                    log('DISCONNECT %s (%s): last ack=%d last intent=%d, last message at %s UTC (%.3fs ago)'
                        % (run, r['hello'].get('variant'), r['ack'], r['intent'], utc(r['last_ts']), time.time() - r['last_ts']))
            conn.close()

    def status_loop(self, every):
        prev = {}
        while True:
            time.sleep(every)
            with self.lock:
                live = [(k, v) for k, v in self.runs.items() if v['conn']]
                lines, advancing = [], 0
                for k, v in live:
                    if k in prev:
                        rate = (v['ack'] - prev[k]) / every
                        advancing += rate > 0
                        rate_s = '%7.0f acks/s' % rate
                    else:
                        rate_s = '    (new)'
                    prev[k] = v['ack']
                    lines.append('  %s %-28s ack=%-9d intent=%-9d %s' % (k, v['hello'].get('variant', '?'), v['ack'], v['intent'], rate_s))
            if lines:
                # Cut the power only after a GO line.
                verdict = 'GO: all %d writers advancing' % len(lines) if advancing == len(lines) else 'WAIT: %d of %d writers advancing' % (advancing, len(lines))
                log('live runs (%s):\n%s' % (verdict, '\n'.join(lines)))


def cmd_listen(a):
    L = Listener(a.state_dir)
    srv = socket.socket(socket.AF_INET6 if ':' in a.bind else socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((a.bind, a.port))
    srv.listen(64)
    log('listening on %s:%d, state in %s' % (a.bind, a.port, a.state_dir))
    threading.Thread(target=L.status_loop, args=(a.status_every,), daemon=True).start()
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=L.handle, args=(conn, addr), daemon=True).start()


# ---------------------------------------------------------------- writer

def frame(run, seq, size):
    plen = size - HDR.size
    pattern = struct.pack('<Q', seq ^ 0x5DEECE66D)
    payload = (pattern * (plen // 8 + 1))[:plen]
    crc = zlib.crc32(struct.pack('<QQI', run, seq, plen) + payload)
    return HDR.pack(MAGIC, run, seq, plen, crc) + payload


def mount_of(path):
    """Return (mountpoint, fstype, options, superoptions) for path, from /proc/self/mountinfo."""
    path = os.path.realpath(path)
    best = None
    try:
        with open('/proc/self/mountinfo') as f:
            for line in f:
                left, right = line.split(' - ', 1)
                lf, rf = left.split(), right.split()
                mp = lf[4].replace('\\040', ' ')
                if path == mp or path.startswith(mp.rstrip('/') + '/') or mp == '/':
                    if best is None or len(mp) > len(best[0]):
                        best = (mp, rf[0], lf[5], rf[2] if len(rf) > 2 else '')
    except OSError:
        pass
    return best


def read_first(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def lsattr_flags(path):
    # FS_IOC_GETFLAGS: report the NOCOW (C, 0x00800000) and compression
    # (c, 0x4) flags without depending on e2fsprogs being installed.
    try:
        import fcntl
        fd = os.open(path, os.O_RDONLY)
        try:
            buf = fcntl.ioctl(fd, 0x80086601, b'\0' * 8)
        finally:
            os.close(fd)
        v = struct.unpack('<q', buf)[0]
        return {'nocow': bool(v & 0x00800000), 'compr': bool(v & 0x4), 'raw': hex(v)}
    except (OSError, ImportError) as e:
        return {'error': str(e)}


# macOS lacks fdatasync; the fallback only serves local dry runs of the tool.
fdatasync = getattr(os, 'fdatasync', os.fsync)


def cmd_write(a):
    if a.mode in ('direct_dsync', 'direct_nosync') and a.record_size % ALIGN:
        sys.exit('direct mode needs --record-size multiple of %d' % ALIGN)
    if a.record_size <= HDR.size:
        sys.exit('--record-size must exceed %d' % HDR.size)
    if os.path.exists(a.file):
        sys.exit('%s exists; verify or remove it first' % a.file)
    host, port = a.listener.rsplit(':', 1)
    sock = socket.create_connection((host, int(port)), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if a.mode in ('odsync', 'direct_dsync'):
        flags |= os.O_DSYNC
    if a.mode in ('direct_dsync', 'direct_nosync'):
        flags |= os.O_DIRECT
    fd = os.open(a.file, flags, 0o600)
    if a.prealloc:
        if hasattr(os, 'posix_fallocate'):
            os.posix_fallocate(fd, 0, a.prealloc)
        else:
            os.ftruncate(fd, a.prealloc)
    if a.prewrite:
        if not a.prealloc:
            sys.exit('--prewrite needs --prealloc')
        # Turn unwritten (fallocated) extents into written ones, so later
        # O_DIRECT|O_DSYNC writes are pure overwrites. On ext4/xfs that makes
        # iomap send them as FUA writes with no flush, which tests whether
        # the device honours FUA. Uses a separate buffered fd.
        zfd = os.open(a.file, os.O_WRONLY)
        chunk = b'\0' * (1 << 20)
        done = 0
        while done < a.prealloc:
            done += os.pwrite(zfd, chunk[:min(len(chunk), a.prealloc - done)], done)
        os.fsync(zfd)
        os.close(zfd)
    run = random.getrandbits(63)
    # The sidecar lets verify find the run even if record 0 itself is lost.
    with open(a.file + '.run', 'w') as sf:
        json.dump({'run': '%016x' % run, 'record_size': a.record_size}, sf)
        sf.flush()
        os.fsync(sf.fileno())
    # Make the file's existence and size durable before the first ack, and
    # the directory entries too, so verify never finds the file missing for a
    # reason unrelated to the data path under test.
    os.fsync(fd)
    dfd = os.open(os.path.dirname(os.path.abspath(a.file)), os.O_RDONLY)
    os.fsync(dfd)
    os.close(dfd)

    mnt = mount_of(a.file)
    hello = {
        'run': '%016x' % run, 'variant': a.variant, 'file': os.path.abspath(a.file),
        'host': socket.gethostname(), 'mode': a.mode, 'record_size': a.record_size,
        'prealloc': a.prealloc, 'prewrite': a.prewrite, 'interval': a.interval, 'marker': bool(a.marker),
        'mount': mnt, 'attrs': lsattr_flags(a.file),
        'kernel': os.uname().release,
        'flush_interval_param': read_first('/sys/module/nvme_apple/parameters/flush_interval', 'n/a'),
        'cmdline_flush': [w for w in read_first('/proc/cmdline').split() if 'flush_interval' in w],
        'write_cache': read_first('/sys/block/nvme0n1/queue/write_cache', 'n/a'),
        'started': time.time(),
    }
    sock.sendall(('H %s\n' % json.dumps(hello)).encode())
    # Each ack is also written to the tracefs instance's trace_marker, so the
    # ack and the device flushes land in one trace on one clock (flushtrace.py
    # stream/analyze turns that into the predicted loss for this pull).
    mfd = None
    if a.marker:
        try:
            mfd = os.open(a.marker, os.O_WRONLY)
        except OSError as e:
            log('cannot open trace marker %s: %s; continuing without markers' % (a.marker, e))
    log('run %s variant=%s file=%s mode=%s' % (hello['run'], a.variant, a.file, a.mode))

    buf = mmap.mmap(-1, a.record_size)  # page-aligned, valid for O_DIRECT
    seq, off = 0, 0
    t_end = time.time() + a.duration if a.duration else None
    while t_end is None or time.time() < t_end:
        if a.prealloc and off + a.record_size > a.prealloc:
            log('reached preallocated size; stopping (use a bigger --prealloc)')
            break
        buf.seek(0)
        buf.write(frame(run, seq, a.record_size))
        sock.sendall(b'W %d\n' % seq)
        n = os.pwrite(fd, buf, off)
        if n != a.record_size:
            sys.exit('short write %d at %d' % (n, off))
        if a.mode == 'fdatasync':
            fdatasync(fd)
        elif a.mode == 'fsync':
            os.fsync(fd)
        sock.sendall(b'A %d\n' % seq)
        if mfd is not None:
            try:
                os.write(mfd, b'A %s %s %d' % (hello['run'].encode(), a.variant.encode(), seq))
            except OSError as e:
                log('trace marker write failed (%s); markers off from seq %d' % (e, seq))
                os.close(mfd)
                mfd = None
        seq += 1
        off += a.record_size
        if a.interval:
            time.sleep(a.interval)
    os.close(fd)
    sock.close()
    log('stopped cleanly at seq %d' % (seq - 1))


# ---------------------------------------------------------------- verify

def scan(path, record_size):
    """Walk records from offset 0. Return (last_good_seq, run, end_offset, stop_reason, tail_info)."""
    size = os.path.getsize(path)
    run = None
    seq_expected = 0
    off = 0
    reason = 'eof'
    with open(path, 'rb') as f:
        while off + HDR.size <= size:
            f.seek(off)
            rec = f.read(record_size)
            if len(rec) < record_size:
                reason = 'short tail (%d bytes)' % len(rec)
                break
            magic, r, s, plen, crc = HDR.unpack_from(rec)
            if rec == b'\0' * record_size:
                reason = 'zero record'
                break
            if magic != MAGIC:
                reason = 'bad magic (garbage or stale block)'
                break
            if run is None:
                run = r
            if r != run or s != seq_expected or plen != record_size - HDR.size:
                reason = 'unexpected run/seq/len (run %x seq %d, expected seq %d)' % (r, s, seq_expected)
                break
            if zlib.crc32(struct.pack('<QQI', r, s, plen) + rec[HDR.size:]) != crc:
                reason = 'crc mismatch (torn record)'
                break
            seq_expected += 1
            off += record_size
        # Look past the stop point for any valid record of this run: that
        # would mean later writes survived while earlier ones did not, i.e.
        # the device or fs reordered persistence (a hole, not a clean tail).
        later = []
        probe = off + record_size
        while run is not None and probe + HDR.size <= size and len(later) < 5:
            f.seek(probe)
            rec = f.read(record_size)
            if len(rec) == record_size and rec[:8] == MAGIC:
                _, r, s, plen, crc = HDR.unpack_from(rec)
                if r == run and zlib.crc32(struct.pack('<QQI', r, s, plen) + rec[HDR.size:]) == crc:
                    later.append(s)
            probe += record_size
    return seq_expected - 1, run, off, reason, later


def infer_record_size(path, default):
    # The first record's payload length gives the frame size; fall back to
    # --record-size when record 0 itself is gone.
    with open(path, 'rb') as f:
        hdr = f.read(HDR.size)
    if len(hdr) == HDR.size:
        magic, _, seq, plen, _ = HDR.unpack(hdr)
        if magic == MAGIC and seq == 0 and 0 < plen < (64 << 20):
            return HDR.size + plen
    return default


def query(listener, run_hex):
    host, port = listener.rsplit(':', 1)
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(('Q %s\n' % run_hex).encode())
        return json.loads(s.makefile('rb').readline())


def cmd_verify(a):
    rc = 0
    for path in a.files:
        if not os.path.exists(path):
            print('%s: MISSING (file itself not durable)' % path)
            rc = 1
            continue
        run_hex, rs = None, a.record_size
        try:
            with open(path + '.run') as sf:
                side = json.load(sf)
            run_hex, rs = side['run'], side['record_size']
        except (OSError, ValueError, KeyError):
            rs = infer_record_size(path, a.record_size)
        ack, intent, variant = a.ack, None, '?'
        if a.listener and run_hex:
            # Ask the listener before scanning the file: if a writer were
            # still running, the file can then only be ahead of the ack.
            info = query(a.listener, run_hex)
            if not info.get('known'):
                print('%s: listener does not know run %s' % (path, run_hex))
                rc = max(rc, 2)
                continue
            if info.get('connected'):
                print('%s: WARNING writer for run %s is still connected; this is a dry run, not a power-loss result' % (path, run_hex))
            ack, intent, variant = info['ack'], info['intent'], info['hello'].get('variant')
            if info.get('last_ts'):
                print('%s: listener got the last message from this writer at %s UTC (compare with the pull time)' % (path, utc(info['last_ts'])))
        last, run, end, reason, later = scan(path, rs)
        if a.listener and not run_hex and run is not None:
            # Sidecar lost but the records name their run: ask with that.
            run_hex = '%016x' % run
            info = query(a.listener, run_hex)
            if info.get('known'):
                ack, intent, variant = info['ack'], info['intent'], info['hello'].get('variant')
            print('%s: sidecar missing; run %s recovered from the records' % (path, run_hex))
        if run is not None and run_hex and '%016x' % run != run_hex:
            print('%s: record run %016x does not match sidecar %s' % (path, run, run_hex))
            rc = max(rc, 2)
            continue
        run_hex = run_hex or (run is not None and '%016x' % run) or '?'
        if ack is None:
            print('%s: run %s last on-disk seq %d; no ack value (give --listener or --ack)' % (path, run_hex, last))
            rc = max(rc, 2)
            continue
        lost = ack - last
        verdict = 'OK' if lost <= 0 else 'VIOLATION'
        print('%s: variant=%s run=%s on_disk_last=%d acked_last=%d intent_last=%s stop=%r end_off=%d'
              % (path, variant, run_hex, last, ack, intent, reason, end))
        if later:
            print('  REORDER: valid later records %s exist beyond the stop point' % later)
        if lost > 0:
            print('  %s: %d acknowledged records lost (%.1f KiB)' % (verdict, lost, lost * rs / 1024.0))
            rc = 1
        else:
            print('  %s: every acknowledged record is present (%d unacknowledged in-flight records also on disk)' % (verdict, last - ack))
    sys.exit(rc)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest='cmd', required=True)

    l = sp.add_parser('listen')
    l.add_argument('--bind', default='0.0.0.0')
    l.add_argument('--port', type=int, default=5400)
    l.add_argument('--state-dir', default='./dcheck-state')
    l.add_argument('--status-every', type=float, default=5.0)

    w = sp.add_parser('write')
    w.add_argument('--listener', required=True, help='host:port')
    w.add_argument('--file', required=True)
    w.add_argument('--variant', required=True, help='label, e.g. btrfs-cow-fdatasync')
    w.add_argument('--mode', default='fdatasync', choices=['fdatasync', 'fsync', 'odsync', 'direct_dsync', 'direct_nosync'],
                   help='direct_nosync: O_DIRECT without O_DSYNC, acked when pwrite returns; the no-durability control for the FUA test')
    w.add_argument('--marker', help='tracefs trace_marker to write each ack to, e.g. /sys/kernel/tracing/instances/nvmeflush/trace_marker')
    w.add_argument('--record-size', type=int, default=ALIGN)
    w.add_argument('--prealloc', type=int, default=0, help='bytes to fallocate up front (etcd uses 64000000)')
    w.add_argument('--prewrite', action='store_true', help='write zeros over the preallocated range first (see FUA test)')
    w.add_argument('--interval', type=float, default=0.0, help='sleep between records (s)')
    w.add_argument('--duration', type=float, default=0.0, help='stop after N s (0 = until power is cut)')

    v = sp.add_parser('verify')
    v.add_argument('files', nargs='+')
    v.add_argument('--listener', help='host:port')
    v.add_argument('--ack', type=int, help='last acknowledged seq, if the listener is unavailable')
    v.add_argument('--record-size', type=int, default=ALIGN)

    a = p.parse_args()
    {'listen': cmd_listen, 'write': cmd_write, 'verify': cmd_verify}[a.cmd](a)


if __name__ == '__main__':
    main()
