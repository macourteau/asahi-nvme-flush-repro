# Results

Raw outputs from one test machine: a Mac mini (M1, 2020) with an APPLE SSD
AP0256Q (firmware 591), running kernel `7.1.13-402.asahi.fc44.aarch64+16k`.
E0, E1 and E2 (pulls 1-6) ran with `nvme_apple.flush_interval=1000` (the
default); E3 (the probe, the latency run and pulls 7-11) ran with
`nvme_apple.flush_interval=0` set at boot. All runs took place on 2026-10-12.
Times are UTC.

The outputs were scrubbed before publication:

- the hostname is `test-node`;
- filesystem UUIDs are `<uuid>`;
- the `want_hash` values in fs-verity messages (content hashes of unrelated
  files) are `<redacted>`;
- the text of the previous boot's last journal lines is `<redacted>`, with
  their timestamps and sources kept;
- in `health-baseline.txt`, four kernel lines about an unrelated network block
  device (rbd) mounting and unmounting were removed.

Nothing else was changed. The `systemd-fsck-root` lines dated 2026-09-14 were
logged early in boot, before the clock was set from the network; that date is
not when the check ran.

| File | Contents |
|---|---|
| `preflight-before-E1.txt` | `node.sh preflight` before the first experiment |
| `health-baseline.txt` | `node.sh health` before the first pull |
| `E0.txt` | `e0.sh`: flush swallowing under 300 back-to-back syncs (E0a), and the device flush cost with syncs 2.5 s apart (E0b) |
| `pullN-verify.txt` | `node.sh verify` after pull N: the observed loss L per writer, plus filesystem health after the cut |
| `pullN-analyze.txt` | `flushtrace.py analyze --fake-us 600 --listener ...` on pull N's trace (600 µs is the tool's default): the predicted loss E per writer |
| `pull2-analyze-fake-us-200.txt` | pull 2 analysed with the original 200 µs threshold. One swallowed flush took 327 µs and was counted as a device flush, so E comes out as 30 where L is 123 |
| `E2-nopull-check-analyze.txt` | E2 dry run without a power cut: FUA writes are present and device flushes are seconds apart once the node is quiesced |
| `preflight-before-E3.txt` | `node.sh preflight` before pull 7, with the fix active |
| `E3-probe.txt` | `node.sh probe` with the fix, no power cut: each variant alone for 10 s under the flush trace |
| `E3-latency.txt` | `fsync_bench.py --n 1000` with the fix, three instances run at the same time |

The raw traces (5-220 MB per pull) are not included.

## Per pull

| Pull | Experiment | Cut (UTC) | Writers' last message at the listener | L / E |
|---|---|---|---|---|
| 1 | E1 | 11:02:01.41 | 11:02:01.408-.412 | cow-append 150/150, prealloc-wal 148/148, nocow-wal 150/150, ext4-wal 92/92 |
| 2 | E1 | 11:09:06.76 | 11:09:06.760-.766 | 123/123, 122/122, 122/123, 74/74 |
| 3 | E1 | 11:14:41.22 | 11:14:41.214-.219 | 25/25, 26/26, 25/26, 15/15 |
| 4 | E1 | 11:18:53.66 | 11:18:53.652-.660 | 101/101, 99/99, 99/100, 60/60 |
| 5 | E2 | 11:23:24.65 | 11:23:24.647-.649 | fua-direct 631/631, nosync-control 636/636 |
| 6 | E2 | 11:27:08.12 | 11:27:08.114-.124 | fua-direct 1089/1326, nosync-control 1093/1334 |
| 7 | E3 | 14:44:08.72 | 14:44:08.716-.721 | cow-append 0/1, prealloc-wal 0/0, nocow-wal 0/0, ext4-wal 0/1 |
| 8 | E3 | 14:51:29.46 | 14:51:29.462 | prealloc-wal alone 1/1 |
| 9 | E3 | 15:01:15.29 | 15:01:15.289-.291 | 0/0, 1/1, 1/1, 0/1 |
| 10 | E3 | 15:10:50.36 | 15:10:50.358 | prealloc-wal alone 1/1 |
| 11 | E3 | 15:16:29.38 | 15:16:29.379 | 0/0, 1/1, 0/0, 0/1 |

For pulls 7-11 the cut time is the latest writer's last message at the
listener, rounded to 10 ms. Under the fix, E is an upper bound: some device
flushes completed in under 600 µs and are counted as not reaching the device
(see the E3 section of the top-level README).

Every pull was valid. A `GO` line came less than 5 s before each cut, every
writer's last message arrived within 10 ms of the cut time, and the trace tail
lag was 0. The machine powered on by itself after every cut. After every cut,
ext4 recovered its journal; btrfs replayed its tree log after every cut except
cut 6. No pull produced btrfs checksum or transid errors, ext4 errors, or a
failed mount.

The `fs-verity ... FILE CORRUPTED` lines are a separate issue. Two were
reported before the first pull. The count rose from 2 to 10 at the first boot
after pull 1 and stayed at 10 through pull 11, drawn from a varying set of
files. All of them read back a zero block (`real_hash` is the SHA-256 of 4096
zero bytes); the two before the first pull were logged about 12 min after
boot, the rest about 10 s after boot. They concern files last written the day before by a package
update. The same reports appear on other machines of the same model running
the same kernel that were never power-cut, the files read back correctly
afterwards, and `rpm -V` reports them intact: a boot-time read-side issue,
unrelated to the flush bug.
