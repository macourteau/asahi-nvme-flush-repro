#!/bin/bash
# Post-pull steps for pull N, run on the listener host from the working
# directory that holds listener.log, stream.pid and pullN.trace:
#   check the listener's DISCONNECT lines, stop the trace stream, analyze the
#   trace, wait for the node to come back, verify, clean, and take the
#   preflight for pull N+1.
#
# Usage: TEST_NODE=user@test-node LISTENER=192.0.2.10:5400 postpull.sh N
#
#   TEST_NODE   ssh destination of the test node (required)
#   LISTENER    host:port of dcheck listen, as the node reaches it (required)
#   NODE_TOOLS  directory holding the tools on the node (/root/nvme-flush-repro)
#   TOOLS       directory holding flushtrace.py here (this script's directory)
#   FAKE_US     in-driver/device flush split for analyze, in us (600)
set -euo pipefail
N=${1:?usage: TEST_NODE=user@host LISTENER=host:port $0 N}
: "${TEST_NODE:?set TEST_NODE to the ssh destination of the test node}"
: "${LISTENER:?set LISTENER to the listener host:port}"
NODE_TOOLS=${NODE_TOOLS:-/root/nvme-flush-repro}
TOOLS=${TOOLS:-$(dirname "$(readlink -f "$0")")}
FAKE_US=${FAKE_US:-600}
s() { ssh -o BatchMode=yes -o ConnectTimeout=5 "$TEST_NODE" "$@" </dev/null; }

echo "== listener"; grep -E 'DISCONNECT|GO' listener.log | tail -6 || true
if kill -0 "$(cat stream.pid)" 2>/dev/null; then echo "stream alive, killing"; kill "$(cat stream.pid)"; else echo "stream exited"; fi
echo "== analyze"
python3 -I "$TOOLS/flushtrace.py" analyze "pull$N.trace" --listener "$LISTENER" --fake-us "$FAKE_US" > "pull$N-analyze.txt" 2>&1
grep -E 'span=|device flush latency|predicted|^    [0-9a-f]{16}' "pull$N-analyze.txt" || true
echo "== wait for ssh"; for _ in $(seq 1 90); do s uptime 2>/dev/null && break; sleep 10; done; sleep 20
echo "== verify"; s "sudo $NODE_TOOLS/node.sh verify $LISTENER" > "pull$N-verify.txt" 2>&1 || true
grep -E 'dmesg counts|Filesystem state|VIOLATION|variant=|no loss|OK' "pull$N-verify.txt" || true
echo "== clean"; s "sudo $NODE_TOOLS/node.sh clean" | tail -1
s "sudo $NODE_TOOLS/node.sh preflight" > "pull$((N + 1))-preflight.txt" 2>&1 || true
grep -E '^fix' "pull$((N + 1))-preflight.txt" || true
