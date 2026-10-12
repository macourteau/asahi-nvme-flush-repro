#!/bin/bash
# Node-side driver for the power-loss reproduction. Run as root on the test
# node, with dcheck.py and flushtrace.py in the same directory.
#
#   node.sh preflight                    facts every trial records, fix status, 5 s flush snapshot
#   node.sh health [--scrub]             btrfs/ext4/verity health; --scrub adds a read-only btrfs scrub
#   node.sh start HOST:PORT [VARIANT..]  start writers (default: the E1 set; or name variants)
#   node.sh fua HOST:PORT                start the E2 pair: FUA writer + no-flush control
#   node.sh quiesce | unquiesce          E2 only: stop/start $QUIESCE_UNITS, ext4 mount lazytime on/off
#   node.sh prepull                      right before a pull: unmount $EFI_MNT, show GO checks
#   node.sh status                       writer units
#   node.sh verify HOST:PORT             after power-up: verify all files and print health
#   node.sh probe                        no pull: each sync variant alone for 10 s under the flush trace
#   node.sh trace-stop                   stop a flushtrace stream and remove its tracefs instance
#   node.sh clean                        stop writers and any stream, delete test files
#
# Writers run as transient systemd units, so they survive the ssh session and
# die only with the power. If the flushtrace stream (tracefs instance
# $FLUSHTRACE_INSTANCE) is running, writers also mark each ack in it.
# Logs: nothing is written to the filesystems under test except the test
# files. Run verify/health through ssh and save their output on the listener
# host.
#
# Environment (defaults match a Fedora Asahi Remix install: btrfs /var,
# ext4 /boot, vfat /boot/efi, all on the internal NVMe; pass through sudo
# with "sudo env NAME=value ./node.sh ..."):
#   BTRFS_DIR            test directory on btrfs                 (/var/tmp/dcheck)
#   EXT4_DIR             test directory on ext4, same SSD        (/boot/.dcheck)
#                        Both are deleted by clean. Their parent directories
#                        must exist; they must not be on tmpfs.
#   EFI_MNT              vfat mount unmounted before a pull      (/boot/efi)
#   QUIESCE_UNITS        services stopped by quiesce; units that
#                        are not installed are skipped           (kubelet containerd)
#   UPDATE_TIMER         unattended-update timer shown by preflight
#                                                                (dnf5-automatic.timer)
#   FLUSHTRACE_INSTANCE  tracefs instance name                   (nvmeflush)
#   NVME_NS              namespace whose dmesg lines health reports (nvme0n1)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

BT=${BTRFS_DIR:-/var/tmp/dcheck}   # btrfs; /var is compress=zstd:1 on Fedora Asahi
BT_NOCOW=$BT/nocow                 # chattr +C: nodatacow (and so no compression)
EXT=${EXT4_DIR:-/boot/.dcheck}     # ext4 partition on the same NVMe device
EFI_MNT=${EFI_MNT:-/boot/efi}
QUIESCE_UNITS=${QUIESCE_UNITS-kubelet containerd}
UPDATE_TIMER=${UPDATE_TIMER:-dnf5-automatic.timer}
export FLUSHTRACE_INSTANCE=${FLUSHTRACE_INSTANCE:-nvmeflush}
MARK=/sys/kernel/tracing/instances/$FLUSHTRACE_INSTANCE/trace_marker
DEFAULT_SET=(btrfs-prealloc-wal btrfs-nocow-wal btrfs-cow-append ext4-wal)

die() { echo "error: $*" >&2; exit 2; }
# The filesystems holding the test directories (for health, quiesce, preflight).
BTRFS_MNT=$(findmnt -no TARGET --target "${BT%/*}") || die "cannot resolve the mount of ${BT%/*}"
EXT4_MNT=$(findmnt -no TARGET --target "${EXT%/*}") || die "cannot resolve the mount of ${EXT%/*}"
# Units from $QUIESCE_UNITS that exist on this node.
quiesce_units() { local u; for u in $QUIESCE_UNITS; do systemctl cat "$u" >/dev/null 2>&1 && echo "$u"; done; true; }
units() { systemctl list-units --all --plain --no-legend 'dck-*' 2>/dev/null | awk '{print $1}'; }

