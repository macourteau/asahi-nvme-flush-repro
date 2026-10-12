#!/usr/bin/env python3
"""flushtrace: see which NVMe flushes reach the device, and line them up with
dcheck acks on one clock (stdlib only).

  sudo python3 -I flushtrace.py snap SECONDS [--keep FILE] [--fake-us US]
      Trace for SECONDS on the node and print a summary.
  sudo python3 -I flushtrace.py stream
      Print the raw trace to stdout until killed. Run it over ssh from the
      listener host for the whole pull (ssh -n node 'sudo python3 -I
      .../flushtrace.py stream' > pullN.trace), started before the writers.
  python3 -I flushtrace.py fix
      Print whether nvme_apple.flush_interval=0 is in effect (sysfs AND cmdline).
  python3 -I flushtrace.py analyze FILE [--listener HOST:PORT] [--fake-us US] [--cut T]
      Offline (on any machine): summary plus, per dcheck run, the acks written
      after the last device flush started, i.e. the acks a power cut at the
      end of the trace would lose if only device flushes made data durable.

All modes share one tracefs instance, /sys/kernel/tracing/instances/NAME,
where NAME is $FLUSHTRACE_INSTANCE (default "nvmeflush"); the global tracer
is left alone. The writers' --marker path is that instance's trace_marker.
Events: nvme_setup_cmd for I/O-queue flushes and writes, nvme_complete_rq for
I/O queues, and the writers' "A <run> <variant> <seq>" markers.

Asahi's nvme-apple driver completes a REQ_OP_FLUSH inline, never sending it,
when the previous device flush was less than nvme_apple.flush_interval ms ago.
Those complete in about 1-5 us when idle and form a tight cluster. Flushes
are split at --fake-us (default 600 us). Under the heavy E1 load on an M1
(AP0256Q), flush latencies in the six power-cut traces had a gap between
582 us and 785 us. Below it is the slow tail of in-driver completions (a
327 us flush was shown to be swallowed by the power-cut result; two flushes
of 578 and 582 us could not be classified and did not affect E). Above it,
every flush reached the controller. 600 us is inside that gap; a 200 us
cutoff counted swallowed flushes as device flushes. Other SSDs may need another value:
the latency histogram is always printed so the clusters can be checked by
eye. After the fix the <10 us cluster must be gone. A real but fast flush
counted as in-driver only overstates exposure, which is the safe direction.

analyze --cut T treats the trace as ending at trace time T (seconds, as in the
trace's timestamp column), to line the analysis up with the pull time.
"""
import json
import os
import re
import signal
import socket
import statistics
import sys
import time

INST = '/sys/kernel/tracing/instances/' + os.environ.get('FLUSHTRACE_INSTANCE', 'nvmeflush')
EV = re.compile(r'\s(\d+\.\d+): (nvme_setup_cmd|nvme_complete_rq): .*?qid=(\d+), cmdid=(\d+)')
MARK = re.compile(r'\s(\d+\.\d+): tracing_mark_write: A ([0-9a-f]{16}) (\S+) (\d+)')
BUCKETS = [(0, 10), (10, 50), (50, 100), (100, 200), (200, 500), (500, 1e12)]


def w(path, val):
    with open(path, 'w') as f:
        f.write(val)


def setup():
    os.mkdir(INST)
    w(INST + '/buffer_size_kb', '16384')
    w(INST + '/events/nvme/nvme_setup_cmd/filter', 'qid!=0 && (opcode==0 || opcode==1)')
    w(INST + '/events/nvme/nvme_complete_rq/filter', 'qid!=0')
    w(INST + '/events/nvme/nvme_setup_cmd/enable', '1')
    w(INST + '/events/nvme/nvme_complete_rq/enable', '1')


def teardown():
    try:
        w(INST + '/tracing_on', '0')
    except OSError:
        pass
    # rmdir fails with EBUSY while a writer still holds trace_marker open.
    for _ in range(50):
        try:
            os.rmdir(INST)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            err = e
            time.sleep(0.1)
    sys.stderr.write('flushtrace: could not remove %s: %s (node.sh trace-stop retries)\n' % (INST, err))


