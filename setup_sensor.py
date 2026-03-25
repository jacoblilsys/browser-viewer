#!/usr/bin/env python3
"""
Rescue tool for configuring sensors on unreachable subnets (e.g. link-local 169.254.x.x).

Temporarily adds a link-local IP to your network interface, sends the configuration
command to the sensor, then removes the temporary IP. The main viewer app does not
need to be stopped.

Requires the same Python virtual environment as the backend (for protobuf).

Usage (Linux, requires sudo — use the venv python directly):
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py <sensor_ip> <sensor_mac> [options]

Examples:
    # Enable DHCP on a link-local sensor:
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --dhcp

    # Set a static IP:
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --ip 192.168.0.50

    # Set server address + enable DHCP (server auto-detected):
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --dhcp --server-ip 192.168.0.124

    # Specify network interface explicitly:
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --dhcp --iface eth0

macOS (requires sudo, uses ifconfig):
    sudo ~/venv-browser-viewer/bin/python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --dhcp

Windows (run as Administrator):
    python setup_sensor.py 169.254.2.58 aa:bb:cc:dd:ee:ff --dhcp
"""

import argparse
import platform
import re
import socket
import subprocess
import sys
import time

# Add backend to path for sensor_api imports
import os
_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend')
sys.path.insert(0, _BACKEND)
_PROTO_DIR = os.path.join(_BACKEND, 'protobuf')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

from protobuf import sensor_cmd_pb2 as _pb

# ── constants ────────────────────────────────────────────────────────────────

_UDP_PORT = 56671
_TIMEOUT  = 10.0
_LINK_LOCAL_TEMP_IP = '169.254.99.1'
_PLATFORM = platform.system()  # 'Linux', 'Windows', 'Darwin'


# ── network helpers ──────────────────────────────────────────────────────────

def _detect_interface():
    """Auto-detect the primary network interface name."""
    if _PLATFORM == 'Windows':
        out = subprocess.check_output(
            ['netsh', 'interface', 'ipv4', 'show', 'interfaces'],
            text=True, timeout=5
        )
        for line in out.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 4 and parts[-2] == 'connected':
                return ' '.join(parts[3:])
        return 'Ethernet'
    elif _PLATFORM == 'Darwin':
        # macOS: parse default route for interface name
        out = subprocess.check_output(['route', '-n', 'get', 'default'], text=True, timeout=5)
        m = re.search(r'interface:\s*(\S+)', out)
        return m.group(1) if m else 'en0'
    else:
        # Linux
        out = subprocess.check_output(['ip', 'route', 'show', 'default'], text=True, timeout=5)
        m = re.search(r'dev\s+(\S+)', out)
        return m.group(1) if m else 'eth0'


def _add_link_local_ip(iface, ip='169.254.99.1'):
    """Add a temporary link-local IP address to the interface."""
    print(f'  Adding {ip}/16 to {iface}...')
    if _PLATFORM == 'Windows':
        subprocess.check_call(
            ['netsh', 'interface', 'ip', 'add', 'address', iface, ip, '255.255.0.0'],
            timeout=10
        )
    elif _PLATFORM == 'Darwin':
        subprocess.check_call(
            ['ifconfig', iface, 'alias', ip, '255.255.0.0'],
            timeout=5
        )
    else:
        subprocess.check_call(['ip', 'addr', 'add', f'{ip}/16', 'dev', iface], timeout=5)
    time.sleep(0.5)  # let the OS register the route


def _remove_link_local_ip(iface, ip='169.254.99.1'):
    """Remove the temporary link-local IP address."""
    print(f'  Removing {ip}/16 from {iface}...')
    try:
        if _PLATFORM == 'Windows':
            subprocess.check_call(
                ['netsh', 'interface', 'ip', 'delete', 'address', iface, ip],
                timeout=10
            )
        elif _PLATFORM == 'Darwin':
            subprocess.check_call(
                ['ifconfig', iface, '-alias', ip],
                timeout=5
            )
        else:
            subprocess.check_call(['ip', 'addr', 'del', f'{ip}/16', 'dev', iface], timeout=5)
    except Exception as e:
        print(f'  Warning: could not remove temp IP: {e}')


# ── sensor command (simplified from sensor_api.py) ───────────────────────────

import hashlib
import hmac as _hmac
import struct
import random

_REQ_HDR_FMT  = '<I6sI16s'
_REQ_HDR_SIZE = struct.calcsize(_REQ_HDR_FMT)
_RESP_HDR_FMT = '<II'
_RESP_HDR_SIZE = struct.calcsize(_RESP_HDR_FMT)


def _mac_bytes(mac_str):
    return bytes(int(b, 16) for b in mac_str.split(':'))


def _build_request(payload, mac_str, password):
    req_id = random.getrandbits(32)
    mac = _mac_bytes(mac_str)
    pwd_hash = hashlib.md5(password.encode()).digest() if password else b'\x00' * 16
    sig = _hmac.new(pwd_hash, payload, hashlib.md5).digest()
    header = struct.pack(_REQ_HDR_FMT, _REQ_HDR_SIZE, mac, req_id, sig)
    return header + payload, req_id


def _pack_ip(ip_str):
    """Convert dotted IP string to big-endian uint32."""
    return struct.unpack('>I', socket.inet_aton(ip_str))[0]


