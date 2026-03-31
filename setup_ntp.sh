#!/bin/bash
#
# NTP Server Setup — configures chrony to serve NTP on the local network.
# Run once with: sudo bash setup_ntp.sh
#
# What it does:
# 1. Installs chrony (if not installed)
# 2. Adds 'allow' directive for the local subnet
# 3. Fixes the -F sandbox issue that prevents binding to port 123
# 4. Restarts chrony and verifies port 123 is open
#

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo: sudo bash setup_ntp.sh"
    exit 1
fi

echo "=== NTP Server Setup ==="
echo

# 1. Install chrony
if ! command -v chronyd &>/dev/null; then
    echo "Installing chrony..."
    apt install -y chrony
else
    echo "chrony is already installed."
fi

# 2. Detect local subnet and add allow directive
LOCAL_IP=$(ip -4 route get 192.168.0.1 2>/dev/null | grep -oP 'src \K\S+' || echo "")
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(hostname -I | awk '{print $1}')
fi
SUBNET=$(echo "$LOCAL_IP" | sed 's/\.[0-9]*$/.0\/24/')

if grep -q "^allow" /etc/chrony/chrony.conf; then
    echo "allow directive already exists in chrony.conf"
else
    echo "Adding: allow $SUBNET"
    echo "allow $SUBNET" >> /etc/chrony/chrony.conf
fi

# 3. Fix sandbox issue — remove -F flag that prevents port 123 binding
echo "Creating systemd override to disable sandbox..."
mkdir -p /etc/systemd/system/chrony.service.d
cat > /etc/systemd/system/chrony.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/sbin/chronyd
EOF

# 4. Restart
echo "Reloading systemd and restarting chrony..."
systemctl daemon-reload
systemctl stop systemd-timesyncd 2>/dev/null || true
systemctl disable systemd-timesyncd 2>/dev/null || true
systemctl enable --now chrony
systemctl restart chrony

# 5. Verify
sleep 1
echo
echo "=== Verification ==="
if ss -uln | grep -q ':123 '; then
    echo "OK: Port 123 is open"
else
    echo "WARNING: Port 123 is NOT open — check chrony logs: journalctl -u chrony"
fi

echo
echo "Sync status:"
chronyc tracking 2>/dev/null | grep -E "Reference|Stratum|System time" || echo "(not yet synced — wait a moment)"

echo
echo "=== Done ==="
echo "Your machine ($LOCAL_IP) is now serving NTP on port 123."
echo "Set the sensor's NTP Server IP to: $LOCAL_IP"
