#!/bin/bash
# E0 (no power cut): is the flush-swallowing cluster present, and what does a
# real device flush cost? Run as root on the test node, from any directory:
#   sudo bash e0.sh > E0.txt
# E0_DIR is a scratch directory on the filesystem to test (default
# /var/tmp/fsb, btrfs on Fedora Asahi); it is created and removed again.
# The traces are kept in /var/tmp/E0a.trace and /var/tmp/E0b.trace.
set -e
cd "$(dirname "$(readlink -f "$0")")"
D=${E0_DIR:-/var/tmp/fsb}
mkdir -p "$D"
date -u +%Y-%m-%dT%H:%M:%SZ
echo "### E0a: 300 back-to-back fdatasyncs under an 8 s trace"
python3 -I flushtrace.py snap 8 --keep /var/tmp/E0a.trace & T=$!; sleep 0.5
python3 -I fsync_bench.py "$D" --n 300; wait $T
echo "### E0b: 10 fdatasyncs 2.5 s apart under a 30 s trace (each sync > 1 s after the previous device flush)"
python3 -I flushtrace.py snap 30 --keep /var/tmp/E0b.trace & T=$!; sleep 0.5
python3 -I fsync_bench.py "$D" --n 10 --gap 2.5; wait $T
rmdir "$D"
