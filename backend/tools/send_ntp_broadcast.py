#!/usr/bin/env python3
"""Force a single SNTP broadcast onto the sensor's L2 segment.

Test helper for NTP **Listen** mode (firmware >= 0x1031). In Listen mode the
sensor never sends outbound NTP; it only listens for broadcast/multicast SNTP
on UDP/123. A broadcast daemon (chronyd/ntpd) only emits on its own interval,
so this sends one valid SNTP broadcast frame on demand — a much tighter test
loop and it isolates "does the sensor accept a broadcast" from "is my daemon
actually broadcasting".

The frame is a 48-byte NTP packet, mode 5 (broadcast), with the transmit
timestamp set to now. The sensor reads that transmit timestamp and disciplines
its clock. Since the 0x1031 firmware gates the originate-timestamp check to
POLL mode only, a crafted broadcast is accepted in Listen mode as long as
LI/version/mode/stratum are valid and the transmit timestamp is non-zero.

Run this FROM A HOST ON THE SAME SWITCH/VLAN AS THE SENSOR (broadcasts do not
route). Then watch the viewer's time-sync indicator flip amber -> green.

The frame is sent FROM UDP port 123 by default. Real NTP broadcast servers
always use source port 123, and lwIP's SNTP receive path (SNTP_CHECK_RESPONSE
>= 1, which this firmware uses) checks the source port against the NTP port --
a frame from an ephemeral source port is silently dropped. Binding port 123
needs elevated privileges (Linux: run with sudo; Windows: run as Administrator)
AND no other NTP service (chronyd, systemd-timesyncd, W32Time) holding the port
-- stop it first, or pass a different src_port to bypass the bind.

Usage:
    python3 send_ntp_broadcast.py [broadcast_addr] [src_ip_to_bind] [src_port]

Examples:
    sudo python3 send_ntp_broadcast.py 192.168.0.255                    # /24 LAN
    sudo python3 send_ntp_broadcast.py 169.254.255.255                  # link-local
    sudo python3 send_ntp_broadcast.py 169.254.255.255 169.254.41.42    # pin source NIC
    python3 send_ntp_broadcast.py 192.168.0.255 "" 0                    # ephemeral port (no sudo; may be dropped)

Confirm it's on the wire (source port should read 123):
    sudo tcpdump -ni <iface> udp port 123
"""
import socket
import struct
import sys
import time

NTP_EPOCH = 2208988800  # seconds between 1900-01-01 and 1970-01-01


def ntp_ts(unix):
    """Convert a Unix time (float) to a 64-bit NTP timestamp (secs, frac)."""
    secs = int(unix) + NTP_EPOCH
    frac = int((unix - int(unix)) * (1 << 32))
    return secs, frac


def build_packet(now):
    ref_s, ref_f = ntp_ts(now - 1)   # reference = ~1 s ago
    tx_s,  tx_f  = ntp_ts(now)       # transmit  = now (this is what the sensor uses)
    li_vn_mode = (0 << 6) | (4 << 3) | 5   # LI=0, VN=4, Mode=5 (broadcast)
    return struct.pack(
        "!B B b b I I 4s II II II II",
        li_vn_mode, 1, 6, -20,       # stratum=1, poll=6, precision=-20
        0, 0, b"LOCL",               # root delay, root dispersion, reference id
        ref_s, ref_f,                # reference timestamp
        0, 0,                        # originate timestamp (ignored in Listen mode)
        0, 0,                        # receive timestamp
        tx_s, tx_f,                  # transmit timestamp
    )


def main(argv):
    bcast    = argv[1] if len(argv) > 1 else "169.254.255.255"
    src_ip   = argv[2] if len(argv) > 2 and argv[2] else None
    src_port = int(argv[3]) if len(argv) > 3 else 123
    port     = 123

    pkt = build_packet(time.time())
    assert len(pkt) == 48, f"NTP packet must be 48 bytes, got {len(pkt)}"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Bind the source address (pin egress NIC on link-local / multi-homed hosts)
    # and, by default, the source port to 123 so the frame looks like a real NTP
    # server -- lwIP drops broadcasts whose source port isn't 123.
    try:
        s.bind((src_ip or "", src_port))
    except OSError as e:
        s.close()
        print(f"error: could not bind source {src_ip or '*'}:{src_port} -- {e}\n"
              f"  Port 123 needs root/Administrator and a free port. Either:\n"
              f"    - run this with sudo (Linux) / as Administrator (Windows), and\n"
              f"    - stop any local NTP service (chronyd, systemd-timesyncd, W32Time), OR\n"
              f"    - pass a non-123 source port to bypass (may be dropped by the sensor):\n"
              f"        python3 {argv[0]} {bcast} \"{src_ip or ''}\" 0",
              file=sys.stderr)
        return 1

    try:
        s.sendto(pkt, (bcast, port))
    finally:
        s.close()
    print(f"sent {len(pkt)}-byte SNTP broadcast (mode 5) to {bcast}:{port} "
          f"from {src_ip or '*'}:{src_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