fixline() { python3 -I flushtrace.py fix; }

preflight() {
  echo "host:            $(hostname)"
  echo "model:           $(tr -d '\0' </proc/device-tree/model)"
  echo "ssd:             $(xargs </sys/class/nvme/nvme0/model) fw $(xargs </sys/class/nvme/nvme0/firmware_rev)"
  echo "kernel:          $(uname -r)"
  echo "cmdline:         $(cat /proc/cmdline)"
  fixline
  echo "write_cache:     $(cat /sys/block/nvme0n1/queue/write_cache)   fua: $(cat /sys/block/nvme0n1/queue/fua)"
  echo "$BTRFS_MNT:            $(findmnt -no FSTYPE,OPTIONS "$BTRFS_MNT")"
  echo "$EXT4_MNT:           $(findmnt -no FSTYPE,OPTIONS "$EXT4_MNT")   free: $(df -h --output=avail "$EXT4_MNT" | tail -1 | xargs)"
  echo "$EFI_MNT:       $(findmnt -no OPTIONS "$EFI_MNT" 2>/dev/null || echo 'not mounted')"
  local u; for u in $(quiesce_units); do echo "$u: $(systemctl is-active "$u" || true)"; done
  echo "$UPDATE_TIMER:  $(systemctl is-enabled "$UPDATE_TIMER" 2>/dev/null || echo 'not installed')"
  echo "uptime:          $(uptime)"
  if [ -e "${MARK%/trace_marker}" ]; then
    echo "flush snapshot:  skipped (the flushtrace stream holds the $FLUSHTRACE_INSTANCE instance)"
  else
    echo "flush snapshot (5 s):"; python3 -I flushtrace.py snap 5
  fi
}

health() {
  local bootdev; bootdev=$(findmnt -no SOURCE "$EXT4_MNT")
  echo "--- btrfs device stats (non-zero only):"; btrfs device stats "$BTRFS_MNT" | grep -v ' 0$' || echo "all zero"
  # Only messages about the internal SSD (NVME_NS, default nvme0n1) and its
  # driver; other block devices (network or USB disks) are left out.
  local ns=${NVME_NS:-nvme0n1}
  echo "--- dmesg counts: btrfs csum/transid/corrupt=$(dmesg | grep -iE 'BTRFS.*(csum|transid|corrupt|error)' | grep "$ns" | grep -vc 'scanned by' || true)  fs-verity CORRUPTED=$(dmesg | grep 'fs-verity.*CORRUPTED' | grep -c "$ns" || true)  EXT4 errors=$(dmesg | grep -iE 'EXT4-fs (error|warning)' | grep -c "$ns" || true)"
  echo "--- dmesg fs/nvme lines:"; dmesg | grep -iE 'btrfs|ext4|nvme|fs-verity|FAT-fs' | grep -E "$ns|nvme-apple|nvme nvme" | tail -25
  echo "--- $EXT4_MNT ext4 ($bootdev):"; tune2fs -l "$bootdev" | grep -iE 'filesystem state|errors behavior|error count|first error|last error|mount count'
  echo "--- fsck units this boot (fstab passno 0 for $EXT4_MNT means none is expected):"
  journalctl -b --no-pager -o short-iso -u 'systemd-fsck*' 2>/dev/null | tail -5 || true
  if [ "${1:-}" = --scrub ]; then
    echo "--- read-only btrfs scrub of $BTRFS_MNT (minutes):"; btrfs scrub start -Bdr "$BTRFS_MNT"
  fi
}

launch() {  # unit name, then dcheck write args
  local unit=$1; shift
  local marker=()
  [ -w "$MARK" ] && marker=(--marker "$MARK")
  systemd-run --quiet --unit "$unit" --collect -p StandardError=journal \
    /usr/bin/python3 -I "$PWD/dcheck.py" write "${marker[@]}" "$@"
}

