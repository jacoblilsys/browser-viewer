"""
UDP command sender — reimplements Utils/Console/netapi/ for async use.

Protocol:
  Request  = 30-byte header (header_size, MAC, req_id, HMAC-MD5) + protobuf payload
  Response = 8-byte header  (header_size, req_id) + protobuf payload

HMAC: hmac.new(md5(password), payload, md5).digest()
"""

import asyncio
import hashlib
import hmac as _hmac
import socket
import struct
import time
from typing import Optional

# Import the patched sensor_cmd_pb2 (avoids descriptor-pool conflict with
# the stream config_pb2 that message_pb2 uses).
import os
import sys
_PROTO_DIR = os.path.join(os.path.dirname(__file__), 'protobuf')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

from protobuf import sensor_cmd_pb2 as _pb

def _safe_enum_name(enum_type, value, extensions=None):
    """Get enum name, falling back to extensions dict for new values not in compiled proto."""
    try:
        return enum_type.Name(value)
    except ValueError:
        if extensions and value in extensions:
            return extensions[value]
        return str(value)

_UDP_PORT           = 56671
_MULTICAST_ADDR     = '224.0.0.251'
_RESPONSE_TIMEOUT_S = 10.0
_NETWORK_IF         = os.environ.get('NETWORK_IF', '').strip()

# Request header: header_size(4) + mac(6) + req_id(4) + hmac(16) = 30 bytes
_REQ_HDR_FMT  = '<I6sI16s'
_REQ_HDR_SIZE = struct.calcsize(_REQ_HDR_FMT)   # 30

# Response header: header_size(4) + req_id(4) = 8 bytes
_RESP_HDR_FMT  = '<II'
_RESP_HDR_SIZE = struct.calcsize(_RESP_HDR_FMT)  # 8


# ── helpers ──────────────────────────────────────────────────────────────────

def _calc_hmac(payload: bytes, password: str) -> bytes:
    key = hashlib.md5(password.encode('utf-8')).digest()
    return _hmac.new(key, payload, hashlib.md5).digest()


def _mac_bytes(mac_str: str) -> bytes:
    """'aa:bb:cc:dd:ee:ff' → 6 bytes (big-endian)."""
    return bytes(int(h, 16) for h in mac_str.split(':'))


def _gen_req_id() -> int:
    return int(time.monotonic_ns() // 1000) & 0xFFFFFFFF or 1


def _build_request(payload: bytes, mac_str: str, password: str) -> tuple[bytes, int]:
    req_id = _gen_req_id()
    mac_b  = _mac_bytes(mac_str)
    hmac_b = _calc_hmac(payload, password)
    header = struct.pack(_REQ_HDR_FMT, _REQ_HDR_SIZE, mac_b, req_id, hmac_b)
    return header + payload, req_id


def _parse_response(data: bytes) -> Optional[bytes]:
    if len(data) < _RESP_HDR_SIZE:
        return None
    hdr_size, _ = struct.unpack_from(_RESP_HDR_FMT, data)
    if hdr_size != _RESP_HDR_SIZE:
        return None
    return data[_RESP_HDR_SIZE:]


class SensorApiError(Exception):
    pass


class SensorAuthError(SensorApiError):
    """The sensor rejected the request's HMAC — wrong or missing password."""
    pass


# ── blocking UDP transport (runs in thread pool) ─────────────────────────────

def _get_local_ip() -> str:
    """Return NETWORK_IF env var if set, otherwise auto-detect via routing table."""
    if _NETWORK_IF:
        return _NETWORK_IF
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '0.0.0.0'


def _send_recv_blocking(
    target_ip: str,
    payload: bytes,
    mac_str: str,
    password: str,
) -> bytes:
    """Blocking UDP send/receive — called via run_in_executor."""
    data, req_id = _build_request(payload, mac_str, password)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

    # Match Console transport: bind multicast to the correct interface and
    # join the multicast group so we receive responses sent to 224.0.0.251.
    local_ip = _get_local_ip()
    local_if = socket.inet_aton(local_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, local_if)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(_MULTICAST_ADDR) + local_if)

    sock.settimeout(_RESPONSE_TIMEOUT_S)
    sock.bind(('', 0))

    try:
        # Send to the sensor's last-known unicast IP AND to the multicast group.
        # The request header carries the target MAC, so the sensor matches on
        # that and replies via multicast (which we've joined) — this reaches it
        # even after a DHCP lease change makes the stored IP stale.
        sock.sendto(data, (target_ip, _UDP_PORT))
        try:
            sock.sendto(data, (_MULTICAST_ADDR, _UDP_PORT))
        except OSError:
            pass  # multicast send may fail on odd interfaces; unicast still tried

        deadline = time.monotonic() + _RESPONSE_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SensorApiError(
                    f'Timeout: no response from {target_ip}:{_UDP_PORT} '
                    f'(multicast via {local_ip} — check NETWORK_IF)'
                )
            sock.settimeout(remaining)
            try:
                resp_data = sock.recv(4096)
            except socket.timeout:
                raise SensorApiError(
                    f'Timeout: no response from {target_ip}:{_UDP_PORT} '
                    f'(multicast via {local_ip} — check NETWORK_IF)'
                )

            if len(resp_data) < _RESP_HDR_SIZE:
                continue
            hdr_size, resp_req_id = struct.unpack_from(_RESP_HDR_FMT, resp_data)
            if hdr_size == _RESP_HDR_SIZE and resp_req_id == req_id:
                return resp_data[_RESP_HDR_SIZE:]
    finally:
        sock.close()


