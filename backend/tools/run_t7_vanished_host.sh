#!/bin/bash
# Test 7 of the firmware 0x103D stream-control test plan: "vanished host".
# Blocks the sensor's raw TCP traffic to/from this PC for 40 s with iptables (so
# neither side sees a FIN or RST), removes the rule again, and checks that raw
# comes back on a NEW connection (the sensor's TCP keepalive dropped the old one).
#
# Needs root for iptables; stop the viewer backend first (the tool owns the ports):
#     sudo bash tools/run_t7_vanished_host.sh <sensor-ip> <sensor-mac> <app-password> [python]
# `python` defaults to $PYTHON or python3 (use the venv that has the backend's deps).
#
# Takes about 1.5 minutes. The firewall rules are always removed at the end,
# even if the test fails. Result lines start with PASS or FAIL.

set -e
if [ $# -lt 3 ]; then
    sed -n '2,13p' "$0"; exit 2
fi
IP=$1; MAC=$2; PW=$3; PY=${4:-${PYTHON:-python3}}; PORT=${TCP_PORT:-8066}
cd "$(dirname "$(readlink -f "$0")")/.."

# The sensor ports must be free: wait for other bench runs or the viewer backend.
while pgrep -f "tools/stream_control_check.py|uvicorn main:app" >/dev/null; do
    echo "the sensor ports are in use (another bench run or the viewer backend) — waiting..."
    sleep 15
done

cleanup() {
    iptables -D INPUT  -s "$IP" -p tcp --dport "$PORT" -j DROP 2>/dev/null || true
    iptables -D OUTPUT -d "$IP" -p tcp --sport "$PORT" -j DROP 2>/dev/null || true
}
trap cleanup EXIT

"$PY" tools/stream_control_check.py --force --port "$PORT" --tests t7 \
    --ip "$IP" --mac "$MAC" --password "$PW" 2>&1 | grep -v "DEBUG"
echo "done — firewall rules removed"