variant() {  # name listener
  local v=$1 l=$2
  case $v in
    # etcd's WAL pattern: fallocate()d file, ~900 B appends, fdatasync. The
    # fallocate sets BTRFS_INODE_PREALLOC, so btrfs writes into the
    # preallocated extents in place and rewrites partial blocks by
    # uncompressed COW (should_nocow/run_delalloc_nocow in 7.1): never compressed.
    btrfs-prealloc-wal)    mkdir -p "$BT/cow"; launch "dck-$v" --listener "$l" --variant "$v" --file "$BT/cow/wal.dat" --record-size 900 --prealloc 256000000 --interval 0.005 ;;
    btrfs-nocow-wal)       mkdir -p "$BT_NOCOW"; chattr +C "$BT_NOCOW"
                           launch "dck-$v" --listener "$l" --variant "$v" --file "$BT_NOCOW/wal.dat" --record-size 900 --prealloc 256000000 --interval 0.005 ;;
    # no fallocate: ordinary COW extents, and every fdatasync also has to log
    # the new i_size. Not compressed in practice either: each fdatasync writes
    # out a single 4 KiB block, and btrfs does not compress single-block
    # ranges (compsize after a smoke run: 1% of the file zstd).
    btrfs-cow-append) mkdir -p "$BT/cow"; launch "dck-$v" --listener "$l" --variant "$v" --file "$BT/cow/append.dat" --record-size 4096 --interval 0.005 ;;
    ext4-wal)              mkdir -p "$EXT"; launch "dck-$v" --listener "$l" --variant "$v" --file "$EXT/wal.dat" --record-size 900 --prealloc 128000000 --interval 0.01 ;;
    *) die "unknown variant $v" ;;
  esac
}