async def _send_recv(target_ip: str, payload: bytes, mac_str: str, password: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _send_recv_blocking, target_ip, payload, mac_str, password
    )


def _check(resp_bytes: bytes) -> _pb.Response:
    resp = _pb.Response()
    resp.ParseFromString(resp_bytes)
    if resp.status != _pb.RESPONSE_STATUS_OK:
        name = _pb.ResponseStatus.Name(resp.status)
        if resp.status == _pb.RESPONSE_STATUS_INVALID_HMAC:
            # Wrong or missing password — surfaced distinctly so the UI can
            # tell the operator to check the password rather than assume the
            # sensor is broken.
            raise SensorAuthError(f'Sensor error: {name}')
        raise SensorApiError(f'Sensor error: {name}')
    return resp


# ── public API ───────────────────────────────────────────────────────────────

async def get_sensor_info(target_ip: str, mac: str, password: str) -> dict:
    req = _pb.Request(msg_version=1)
    req.get_sensor_info.SetInParent()
    resp = _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))
    info = resp.info
    return {
        'sensor_type':         info.sensor_type,
        'hardware_version':    info.hardware_version,
        'firmware_version':    info.firmware_version,
        'bootloader_version':  info.bootloader_version,
        'temp1':               info.temp1,
        'temp2':               info.temp2,
        'temp_core':           info.temp_core,
        'utc_time':            info.utc_time,
        'error_bits':          info.error_bits,
        'cpu_usage':           info.cpu_usage,
        'debug_str':           info.debug_str,
    }


async def get_sensor_config(target_ip: str, mac: str, password: str) -> dict:
    req = _pb.Request(msg_version=1)
    req.get_sensor_config.SetInParent()
    resp = _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))
    sc = resp.sensor
    return {
        'full_scale': _pb.AccelFullScale.Name(sc.full_scale),
        'axes':       _pb.AxisMask.Name(sc.axes),
        'odr_div':    _pb.OdrDiv.Name(sc.odr_div),
        'filter': {
            'filter_enabled': _pb.FilterEnabled.Name(sc.filter.filter_enabled),
            'filter_cutoff':  _pb.CutoffKHz.Name(sc.filter.filter_cutoff),
        },
        'fft_size':   _pb.FftSize.Name(sc.fft_size),
        'fft_precision': _safe_enum_name(_pb.FftPrecision, sc.fft_precision),
    }


async def set_sensor_config(
    target_ip: str,
    mac: str,
    password: str,
    full_scale: Optional[str] = None,
    axes: Optional[str] = None,
    odr_div: Optional[str] = None,
    filter_enabled: Optional[str] = None,
    filter_cutoff: Optional[str] = None,
    fft_size: Optional[str] = None,
    fft_precision: Optional[str] = None,
):
    # Fetch current config first so we only overwrite supplied fields.
    get_req = _pb.Request(msg_version=1)
    get_req.get_sensor_config.SetInParent()
    get_resp = _check(await _send_recv(target_ip, get_req.SerializeToString(), mac, password))
    sc = get_resp.sensor

    if full_scale is not None:
        sc.full_scale = _pb.AccelFullScale.Value(full_scale)
    if axes is not None:
        sc.axes = _pb.AxisMask.Value(axes)
    if odr_div is not None:
        sc.odr_div = _pb.OdrDiv.Value(odr_div)
    if filter_enabled is not None:
        sc.filter.filter_enabled = _pb.FilterEnabled.Value(filter_enabled)
    if filter_cutoff is not None:
        sc.filter.filter_cutoff = _pb.CutoffKHz.Value(filter_cutoff)
    if fft_size is not None:
        sc.fft_size = _pb.FftSize.Value(fft_size)
    if fft_precision is not None:
        # Takes effect immediately on the sensor (one FFT frame is dropped
        # during the engine switch); no reboot required.
        sc.fft_precision = _pb.FftPrecision.Value(fft_precision)

    set_req = _pb.Request(msg_version=1)
    set_req.set_sensor_config.CopyFrom(sc)
    _check(await _send_recv(target_ip, set_req.SerializeToString(), mac, password))