def read_first(path, default='n/a'):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def fix_status():
    """PASS only if both the boot-time module value and the command line say 0.
    A runtime write to the sysfs parameter reads back 0 but does not change
    the driver, which copied the value at probe."""
    param = read_first('/sys/module/nvme_apple/parameters/flush_interval')
    cmdline = read_first('/proc/cmdline', '')
    on_cmdline = 'nvme_apple.flush_interval=0' in cmdline.split()
    modprobe = any('flush_interval=0' in read_first(os.path.join('/etc/modprobe.d', f), '')
                   for f in (os.listdir('/etc/modprobe.d') if os.path.isdir('/etc/modprobe.d') else []))
    ok = param == '0' and on_cmdline
    return 'fix %s: sysfs flush_interval=%s, cmdline arg %s, modprobe.d %s' % (
        'ACTIVE' if ok else 'NOT ACTIVE', param, 'present' if on_cmdline else 'absent', 'present' if modprobe else 'absent')


def analyze(text, fake_us, listener=None, quiet_fix=False, cut=None):
    pend = {}
    flushes = []          # (setup_t, complete_t, us)
    fua_lat, plain_lat = [], []
    marks = {}            # run -> dict(variant, times[], seqs[])
    first_t = last_t = None
    for line in text.splitlines():
        m = MARK.search(line) or EV.search(line)
        if m and cut is not None and float(m.group(1)) > cut:
            break
        m = MARK.search(line)
        if m:
            t = float(m.group(1))
            r = marks.setdefault(m.group(2), {'variant': m.group(3), 't': [], 'seq': []})
            r['t'].append(t)
            r['seq'].append(int(m.group(4)))
            first_t = t if first_t is None else first_t
            last_t = t
            continue
        m = EV.search(line)
        if not m:
            continue
        t, ev, key = float(m.group(1)), m.group(2), (m.group(3), m.group(4))
        first_t = t if first_t is None else first_t
        last_t = t
        if ev == 'nvme_setup_cmd':
            if 'nvme_cmd_flush' in line:
                pend[key] = ('flush', t)
            else:
                c = re.search(r'ctrl=0x([0-9a-f]+)', line)
                pend[key] = ('fua' if c and int(c.group(1), 16) & 0x4000 else 'plain', t)
        elif key in pend:
            kind, t0 = pend.pop(key)
            us = (t - t0) * 1e6
            if kind == 'flush':
                flushes.append((t0, t, us))
            else:
                (fua_lat if kind == 'fua' else plain_lat).append(us)

    span = (last_t - first_t) if first_t is not None else 0.0
    dev = [f for f in flushes if f[2] >= fake_us]
    fake = [f for f in flushes if f[2] < fake_us]
    print('span=%.1fs flushes=%d  completed-in-driver(<%gus)=%d  sent-to-device=%d  writes: FUA=%d plain=%d'
          % (span, len(flushes), fake_us, len(fake), len(dev), len(fua_lat), len(plain_lat)))
    if flushes:
        hist = ['%s: %d' % ('<%gus' % hi if lo == 0 else ('>=%gus' % lo if hi > 1e11 else '%g-%gus' % (lo, hi)),
                            sum(1 for f in flushes if lo <= f[2] < hi)) for lo, hi in BUCKETS]
        print('  flush latency histogram: ' + ', '.join(hist))
    if fake:
        print('  in-driver completion latency: median %.1f us' % statistics.median(f[2] for f in fake))
    if dev:
        ls = sorted(f[2] for f in dev)
        print('  device flush latency: min %.0f us, median %.0f us, p90 %.0f us, max %.0f us (n=%d)'
              % (ls[0], statistics.median(ls), ls[int(0.9 * (len(ls) - 1))], ls[-1], len(ls)))
    for name, lat in (('FUA write', fua_lat), ('plain write', plain_lat)):
        if lat:
            ls = sorted(lat)
            print('  %s latency: median %.0f us, p90 %.0f us (n=%d)' % (name, statistics.median(ls), ls[int(0.9 * (len(ls) - 1))], len(ls)))
    if len(dev) > 1:
        gaps = [b[0] - a[0] for a, b in zip(dev, dev[1:])]
        print('  gap between device flushes: median %.3f s, max %.3f s' % (statistics.median(gaps), max(gaps)))

    if marks:
        # A device flush makes durable everything whose write completed
        # before the flush was sent. Writers ack after their write completed,
        # so an ack marked before a device flush's setup is covered by that
        # flush, and an ack after the last setup is exposed. (Slightly
        # conservative: a flush sent just after a write but just before its
        # ack marker also covered it.)
        last_dev_setup = dev[-1][0] if dev else None
        print('  per dcheck run (sync = acks; exposed = acks after the last device-flush setup at %s):'
              % ('t=%.6f' % last_dev_setup if last_dev_setup is not None else 'none in trace'))
        for run, r in sorted(marks.items(), key=lambda kv: kv[1]['variant']):
            n = len(r['seq'])
            covered = [s for t, s in zip(r['t'], r['seq']) if last_dev_setup is not None and t < last_dev_setup]
            exposed = n - len(covered)
            dev_in_window = sum(1 for f in dev if r['t'][0] <= f[0] <= r['t'][-1])
            line = '    %s %-28s acks=%-7d device-flushes-during=%-6d (>= acks: %s) last-marked-seq=%-8d exposed=%d' % (
                run, r['variant'], n, dev_in_window, 'yes' if dev_in_window >= n else 'NO', r['seq'][-1], exposed)
            if covered:
                line += ' (last covered seq %d)' % covered[-1]
            if listener:
                try:
                    host, port = listener.rsplit(':', 1)
                    with socket.create_connection((host, int(port)), timeout=5) as s:
                        s.sendall(('Q %s\n' % run).encode())
                        info = json.loads(s.makefile('rb').readline())
                    if info.get('known'):
                        lag = info['ack'] - r['seq'][-1]
                        line += '\n      listener last ack=%d -> trace tail lag %d acks; predicted max loss if unflushed data is lost = %d' % (
                            info['ack'], lag, info['ack'] - (covered[-1] if covered else -1))
                except (OSError, ValueError) as e:
                    line += ' (listener query failed: %s)' % e
            print(line)
    if not quiet_fix and os.path.exists('/proc/cmdline') and os.path.exists('/sys/module'):
        print('  ' + fix_status())


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ('snap', 'stream', 'analyze', 'fix'):
        sys.exit(__doc__)
    mode = args[0]
    if mode == 'fix':
        print(fix_status())
        return
    fake_us = float(args[args.index('--fake-us') + 1]) if '--fake-us' in args else 600.0
    if mode == 'analyze':
        listener = args[args.index('--listener') + 1] if '--listener' in args else None
        cut = float(args[args.index('--cut') + 1]) if '--cut' in args else None
        with open(args[1], errors='replace') as f:
            analyze(f.read(), fake_us, listener, quiet_fix=True, cut=cut)
        return
    if mode == 'snap':
        secs = float(args[1]) if len(args) > 1 and not args[1].startswith('-') else 10.0
        keep = args[args.index('--keep') + 1] if '--keep' in args else None
        setup()
        try:
            time.sleep(secs)
            w(INST + '/tracing_on', '0')
            with open(INST + '/trace') as f:
                text = f.read()
        finally:
            teardown()
        if keep:
            with open(keep, 'w') as f:
                f.write(text)
        analyze(text, fake_us)
        return
    # stream: until SIGTERM/SIGINT/SIGHUP or the reader goes away
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    setup()
    try:
        sys.stdout.write('# flushtrace stream start %s UTC on %s; %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()), socket.gethostname(), fix_status()))
        sys.stdout.flush()
        with open(INST + '/trace_pipe') as tp:
            for line in tp:
                sys.stdout.write(line)
                sys.stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        teardown()


if __name__ == '__main__':
    main()