def _send_command(target_ip, mac_str, password, local_ip, **config_kwargs):
    """Build and send a network config set command."""
    nc = _pb.NetworkConfigV4()

    for key, val in config_kwargs.items():
        if val is None:
            continue
        if key == 'dhcp':
            nc.dhcp = _pb.FeatureToggle.Value(val)
        elif key == 'data_stream':
            nc.data_stream = _pb.FeatureToggle.Value(val)
        elif key == 'fft_stream':
            nc.fft_stream = _pb.FeatureToggle.Value(val)
        elif key == 'ip':
            nc.ip = _pack_ip(val)
        elif key == 'netmask':
            nc.netmask = _pack_ip(val)
        elif key == 'gateway':
            nc.gateway = _pack_ip(val)
        elif key == 'server_ip':
            nc.server_ip = _pack_ip(val)
        elif key == 'server_port':
            nc.server_port = int(val)
        elif key == 'ntp_server_ip':
            nc.ntp_server_ip = _pack_ip(val)

    req = _pb.Request(msg_version=1)
    req.set_network_config.CopyFrom(nc)
    payload = req.SerializeToString()
    data, req_id = _build_request(payload, mac_str, password)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

    local_if = socket.inet_aton(local_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, local_if)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton('224.0.0.251') + local_if)
    sock.settimeout(_TIMEOUT)
    sock.bind((local_ip, 0))

    try:
        bound_addr = sock.getsockname()
        print(f'  Socket bound to {bound_addr[0]}:{bound_addr[1]}')
        sock.sendto(data, (target_ip, _UDP_PORT))
        print(f'  Command sent to {target_ip}:{_UDP_PORT} via {local_ip} ({len(data)} bytes)')

        deadline = time.monotonic() + _TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print('  Warning: no response (timeout). Command may still have been received.')
                return False
            sock.settimeout(remaining)
            try:
                resp = sock.recv(4096)
            except socket.timeout:
                print('  Warning: no response (timeout). Command may still have been received.')
                return False
            if len(resp) >= _RESP_HDR_SIZE:
                _, resp_id = struct.unpack_from(_RESP_HDR_FMT, resp)
                if resp_id == req_id:
                    print('  Response received — OK')
                    return True
    finally:
        sock.close()


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Configure a sensor on an unreachable subnet (e.g. link-local).',
        epilog='Requires root/admin privileges to add temporary network addresses.'
    )
    parser.add_argument('sensor_ip', help='Current IP of the sensor (e.g. 169.254.2.58)')
    parser.add_argument('sensor_mac', help='MAC address of the sensor (e.g. aa:bb:cc:dd:ee:ff)')
    parser.add_argument('--password', '-p', default='', help='Sensor password (default: empty)')
    parser.add_argument('--iface', '-i', help='Network interface name (auto-detected if omitted)')
    parser.add_argument('--dhcp', action='store_true', help='Enable DHCP on the sensor')
    parser.add_argument('--ip', help='Set static IP on the sensor (e.g. 192.168.0.50)')
    parser.add_argument('--netmask', default='255.255.255.0', help='Netmask for static IP (default: 255.255.255.0)')
    parser.add_argument('--gateway', help='Gateway for static IP')
    parser.add_argument('--server-ip', help='Server IP to configure (auto-detected if omitted)')
    parser.add_argument('--server-port', type=int, default=8066, help='Server TCP port (default: 8066)')
    parser.add_argument('--ntp-ip', help='NTP server IP (defaults to server-ip)')
    parser.add_argument('--enable-stream', action='store_true', default=True,
                        help='Enable data streaming (default: yes)')

    args = parser.parse_args()

    if not args.dhcp and not args.ip:
        parser.error('Specify --dhcp or --ip <address>')

    is_link_local = args.sensor_ip.startswith('169.254.')
    iface = args.iface or _detect_interface()

    # Auto-detect server IP if not provided
    if not args.server_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('192.168.0.1', 1))
            args.server_ip = s.getsockname()[0]
            s.close()
        except Exception:
            args.server_ip = '0.0.0.0'

    ntp_ip = args.ntp_ip or args.server_ip

    print(f'\nSensor Setup Tool')
    print(f'  Sensor:    {args.sensor_mac} @ {args.sensor_ip}')
    print(f'  Interface: {iface}')
    print(f'  Server:    {args.server_ip}:{args.server_port}')
    print()

    # Build config kwargs
    config = {
        'server_ip':     args.server_ip,
        'server_port':   args.server_port,
        'ntp_server_ip': ntp_ip,
        'data_stream':   'FEATURE_ENABLED' if args.enable_stream else None,
    }

    if args.dhcp:
        config['dhcp'] = 'FEATURE_ENABLED'
        print('  Mode: DHCP enabled')
    elif args.ip:
        config['dhcp'] = 'FEATURE_DISABLED'
        config['ip'] = args.ip
        config['netmask'] = args.netmask
        if args.gateway:
            config['gateway'] = args.gateway
        print(f'  Mode: Static IP {args.ip}/{args.netmask}')

    print()

    temp_ip_added = False
    local_ip = _LINK_LOCAL_TEMP_IP

    try:
        if is_link_local:
            print('Step 1: Adding temporary link-local address...')
            _add_link_local_ip(iface, _LINK_LOCAL_TEMP_IP)
            temp_ip_added = True
            local_ip = _LINK_LOCAL_TEMP_IP
        else:
            local_ip = args.server_ip

        print('Step 2: Sending configuration command...')
        ok = _send_command(
            args.sensor_ip, args.sensor_mac, args.password,
            local_ip, **config
        )

        if ok:
            print('\nDone! The sensor should reboot with its new network settings.')
        else:
            print('\nCommand sent but no confirmation received. The sensor may still apply the settings after reboot.')

    finally:
        if temp_ip_added:
            print('\nStep 3: Cleaning up temporary address...')
            _remove_link_local_ip(iface, _LINK_LOCAL_TEMP_IP)

    print()


if __name__ == '__main__':
    main()