start() {
  local l=$1; shift
  if [ -e "$BT" ] || [ -e "$EXT" ]; then die "test files exist; run verify then clean first"; fi
  [ -w "$MARK" ] || echo "warning: flushtrace stream not running; acks will not be marked in a trace"
  local set=("$@"); [ ${#set[@]} -gt 0 ] || set=("${DEFAULT_SET[@]}")
  for v in "${set[@]}"; do variant "$v" "$l"; done
  sleep 2; status
  echo "Writers running. Wait for a GO line on the listener and at least 60 s, then prepull, then pull."
}

fua() {
  local l=$1
  if [ -e "$EXT" ]; then die "test files exist; run verify then clean first"; fi
  [ -w "$MARK" ] || echo "warning: flushtrace stream not running; E2 needs it for the exposure count"
  mkdir -p "$EXT"
  # O_DIRECT|O_DSYNC overwrites of prewritten blocks on ext4 go out as FUA
  # writes with no flush (checked in the trace: ctrl=0x4000). The control
  # does the same writes without O_DSYNC (plain writes, acked on pwrite
  # return), so it shows what an unflushed write's fate is in this same pull.
  # 128 MB each at 50 records/s lasts ~10 min; both need ~260 MB free on $EXT4_MNT.
  launch dck-ext4-fua-direct --listener "$l" --variant ext4-fua-direct --file "$EXT/fua.dat" --mode direct_dsync --record-size 4096 --prealloc 128000000 --prewrite --interval 0.02
  launch dck-ext4-nosync-control --listener "$l" --variant ext4-nosync-control --file "$EXT/control.dat" --mode direct_nosync --record-size 4096 --prealloc 128000000 --prewrite --interval 0.02
  sleep 8; status
  echo "Wait for a GO line and at least 60 s; E2 counts a pull only if analyze shows >= 50 exposed FUA acks."
}

# E2 needs device flushes to be rare, so background services that sync often
# are stopped (on a Kubernetes node: kubelet and containerd; running
# containers survive, because their shims run with KillMode=process), and the
# ext4 mount is remounted lazytime so jbd2 does not commit timestamp updates
# every few seconds.
quiesce() {
  local u; u=$(quiesce_units)
  # shellcheck disable=SC2086  # word splitting of the unit list is intended
  [ -z "$u" ] || systemctl stop $u
  mount -o remount,lazytime "$EXT4_MNT"
  echo "stopped: ${u:-none}; $EXT4_MNT remounted lazytime: $(findmnt -no OPTIONS "$EXT4_MNT")"
}

unquiesce() {
  mount -o remount,nolazytime "$EXT4_MNT" || true
  # Start in reverse order (containerd before kubelet with the defaults).
  local u units=() i
  for u in $(quiesce_units); do units=("$u" "${units[@]}"); done
  for i in "${units[@]}"; do systemctl start "$i"; done
  echo "$EXT4_MNT: $(findmnt -no OPTIONS "$EXT4_MNT")"
  for i in "${units[@]}"; do echo "$i: $(systemctl is-active "$i" || true)"; done
}

prepull() {
  # The EFI system partition (vfat) has no journal; keep it out of the cut.
  if mountpoint -q "$EFI_MNT"; then umount "$EFI_MNT" || mount -o remount,ro "$EFI_MNT"; fi
  echo "$EFI_MNT: $(findmnt -no OPTIONS "$EFI_MNT" 2>/dev/null || echo 'not mounted')"
  status
  echo "Pull only after the listener shows a GO line."
}

status() {
  local any=0
  for u in $(units); do any=1; echo "$u: $(systemctl is-active "$u" || true)"; done
  [ $any = 1 ] || echo "no writer units"
}

verify() {
  local l=$1
  shopt -s nullglob
  files=("$BT"/cow/*.dat "$BT_NOCOW"/*.dat "$EXT"/*.dat)
  [ ${#files[@]} -gt 0 ] || die "no test files found"
  preflight 2>&1 | sed -n '1,8p'
  echo "previous boot's last journal lines:"; journalctl -b -1 -n 3 --no-pager -o short-iso | cut -c1-200
  health
  set +e
  python3 -I dcheck.py verify --listener "$l" "${files[@]}"
  echo "verify exit code: $?"
}

probe() {
  # No pull. After the fix every fdatasync must reach the device: for each
  # variant alone, device flushes during the run should be >= acks.
  [ -e "${MARK%/trace_marker}" ] && die "stop the flushtrace stream first"
  if [ -e "$BT" ] || [ -e "$EXT" ]; then die "test files exist; clean first"; fi
  local st; st=$(mktemp -d /tmp/dck-probe.XXXX)   # /tmp is tmpfs: off the filesystems under test
  python3 -I dcheck.py listen --bind 127.0.0.1 --port 5498 --state-dir "$st" 2>"$st/listen.log" &
  local lp=$!; sleep 0.5
  for v in "${DEFAULT_SET[@]}"; do
    echo "=== $v alone"
    python3 -I flushtrace.py snap 11 &
    local tp=$!; sleep 0.5
    variant "$v" 127.0.0.1:5498
    sleep 10; systemctl stop "dck-$v" 2>/dev/null || true
    wait $tp
    clean_files
  done
  kill $lp; rm -rf "$st"
}

clean_files() { rm -rf "$BT" "$EXT"; }

trace_stop() {
  pkill -f 'flushtrace.py stream' 2>/dev/null || true
  local i
  for i in $(seq 50); do
    [ -e "${MARK%/trace_marker}" ] || break
    rmdir "${MARK%/trace_marker}" 2>/dev/null || sleep 0.1
  done
  [ -e "${MARK%/trace_marker}" ] && echo "warning: tracefs instance $FLUSHTRACE_INSTANCE still present" || true
}

clean() {
  for u in $(units); do systemctl stop "$u" 2>/dev/null || true; done
  trace_stop
  clean_files
  echo cleaned
}

case "${1:-}" in
  preflight) preflight ;;
  health) health "${2:-}" ;;
  start) [ $# -ge 2 ] || die "usage: $0 start HOST:PORT [VARIANT...]"; shift; start "$@" ;;
  fua) [ $# -eq 2 ] || die "usage: $0 fua HOST:PORT"; fua "$2" ;;
  quiesce) quiesce ;;
  unquiesce) unquiesce ;;
  prepull) prepull ;;
  status) status ;;
  verify) [ $# -eq 2 ] || die "usage: $0 verify HOST:PORT"; verify "$2" ;;
  probe) probe ;;
  trace-stop) trace_stop ;;
  clean) clean ;;
  *) sed -n '2,37p' "$0"; exit 2 ;;
esac
