# asahi-nvme-flush-repro

Power-cut reproduction of lost `fsync()`/`fdatasync()` data on Apple Silicon
internal storage under Asahi Linux, with the tools, the raw per-pull results,
and the workaround.

Tested hardware and software: Mac mini (M1, 2020), APPLE SSD AP0256Q, firmware
591; kernel `7.1.13-402.asahi.fc44.aarch64+16k` (Fedora Asahi Remix 44); `/var`
on btrfs, `/boot` on ext4, both on the internal SSD.

## The bug

The Asahi `nvme-apple` driver carries a downstream patch, "apple-nvme: defer
cache flushes by a specified amount". When a cache flush (`REQ_OP_FLUSH`)
arrives less than `nvme_apple.flush_interval` milliseconds (default 1000) after
the previous real flush, the driver completes it immediately with success and
does not send it to the SSD; one real flush follows up to a second later. Any
workload that syncs more often than once a second, such as a database WAL or
etcd, therefore gets `fsync()` returning before its data is on flash. In power
cuts, every record acknowledged after the last real device flush was lost, on
ext4 and btrfs alike, and nothing before it was. The workaround,
`nvme_apple.flush_interval=0`, has to be set at boot: the driver copies the
value only at probe, so a runtime write to the sysfs parameter reads back 0
while the driver keeps deferring.

Separately, the ANS controller acknowledges FUA writes without making them
durable. In power cuts, `O_DIRECT|O_DSYNC` writes (sent as FUA writes) were lost
exactly like writes with no sync at all. ext4 (jbd2 commit block) and btrfs
(superblock) end each sync with a FUA write, so even with `flush_interval=0`
the last commit before a power cut is not durable. Only a real flush command
made data durable. The requested driver change is to stop advertising FUA for
this controller, so that the block layer emulates FUA with a flush.

This was found while investigating a 3-node etcd cluster that lost
acknowledged writes after a power cut. The tests below reproduce the loss
without etcd.

## Driver source