async def get_network_config(target_ip: str, mac: str, password: str) -> dict:
    req = _pb.Request(msg_version=1)
    req.get_network_config.SetInParent()
    resp = _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))
    nc = resp.ipv4_config

    def _ip(v: int) -> str:
        return socket.inet_ntoa(struct.pack('>I', v))

    return {
        'ip':           _ip(nc.ip),
        'netmask':      _ip(nc.netmask),
        'gateway':      _ip(nc.gateway),
        'server_ip':    _ip(nc.server_ip),
        'server_port':  nc.server_port,
        'ntp_server_ip': _ip(nc.ntp_server_ip),
        'ntp_interval_s': nc.ntp_interval_s,
        'ntp_offset_us':  nc.ntp_offset_us if nc.has_ntp_offset_us else None,
        'ntp_min_ms_error_to_update': nc.ntp_min_ms_error_to_update if nc.has_ntp_min_ms_error_to_update else None,
        'dhcp':         _pb.FeatureToggle.Name(nc.dhcp),
        'data_stream':  _pb.FeatureToggle.Name(nc.data_stream),
        'fft_stream':   _pb.FeatureToggle.Name(nc.fft_stream),
        # Transport per stream: FEATURE_DISABLED = TCP (legacy), FEATURE_ENABLED = UDP.
        # Applied on the sensor's next reboot.
        'sample_transport': _safe_enum_name(_pb.FeatureToggle, nc.sample_transport),
        'fft_transport':    _safe_enum_name(_pb.FeatureToggle, nc.fft_transport),
    }


async def set_network_config(target_ip: str, mac: str, password: str, **kwargs):
    # Build a fresh config with only the requested fields set.
    # The sensor firmware applies non-default fields only.
    nc = _pb.NetworkConfigV4()

    def _pack_ip(s: str) -> int:
        return struct.unpack('>I', socket.inet_aton(s))[0]

    for k, v in kwargs.items():
        if k == 'ip':
            nc.ip = _pack_ip(v)
        elif k == 'netmask':
            nc.netmask = _pack_ip(v)
        elif k == 'gateway':
            nc.gateway = _pack_ip(v)
        elif k == 'server_ip':
            nc.server_ip = _pack_ip(v)
        elif k == 'server_port':
            nc.server_port = int(v)
        elif k == 'ntp_server_ip':
            nc.ntp_server_ip = _pack_ip(v)
        elif k == 'ntp_interval_s':
            nc.ntp_interval_s = int(v)
        elif k == 'ntp_offset_us':
            nc.ntp_offset_us = int(v)
            nc.has_ntp_offset_us = True
        elif k == 'ntp_min_ms_error_to_update':
            nc.ntp_min_ms_error_to_update = int(v)
            nc.has_ntp_min_ms_error_to_update = True
        elif k == 'dhcp':
            nc.dhcp = _pb.FeatureToggle.Value(v)
        elif k == 'data_stream':
            nc.data_stream = _pb.FeatureToggle.Value(v)
        elif k == 'fft_stream':
            nc.fft_stream = _pb.FeatureToggle.Value(v)
        elif k == 'sample_transport':
            nc.sample_transport = _pb.FeatureToggle.Value(v)
        elif k == 'fft_transport':
            nc.fft_transport = _pb.FeatureToggle.Value(v)

    set_req = _pb.Request(msg_version=1)
    set_req.set_network_config.CopyFrom(nc)
    _check(await _send_recv(target_ip, set_req.SerializeToString(), mac, password))


async def stream_start(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.stream_data = _pb.FEATURE_ENABLED
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))


async def stream_stop(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.stream_data = _pb.FEATURE_DISABLED
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))


async def stream_fft_start(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.stream_fft = _pb.FEATURE_ENABLED
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))


async def stream_fft_stop(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.stream_fft = _pb.FEATURE_DISABLED
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))


async def reset(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.reset_device = True
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))


async def boot_now(target_ip: str, mac: str, password: str):
    req = _pb.Request(msg_version=1)
    req.command.boot_now = True
    _check(await _send_recv(target_ip, req.SerializeToString(), mac, password))