All line numbers refer to `drivers/nvme/host/apple.c` in
[AsahiLinux/linux](https://github.com/AsahiLinux/linux), branch `asahi` at
commit
[`77cb8f24c238`](https://github.com/AsahiLinux/linux/blob/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/nvme/host/apple.c)
(file blob `a3343c49c6af`). The deferral was added by
[`801c12513602`](https://github.com/AsahiLinux/linux/commit/801c12513602572d32acfb884a854d518489db7a)
("apple-nvme: defer cache flushes by a specified amount", Jens Axboe). Mainline
`apple.c` does not contain it.

The parameter, [l. 213-215](https://github.com/AsahiLinux/linux/blob/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/nvme/host/apple.c#L213-L215):

```c
unsigned int flush_interval = 1000;
module_param(flush_interval, uint, 0644);
MODULE_PARM_DESC(flush_interval, "Grace period in msecs between flushes");
```

The flush is swallowed, [l. 776-794](https://github.com/AsahiLinux/linux/blob/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/nvme/host/apple.c#L776-L794):

```c
	if (!anv->flush_interval || req_op(req) != REQ_OP_FLUSH)
		return false;
	if (delayed_work_pending(&anv->flush_dwork))
		return true;
	if (time_before(jiffies, anv->last_flush + anv->flush_interval)) {
		kblockd_mod_delayed_work_on(WORK_CPU_UNBOUND, &anv->flush_dwork,
						anv->flush_interval);
		...
		return true;
	}
```

and completed as successful in `apple_nvme_queue_rq()`, [l. 832-835](https://github.com/AsahiLinux/linux/blob/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/nvme/host/apple.c#L832-L835):

```c
	if (apple_nvme_delayed_flush(anv, ns, req)) {
                blk_mq_complete_request(req);
                return BLK_STS_OK;
        }
```

The value is copied into the device only in `apple_nvme_probe()`, [l. 1665-1669](https://github.com/AsahiLinux/linux/blob/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/nvme/host/apple.c#L1665-L1669):

```c
	if (flush_interval) {
		anv->flush_interval = msecs_to_jiffies(flush_interval);
		...
	}
```

When the patch was posted upstream in February 2025, Christoph Hellwig
[rejected it](https://lkml.rescloud.iu.edu/2502.1/07294.html): "Breaking file
system transactional guarantee by ignoring data integrity command in the driver
is a no-go." [nixos-apple-silicon](https://github.com/nix-community/nixos-apple-silicon/blob/c263106abdb41b2b88072d5850b04e30dc4bb23e/apple-silicon-support/modules/kernel/default.nix#L74)
sets `nvme_apple.flush_interval=0` by default.

## Method

- **Writer** (`tools/dcheck.py write`, in the style of diskchecker.pl). It
  appends fixed-size, CRC-framed records to a file and syncs each one. Only
  after the sync returns does it send the record's sequence number over TCP to
  a listener on a second machine. The listener therefore knows the highest
  record acknowledged as durable when the power is cut. A missing
  acknowledgement can only hide a loss, so a reported loss is always real.
- **Trace** (`tools/flushtrace.py stream`). A private tracefs instance records
  `nvme_setup_cmd` (flushes and writes on the I/O queues) and
  `nvme_complete_rq`. Each writer also writes every acknowledgement to the
  instance's `trace_marker`. The trace is streamed to the second machine
  during the run, so acknowledgements and NVMe commands share one clock.
- **Prediction E** (`flushtrace.py analyze`). A flush that took under the
  threshold set by `--fake-us` was completed in the driver; a slower one
  reached the device. E is the number of records acknowledged after the last
  device flush was issued. It is the loss to expect if only device flushes
  make data durable.
- **Measurement L** (`dcheck.py verify`). After power returns, the verifier
  reads each file back and reports how many acknowledged records are
  missing.
- **Cut.** Mains is pulled 60-180 s (random) after all writers are running,
  and restored after 30 s.

### Why `--fake-us 600`

`flushtrace.py` uses 600 µs as the default for `--fake-us`.

Swallowed flushes complete in 1-5 µs on an idle machine. Under the E1 load
they stretch out: in pull 2, one completed in 327 µs. With a 200 µs threshold
it was counted as a device flush, and E came out as 30 where L was 123
(`results/pull2-analyze-fake-us-200.txt`).

Pairing every flush's `nvme_setup_cmd` with its `nvme_complete_rq` in all six
power-cut traces shows that no flush took between 582 µs and 785 µs. The tail below
that gap is in-driver completions: up to 410 µs in pulls 1, 2 and 4, plus two
flushes of 578 and 582 µs in pull 3. Above the gap, the fastest flushes were
785 µs (pull 3), 831 µs (pull 4), 862 µs (pull 2) and 891 µs (pull 1). Flush
latency alone cannot say whether the two pull-3 flushes at 578 and 582 µs
reached the device. Neither was the last flush before the cut, and pull 3's E
is the same with a threshold of 500, 600 or 700 µs. The cutoff of 600 µs lies
inside the gap for all six traces. With it, E matches L to within one record
on every pull.

Counting a real flush as swallowed can only overstate E, which is the safe
direction. Check the printed histogram on your own hardware before you trust
the threshold.

## Results

Raw outputs are in `results/` (see `results/README.md`). The node was idle
apart from the test writers (E1, E3) or quiesced (E2).

### E0: flushes are swallowed (no power cut, `flush_interval=1000`)

| Run | Flushes | Completed in driver | Sent to device | Notes |
|---|---|---|---|---|
| E0a: 300 back-to-back `fdatasync`s | 302 | 300 (median 3.0 µs) | 2 (3.6 and 4.1 ms, 1.05 s apart) | `fdatasync` p50 208 µs, p99 387 µs |
| E0b: 10 `fdatasync`s 2.5 s apart | 18 | 6 | 12: median 3.4 ms, p90 3.9 ms, min 2.5 ms, max 5.8 ms | `fdatasync` p50 4.4 ms |

300 syncs in a row caused 2 device flushes. Syncs spaced more than a second
apart reach the device. They show the real cost of a flush on this SSD, a few
milliseconds. FUA writes completed in a median of 7 µs, faster than plain
writes (15 µs).

### E1: power cuts, `flush_interval=1000` (4 pulls, 4 variants)

Variants, run at the same time:

| Variant | Filesystem | Pattern |
|---|---|---|
| `btrfs-cow-append` | btrfs | 4 KiB appends, no preallocation, `fdatasync` |
| `btrfs-prealloc-wal` | btrfs | `fallocate` 256 MB, 900 B appends, `fdatasync` (etcd's WAL pattern) |
| `btrfs-nocow-wal` | btrfs, `chattr +C` | as above, nodatacow |
| `ext4-wal` | ext4 | `fallocate` 128 MB, 900 B appends, `fdatasync` |

L is the number of acknowledged records lost (verify) and E the predicted
loss (analyze, `--fake-us 600`, the default):

| Pull | btrfs-cow-append L/E | btrfs-prealloc-wal L/E | btrfs-nocow-wal L/E | ext4-wal L/E | Device flushes / all flushes |
|---|---|---|---|---|---|
| 1 | 150/150 | 148/148 | 150/150 | 92/92 | 433 / 118848 |
| 2 | 123/123 | 122/122 | 122/123 | 74/74 | 425 / 118617 |
| 3 | 25/25 | 26/26 | 25/26 | 15/15 | 322 / 91340 |
| 4 | 101/101 | 99/99 | 99/100 | 60/60 | 267 / 75544 |

All 4 pulls lost data on every variant. L equals E to within one record on
both filesystems: everything acknowledged after the last device flush was lost,
and nothing acknowledged before it. The SSD did not drain its cache before
power failed, and the power supply did not hold up long enough to cover the
window. Pull validity: every writer's last message reached the listener within
10 ms of the cut, and the trace had no tail lag.

### E2: FUA writes (2 pulls, node quiesced, `flush_interval=1000`)

Two writers ran on ext4 over prewritten, preallocated files:
`ext4-fua-direct` (`O_DIRECT|O_DSYNC` overwrites, which the trace shows going
out as FUA writes with no flush) and `ext4-nosync-control` (the same
`O_DIRECT` overwrites without `O_DSYNC`, acknowledged when `pwrite` returns).
Background services were stopped and ext4 remounted `lazytime`, so device
flushes were seconds apart.

| Pull | ext4-fua-direct L/E | ext4-nosync-control L/E | FUA writes in trace |
|---|---|---|---|
| 5 | 631/631 | 636/636 | 5524 |
| 6 | 1089/1326 | 1093/1334 | 5301 |

The FUA writes fared no better than the unsynced control. In pull 6 both lost
about 240 records (about 5 s) less than predicted, and they did so alike: the
SSD wrote part of its cache back on its own before the cut. FUA bought
nothing.

### E3: power cuts with `flush_interval=0` (5 pulls)

The fix was set on the kernel command line and in `modprobe.d`, and
`flushtrace.py fix` reported it active before every pull. Before the first
pull, with no power cut:

- the check that the setting survives a kernel update (see
  [Workaround](#workaround-nvme_appleflush_interval0)) passed;
- `node.sh probe` ran each variant alone for 10 s under the trace
  (`results/E3-probe.txt`). No flush completed in under 200 µs, and every sync
  reached the device. In `btrfs-cow-append` and `btrfs-prealloc-wal` the
  device flushes are one fewer than the acknowledgements: the last sync fell
  at the edge of the 10 s trace window.

Pulls 7, 9 and 11 ran the four E1 variants. Pulls 8 and 10 ran
`btrfs-prealloc-wal` (etcd's WAL pattern) alone. E is the analyze `exposed`
count, as in E1:

| Pull | btrfs-cow-append L/E | btrfs-prealloc-wal L/E | btrfs-nocow-wal L/E | ext4-wal L/E | Device flushes / all flushes |
|---|---|---|---|---|---|
| 7 | 0/1 | 0/0 | 0/0 | 0/1 | 26887 / 27308 |
| 8 | - | 1/1 | - | - | 34535 / 34535 |
| 9 | 0/0 | 1/1 | 1/1 | 0/1 | 97764 / 107045 |
| 10 | - | 1/1 | - | - | 47173 / 47173 |
| 11 | 0/0 | 1/1 | 0/0 | 0/1 | 52601 / 53427 |

No writer lost more than one acknowledged record in a pull, against 15-150 in
E1. Every lost record was the one acknowledged after the last device flush:
its sync ended with the filesystem's FUA commit write, and no flush followed
before the cut. That is the residue E2 predicts.

The `<10us` histogram bucket, where E0 and E1 put the swallowed flushes, was
empty in every pull. With `flush_interval=0` the driver cannot complete a
flush itself, so every flush reached the device. Some completed in under
600 µs (421, 9281 and 826 flushes in pulls 7, 9 and 11), presumably with a
clean cache. The 600 µs threshold counts those as not reaching the device, so E is
an upper bound here. After every cut, ext4 recovered its journal and btrfs
replayed its tree log, with no checksum, transid or ext4 errors.

### Cost of `flush_interval=0`

| Measure | Value |
|---|---|
| Device flush latency, idle (E0b) | median 3.4 ms, p90 3.9 ms |
| Device flush latency, E1 load | median 3.7-4.3 ms, p90 6.8-8.2 ms |
| Device flush latency, E3 load (with the fix) | median 3.4-3.6 ms, p90 3.8-5.7 ms |
| `fdatasync` with the fix (`fsync_bench.py --n 200`, same M1 model, normal background load) | p50 4.0 ms, p99 10.9 ms |
| `fdatasync` with the fix, 3 concurrent writers (`fsync_bench.py --n 1000` each, test machine) | p50 7.9 ms, p99 11.9 ms |
| etcd `etcd_disk_wal_fsync_duration_seconds` over 30 min, member with the fix | mean 4.7 ms, 99.9% ≤ 16 ms |
| the same, two members without the fix (same cluster, same window) | mean 1.0 and 1.2 ms |

## Reproducing

**Warning.** Power cuts with the bug active lose data by design, and they can
damage the filesystems on the test machine. Use a machine you can reinstall,
and back up `/boot` first.

### Requirements

- An Apple Silicon Mac running Asahi Linux with the `nvme_apple` module
  exposing `flush_interval` (`modinfo nvme_apple | grep flush_interval`).
  The tools assume a Fedora Asahi Remix layout: btrfs `/var`, ext4 `/boot`,
  vfat `/boot/efi`, the SSD at `nvme0`/`nvme0n1`, systemd. Other layouts work
  through the environment variables documented at the top of `tools/node.sh`.
- On the test machine: root, `python3` (stdlib only), tracefs with the `nvme`
  events (`/sys/kernel/tracing/events/nvme`), `btrfs-progs`, `e2fsprogs`,
  `chattr`, `systemd-run`.
- A second machine that keeps power during the cut (the **listener host**),
  with `python3` and ssh access to the test machine. The test machine must
  be able to reach it over TCP (port 5400 below). Any OS works; the analysis
  is offline. Keep both clocks NTP-synced, so the pull time can be compared
  with the listener's timestamps.
- Physical access to the test machine's power cord. Set "start up
  automatically after a power failure" in macOS, or be ready to press the
  power button. A console helps if a filesystem needs repair.
- About 260 MB free on `/boot` and 1 GB on `/var`.

### Setup

The examples below use `test-node` for the test machine and `192.0.2.10` for
the listener host's address as the test machine sees it.

On the listener host, clone the repository, keep its path in `R`, and keep
logs and traces in a separate working directory. All the commands below run
from that working directory.

```sh
git clone <this repo> asahi-nvme-flush-repro && R=$PWD/asahi-nvme-flush-repro
mkdir -p nvme-flush-run && cd nvme-flush-run
scp "$R"/tools/{dcheck.py,flushtrace.py,node.sh,fsync_bench.py,e0.sh} test-node:/var/tmp/
ssh test-node 'sudo install -d -m 700 /root/nvme-flush-repro && \
  sudo mv /var/tmp/{dcheck.py,flushtrace.py,node.sh,fsync_bench.py,e0.sh} /root/nvme-flush-repro/ && \
  sudo chmod +x /root/nvme-flush-repro/node.sh'
```

**Run outputs identify your machines.** The preflight, verify and health
outputs, the trace header, and the listener's state and log contain the test
machine's hostname, its kernel command line (including the root filesystem
UUID), filesystem UUIDs, journal lines, and IP addresses. Scrub them before
you share them. `results/README.md` lists what was scrubbed here. The
`.gitignore` keeps these files out of the repository if you work inside it
anyway.

Stop anything that would reboot the machine mid-test, such as unattended
updates (`dnf5-automatic.timer` on Fedora). If it is a Kubernetes node, drain
it first.

E0 needs no power cut and shows whether the bug is present:

```sh
ssh -n test-node 'sudo /root/nvme-flush-repro/node.sh health' > baseline-health.txt
ssh -n test-node 'sudo bash /root/nvme-flush-repro/e0.sh' > E0.txt
```

A `<10us` bucket holding most of the flushes in E0a means flushes are being
swallowed.

### Per-pull procedure

1. **Listener**, once, on the listener host:
   `python3 -I "$R"/tools/dcheck.py listen --port 5400 --state-dir ./dcheck-state 2>> listener.log &`
2. **Preflight:** `ssh test-node 'sudo /root/nvme-flush-repro/node.sh preflight' > pullN-preflight.txt`.
   It records the hardware, the kernel, and whether the fix is active.
3. **Trace stream**, started before the writers. `-n` keeps it from reading
   stdin:
   `ssh -n test-node 'sudo python3 -I /root/nvme-flush-repro/flushtrace.py stream' > pullN.trace & echo $! > stream.pid`
4. **Writers:** `ssh test-node 'sudo /root/nvme-flush-repro/node.sh start 192.0.2.10:5400'`
   starts the four E1 variants. Use `node.sh fua 192.0.2.10:5400` for the E2
   pair. For E2, run `node.sh quiesce` first and `node.sh unquiesce` after
   the series. It stops the services in `QUIESCE_UNITS` (by default kubelet
   and containerd, skipped if absent) and remounts ext4 `lazytime`.
5. **Wait.** Wait for a `GO: all N writers advancing` line in
   `listener.log`, then a random 60-180 s (`shuf -i 60-180 -n1`). Run
   `ssh test-node 'sudo /root/nvme-flush-repro/node.sh prepull'`, which
   unmounts `/boot/efi`, and check that the next status line still reads `GO`.
6. **Cut.** Note the time (`date -u +%T.%N`) and pull the power cord. Leave
   it out for 30 s, then plug it back in.
7. **After boot:**
   `TEST_NODE=test-node LISTENER=192.0.2.10:5400 "$R"/tools/postpull.sh N`.
   It checks the listener's `DISCONNECT` lines, stops the stream, writes
   `pullN-analyze.txt` (with `--fake-us 600`), waits for ssh, writes
   `pullN-verify.txt`, deletes the test files (`node.sh clean`), and records
   the preflight for the next pull.

**Validity.** Count a pull only if all of these hold:

- the last `GO` line came within 5 s before the cut;
- every writer's "listener got the last message" time in verify is within
  about a second of the noted cut time;
- the "trace tail lag" in analyze is small compared with E.

### Reading the output

`pullN-verify.txt` has one block per file:

```
.../wal.dat: variant=ext4-wal run=... on_disk_last=22180 acked_last=22272 ...
  VIOLATION: 92 acknowledged records lost (80.9 KiB)
```

L is the number in the `VIOLATION` line, or 0 for `OK`. A `REORDER` line
means later records survived after a gap, so writes were persisted out of
order.

`pullN-analyze.txt` has one line per writer. E is the number in
`predicted max loss if unflushed data is lost = E`. The line also shows
`device-flushes-during=... (>= acks: NO)`: with the bug, there are far fewer
device flushes than acknowledgements.

- **L ≈ E > 0:** acknowledged data was lost, as much as the deferral
  predicts. This is the bug.
- **L = 0 with large E:** unflushed data survived the cut. The SSD or the
  power supply covered the window. Not seen here.
- **L > E + 1:** data was lost even though a device flush covered it. That
  would point at the firmware or the filesystem, not the deferral.

Also check the histogram for an empty band around the `--fake-us`
threshold.

## Workaround: `nvme_apple.flush_interval=0`

`nvme_apple` is a loadable module and lives in the initramfs, so set the
value both on the kernel command line and in `modprobe.d`. These are the
Fedora Asahi Remix steps:

```sh
# every existing boot entry
sudo grubby --update-kernel=ALL --args="nvme_apple.flush_interval=0"
# kernels installed later: kernel-install builds new entries from /etc/kernel/cmdline
[ -f /etc/kernel/cmdline ] && ! grep -qw 'nvme_apple.flush_interval=0' /etc/kernel/cmdline && \
  sudo sed -i 's/[[:space:]]*$/ nvme_apple.flush_interval=0/' /etc/kernel/cmdline
# module option, carried into the initramfs
echo 'options nvme_apple flush_interval=0' | sudo tee /etc/modprobe.d/nvme-apple-flush.conf
sudo dracut -f --regenerate-all
sudo systemctl reboot
```

**Checking it.** Do not rely on sysfs alone. The driver copies
`flush_interval` into the device at probe time, so
`echo 0 > /sys/module/nvme_apple/parameters/flush_interval` makes sysfs read
0 while the driver keeps deferring with 1000. Check the boot command line as
well, and confirm the behaviour:

```sh
grep -qw 'nvme_apple.flush_interval=0' /proc/cmdline && echo cmdline ok
cat /sys/module/nvme_apple/parameters/flush_interval          # 0
sudo python3 -I /root/nvme-flush-repro/flushtrace.py fix       # "fix ACTIVE"
# behavioural: the <10us cluster must be gone, and every sync must produce a device flush
sudo bash -c 'cd /root/nvme-flush-repro && mkdir -p /var/tmp/fsb && \
  (python3 -I flushtrace.py snap 8 & sleep 0.5; python3 -I fsync_bench.py /var/tmp/fsb --n 200; wait); \
  rmdir /var/tmp/fsb'
```

To check that the argument survives a kernel update, regenerate the current
kernel's boot entry and look for it:

```sh
sudo kernel-install add "$(uname -r)" "/lib/modules/$(uname -r)/vmlinuz"
sudo grep -l 'nvme_apple.flush_interval=0' /boot/loader/entries/*.conf
sudo lsinitrd | grep nvme-apple-flush.conf
```

Every value of `flush_interval` above 0 reopens a loss window, so there is no
safe middle value.

## The remaining FUA issue and the requested driver changes

With `flush_interval=0`, every flush reaches the SSD, but E2 shows that the
controller acknowledges FUA writes from its volatile cache. Filesystems
finish a sync with a FUA write after the preflush, so the last commit before a
power cut can still be lost. E3 measured this under `flush_interval=0`: at
most one record per writer per pull was lost, always the record acknowledged
after the last device flush. That is a much smaller window than with the
deferral, but it is not zero.

Requested changes:

1. Default `flush_interval` to 0, or drop the deferral. If it stays, make the
   sysfs parameter reflect the value the driver actually uses, or make it
   read-only.
2. Do not advertise FUA for the ANS controller: leave `BLK_FEAT_FUA` out of
   the queue features. The block layer then emulates a FUA write with a write
   followed by a flush, which this controller honours. `queue/fua` is
   read-only from userspace, so there is no userspace workaround.

## Known limits

- One machine model and SSD (M1, AP0256Q, firmware 591) was power-tested.
  A Mac mini (M2 Pro, 2023) with an AP0512Z showed the same flush swallowing
  in traces, but was not power-tested.
- 4 E1 pulls, 2 E2 pulls and 5 E3 pulls. The results are consistent, but
  the sample is small.
- dcheck cannot fully see the loss of only the last one or two acknowledged
  records: those acknowledgements are the ones most likely to be in flight at
  the cut. A clean result under the fix therefore does not prove that FUA is
  safe; E2 is the FUA test. (E3 nonetheless caught single-record losses.)
- E depends on the trace reaching the listener host before the cut.
  Missing tail events show up as "trace tail lag" and inflate E, never
  deflate it.
- The `--fake-us` threshold (600 µs) is empirical for this SSD. Other models may need
  a different value; the histogram shows where the gap is.
- The verify outputs also show `fs-verity ... FILE CORRUPTED` reports that
  read back a zero block (`real_hash` is the SHA-256 of 4096 zero bytes).
  They are mostly logged early in boot and concern files installed by a package
  update. The same reports appear on other machines of the same model
  running the same kernel that were never power-cut. The affected files read
  back correctly afterwards, and `rpm -V` reports them intact. This is a
  boot-time read-side issue, unrelated to the flush bug.

## Repository layout

| Path | Contents |
|---|---|
| `tools/dcheck.py` | durability checker: `listen` (listener host), `write`, `verify` (test machine) |
| `tools/flushtrace.py` | NVMe flush tracing: `snap`, `stream`, `fix` (test machine), `analyze` (anywhere) |
| `tools/node.sh` | test-machine driver: `preflight`, `health`, `start`, `fua`, `quiesce`, `prepull`, `verify`, `probe`, `clean` |
| `tools/postpull.sh` | post-pull steps, run on the listener host |
| `tools/e0.sh` | E0 flush-swallowing check and flush cost (no power cut) |
| `tools/fsync_bench.py` | write+sync latency |
| `tools/walparse.py` | etcd v3 WAL record walker, used on the original etcd member |
| `results/` | per-pull verify and analyze outputs, E0, the E3 probe and latency runs, the health baseline |

All tools are Python 3 standard library or bash. Run the Python tools with
`python3 -I`.

## Acknowledgements

The investigation, the tools, and this write-up were produced with the help
of Claude (Anthropic) and reviewed by the author.
