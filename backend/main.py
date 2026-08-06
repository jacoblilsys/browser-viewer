"""
FastAPI application — wires together all backend components.

Start with:
    cd Utils/browser_viewer/backend
    uvicorn main:app --reload --port 8000
"""

import asyncio
import os
import sys
import time

# Bootstrap protobuf path before any local imports that need it.
_PROTO_DIR = os.path.join(os.path.dirname(__file__), 'protobuf')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

import logging
_log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=getattr(logging, _log_level, logging.INFO),
                    format='%(levelname)s %(name)s: %(message)s')

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from network_receiver import NetworkReceiver
from udp_receiver import UDPReceiver
import protobuf_decoder
from protobuf_decoder import decode_any, FrameData, FFTFrameData
from broadcaster import Broadcaster
from log_writer import LogManager
from mdns_scanner import MDNSScanner
import fw_update

# ── configuration ─────────────────────────────────────────────────────────────

_TCP_HOST   = '0.0.0.0'
_TCP_PORT   = int(os.environ.get('TCP_PORT', '8066'))
# UDP data streaming (opt-in per-stream on the sensor). The device sends UDP to
# the same server_port it uses for TCP, so default UDP_PORT to TCP_PORT.
# TCP and UDP on the same port number do not conflict.
_UDP_PORT   = int(os.environ.get('UDP_PORT', str(_TCP_PORT)))
_LOG_DIR    = os.environ.get('LOG_DIR', './logs')
_WS_FPS     = int(os.environ.get('WS_FPS', '60'))
_NETWORK_IF = os.environ.get('NETWORK_IF', '').strip()
_FRONTEND   = Path(__file__).parent.parent / 'frontend'


def _is_ipv4(s: str) -> bool:
    parts = (s or '').split('.')
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _cli_bind_host() -> str:
    """The uvicorn --host bind IP from argv, if it's a specific (non-wildcard,
    non-loopback) IPv4 — e.g. `uvicorn main:app --host 169.254.41.42`."""
    argv = sys.argv
    host = ''
    for i, a in enumerate(argv):
        if a == '--host' and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith('--host='):
            host = a.split('=', 1)[1]
    if _is_ipv4(host) and host != '0.0.0.0' and not host.startswith('127.'):
        return host
    return ''


def _request_host(request) -> str:
    """The address the browser used to reach us (from the request), when it's a
    usable non-loopback IPv4 — the most reliable server IP on multi-homed hosts."""
    h = request.url.hostname if request else ''
    return h if (_is_ipv4(h) and not h.startswith('127.')) else ''


def _get_local_ip() -> str:
    """Best guess at this server's IP, in priority order: NETWORK_IF override,
    the uvicorn --host bind IP, then routing-table auto-detect."""
    if _NETWORK_IF:
        return _NETWORK_IF
    bind = _cli_bind_host()
    if bind:
        return bind
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('192.168.0.1', 1))   # doesn't actually send anything
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _get_interface_name(ip: str) -> str:
    """Return the network adapter name for the given IP address."""
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for addr in adapter.ips:
                if addr.is_IPv4 and addr.ip == ip:
                    return adapter.nice_name
    except Exception:
        pass
    return ''

# ── global state ─────────────────────────────────────────────────────────────

_queue:    asyncio.Queue = asyncio.Queue(maxsize=4096)
_receiver: Optional[NetworkReceiver] = None
_udp_receiver: Optional[UDPReceiver] = None
_broadcaster  = Broadcaster(fps=_WS_FPS)
_log_writer   = LogManager(output_dir=_LOG_DIR)
_mdns         = MDNSScanner(interface_ip=_NETWORK_IF)
_logging_on   = False

# Last sensor config seen per device, keyed by MAC as it appears in
# FrameData.device_id ('aa:bb:cc:dd:ee:ff'). Populated whenever the UI reads or
# writes a config. Used to stamp capture files: none of the sensor settings —
# dc_removal included — are signalled in the packet header, so a saved capture
# would otherwise not record whether the signal was high-passed, at what corner,
# or which hardware filter was active.
_sensor_cfg_seen: Dict[str, dict] = {}


def _note_sensor_cfg(mac: str, cfg: dict):
    if not isinstance(cfg, dict) or cfg.get('detail'):
        return
    f = cfg.get('filter') or {}
    _sensor_cfg_seen[mac.lower()] = {
        'full_scale':     cfg.get('full_scale'),
        'axes':           cfg.get('axes'),
        'odr_div':        cfg.get('odr_div'),
        'filter_enabled': f.get('filter_enabled'),
        'filter_cutoff':  f.get('filter_cutoff'),
        'fft_size':       cfg.get('fft_size'),
        'fft_precision':  cfg.get('fft_precision'),
        'dc_removal':     cfg.get('dc_removal'),
    }
    _log_writer.set_sensor_config(_sensor_cfg_seen)

# ── counters ──────────────────────────────────────────────────────────────────
_stats = {
    'frames': 0,
    'samples': 0,
    'fft_frames': 0,
    'packets': 0,
    'errors': 0,
}


# Last observed transport ('tcp'/'udp') and monotonic timestamp per stream.
# A stream is "active" only if a frame arrived within _TRANSPORT_STALE_S.
_TRANSPORT_STALE_S = 3.0
_transport_seen = {'raw': [None, 0.0], 'fft': [None, 0.0]}

# Per-stream frame accounting for the traffic monitor. `received` = frames
# decoded OK; `missing` = frames lost, inferred from Header.sequence_number
# gaps (works for both TCP and UDP); `resets` = stream restarts / seq resets.
_stream_stats = {
    'raw': {'received': 0, 'missing': 0, 'resets': 0, 'last_uid': None, 'last_seq': None},
    'fft': {'received': 0, 'missing': 0, 'resets': 0, 'last_uid': None, 'last_seq': None},
}
# A forward seq jump larger than this is treated as a stream restart, not loss
# (guards against uint32 wrap and FLAG_STREAM_END seq→0 resets).
_MAX_SEQ_GAP = 100_000


def _account_seq(kind: str, stream_uid: int, seq: int):
    """Update received/missing/resets for a stream from its sequence number."""
    st = _stream_stats[kind]
    st['received'] += 1
    last_uid = st['last_uid']
    last_seq = st['last_seq']
    st['last_uid'] = stream_uid
    st['last_seq'] = seq
    if last_seq is None:
        return  # first frame ever — nothing to compare
    if stream_uid != last_uid:
        st['resets'] += 1   # a different stream_uid means the stream restarted
        return
    delta = (seq - last_seq) & 0xFFFFFFFF   # uint32 wrap-safe forward distance
    if delta == 0:
        return  # duplicate / retransmit — ignore
    if 1 <= delta <= _MAX_SEQ_GAP:
        st['missing'] += delta - 1
    else:
        # backward or implausibly large jump → restart, don't inflate missing
        st['resets'] += 1


def _reset_stream_stats():
    for st in _stream_stats.values():
        st.update({'received': 0, 'missing': 0, 'resets': 0, 'last_uid': None, 'last_seq': None})

# Status is connection/logging/stats metadata — it must NOT be broadcast on
# every decoded frame (that floods the browser at the sensor's frame rate and
# backs up the client event loop until the tab has to be refreshed). Cap it to
# a few Hz here; the 1 Hz heartbeat covers idle periods.
_STATUS_MIN_INTERVAL = 0.25  # seconds → ≤ 4 status broadcasts/sec
_last_status_bcast = 0.0

# Sensor clock state, from Header.flags FLAG_NO_TIME_SYNC on each data frame.
# None = unknown (no frame seen yet); True = disciplined; False = not synced.
_time_synced: 'bool | None' = None


def _all_stats() -> dict:
    """Base counters, UDP diagnostics, and current per-stream transport."""
    s = dict(_stats)
    if _udp_receiver is not None:
        s.update(_udp_receiver.stats())
    now = time.monotonic()
    transports = {}
    for stream in ('raw', 'fft'):
        tp, ts = _transport_seen[stream]
        transports[stream] = tp if (tp and now - ts < _TRANSPORT_STALE_S) else None
        s[f'{stream}_transport'] = transports[stream]   # flat keys (existing toolbar)
    # Nested per-stream detail for the traffic monitor panel.
    s['streams'] = {
        'raw': {'received': _stream_stats['raw']['received'],
                'missing':  _stream_stats['raw']['missing'],
                'resets':   _stream_stats['raw']['resets'],
                'samples':  _stats['samples'],
                'transport': transports['raw']},
        'fft': {'received': _stream_stats['fft']['received'],
                'missing':  _stream_stats['fft']['missing'],
                'resets':   _stream_stats['fft']['resets'],
                'transport': transports['fft']},
    }
    return s


def _data_active() -> bool:
    """True if any stream received data recently (TCP or UDP)."""
    now = time.monotonic()
    return any(ts and now - ts < _TRANSPORT_STALE_S for _, ts in _transport_seen.values())


def _is_connected() -> bool:
    """Sensor considered connected if the TCP socket is up OR data is arriving
    on any transport (UDP is connectionless, so recent data == 'connected')."""
    tcp = _receiver.connected_clients > 0 if _receiver else False
    return tcp or _data_active()


# ── lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _receiver, _udp_receiver

    _ip = _get_local_ip()
    _if = _get_interface_name(_ip)
    _if_label = f' ({_if})' if _if else ''
    _log_host = logging.getLogger('host')
    _log_host.info('Network interface: %s%s', _ip, _if_label)
    _log_host.info('Sensor TCP port: %d', _TCP_PORT)
    _log_host.info('Sensor UDP port: %d', _UDP_PORT)
    if not _NETWORK_IF:
        _log_host.info(
            'Tip: set NETWORK_IF=<ip> env var to force a specific interface'
        )

    loop = asyncio.get_event_loop()
    _receiver = NetworkReceiver(_TCP_HOST, _TCP_PORT, _queue, loop)
    _receiver.start()
    # UDP receiver runs alongside TCP: harmless when the sensor uses TCP, and
    # ready to reassemble chunked frames when either stream is switched to UDP.
    _udp_receiver = UDPReceiver(_TCP_HOST, _UDP_PORT, _queue, loop)
    _udp_receiver.start()
    _broadcaster.start()
    _mdns.start()

    asyncio.create_task(_drain_queue())
    asyncio.create_task(_status_heartbeat())

    # Print the browser URL prominently
    _http_port = int(os.environ.get('PORT', os.environ.get('UVICORN_PORT', '8000')))
    _url = f'http://{_ip}:{_http_port}'
    _line = f'  Open in browser:  {_url}'
    _w = len(_line) + 2
    print()
    print(f'  ┌{"─" * _w}┐')
    print(f'  │{_line}  │')
    print(f'  └{"─" * _w}┘')
    print()

    yield

    _receiver.stop()
    if _udp_receiver is not None:
        _udp_receiver.stop()
    _mdns.stop()
    _log_writer.close_all()


app = FastAPI(lifespan=lifespan)

# Serve frontend static files
if _FRONTEND.exists():
    app.mount('/static', StaticFiles(directory=str(_FRONTEND)), name='static')


# ── queue drain ───────────────────────────────────────────────────────────────

async def _maybe_broadcast_status(device_id: str = ''):
    """Throttled status broadcast (shared across the raw & fft drain branches).
    Capped at _STATUS_MIN_INTERVAL so status doesn't flood the browser at the
    full decode rate; the 1 Hz heartbeat covers idle periods."""
    global _last_status_bcast
    now = time.monotonic()
    if now - _last_status_bcast < _STATUS_MIN_INTERVAL:
        return
    _last_status_bcast = now
    sensor_ip = _receiver.remote_ip or '' if _receiver else ''
    await _broadcaster.broadcast_status(
        connected=_is_connected(),
        logging=_logging_on,
        device_id=device_id,
        sensor_ip=sensor_ip,
        log_format=_log_writer.fmt,
        stats=_all_stats(),
        time_synced=_time_synced,
    )


async def _drain_queue():
    global _time_synced
    import logging as _logging
    _log = _logging.getLogger('drain')
    _last_device_id = ''
    while True:
        payload_bytes, recv_time_ns, transport = await _queue.get()
        _stats['packets'] += 1
        try:
            result = decode_any(payload_bytes, recv_time_ns)
        except Exception as e:
            _stats['errors'] += 1
            _log.warning('decode failed: %s', e)
            continue

        if result is None:
            continue

        if isinstance(result, FrameData):
            _stats['frames'] += 1
            _stats['samples'] += sum(len(arr) for arr in result.columns.values())
            _account_seq('raw', result.stream_uid, result.seq)
            _last_device_id = result.device_id
            _time_synced = not result.no_time_sync
            _transport_seen['raw'] = [transport, time.monotonic()]

            if _logging_on:
                try:
                    _log_writer.write(result)
                except Exception as e:
                    _log.warning('log_writer failed: %s', e)

            await _broadcaster.push(result)
            await _maybe_broadcast_status(result.device_id)

        elif isinstance(result, FFTFrameData):
            _stats['fft_frames'] += 1
            _account_seq('fft', result.stream_uid, result.seq)
            _last_device_id = result.device_id
            _time_synced = not result.no_time_sync
            _transport_seen['fft'] = [transport, time.monotonic()]

            if _logging_on:
                try:
                    _log_writer.write_fft(result)
                except Exception as e:
                    _log.warning('log_writer fft failed: %s', e)

            await _broadcaster.push_fft(result)
            # Also refresh status from the FFT branch (shared throttle) so the
            # monitor panel stays live even when only FFT is streaming.
            await _maybe_broadcast_status(result.device_id)


async def _status_heartbeat():
    """Broadcast TCP connection state every second even without data frames."""
    import logging as _logging
    _log = _logging.getLogger('heartbeat')
    _last_device_id = ''
    while True:
        await asyncio.sleep(1.0)
        sensor_ip = _receiver.remote_ip or '' if _receiver else ''
        await _broadcaster.broadcast_status(
            connected=_is_connected(),
            logging=_logging_on,
            device_id=_last_device_id,
            sensor_ip=sensor_ip,
            log_format=_log_writer.fmt,
            stats=_all_stats(),
            time_synced=_time_synced,
        )


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get('/')
async def index():
    index_path = _FRONTEND / 'index.html'
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({'message': 'Browser Viewer backend running. Frontend not found.'})


@app.get('/api/devices')
async def api_devices():
    return _mdns.get_devices()


@app.get('/api/host')
async def api_host(request: Request):
    # Prefer the address the browser actually reached us on — correct on
    # link-local / multi-homed hosts where routing-table auto-detect guesses
    # wrong. NETWORK_IF (explicit) still wins if set.
    ip = _NETWORK_IF or _request_host(request) or _get_local_ip()
    return {'ip': ip, 'tcp_port': _TCP_PORT, 'udp_port': _UDP_PORT,
            'if_name': _get_interface_name(ip)}


@app.get('/api/status')
async def api_status():
    connected = _receiver.connected_clients > 0 if _receiver else False
    return {
        'connected': connected,
        'logging':   _logging_on,
        'streaming': not _broadcaster.paused,
        'time_sync': _time_synced,
        'tcp_port':  _TCP_PORT,
        'udp_port':  _UDP_PORT,
    }


class NtpCheckPayload(BaseModel):
    ip: str


def _sntp_check(ip: str, timeout: float = 3.0) -> dict:
    """Send a minimal SNTP request and parse the response."""
    import struct as _struct, socket as _sock, time as _time

    # NTP request: version 3, mode 3 (client), 48 bytes
    req = b'\x1b' + b'\x00' * 47

    sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t0 = _time.time()
        sock.sendto(req, (ip, 123))
        data, _ = sock.recvfrom(256)
        t1 = _time.time()
        rtt_ms = (t1 - t0) * 1000

        if len(data) < 48:
            return {'reachable': False, 'error': 'short response'}

        # Parse stratum (byte 1) and transmit timestamp (bytes 40-47)
        stratum = data[1]
        # NTP timestamp: seconds since 1900-01-01
        ntp_sec = _struct.unpack('!I', data[40:44])[0]
        ntp_frac = _struct.unpack('!I', data[44:48])[0]
        # Convert to Unix epoch (NTP epoch is 1900, Unix is 1970)
        ntp_unix = ntp_sec - 2208988800 + ntp_frac / (2**32)
        offset_ms = (ntp_unix - (t0 + t1) / 2) * 1000

        return {
            'reachable': True,
            'stratum': stratum,
            'offset_ms': round(offset_ms, 2),
            'rtt_ms': round(rtt_ms, 2),
        }
    except _sock.timeout:
        return {'reachable': False, 'error': 'timeout'}
    except Exception as e:
        return {'reachable': False, 'error': str(e)}
    finally:
        sock.close()


@app.post('/api/ntp/check')
async def api_ntp_check(body: NtpCheckPayload):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _sntp_check, body.ip)
    return result


@app.get('/api/udp/debug')
async def api_udp_debug():
    """Deep UDP reassembly diagnostics — which chunk_idx arrive vs go missing."""
    if _udp_receiver is None:
        return {'error': 'no udp receiver'}
    return _udp_receiver.debug_info()


@app.post('/api/stats/reset')
async def api_stats_reset():
    for k in _stats:
        _stats[k] = 0
    _reset_stream_stats()
    if _udp_receiver is not None:
        _udp_receiver.reset_stats()
    return {'ok': True}


@app.post('/api/logging/start')
async def logging_start():
    global _logging_on
    _logging_on = True
    return {'logging': True}


@app.post('/api/logging/stop')
async def logging_stop():
    global _logging_on
    _logging_on = False
    _log_writer.close_all()
    return {'logging': False}


class LogFormatPayload(BaseModel):
    format: str


@app.get('/api/logging/format')
async def logging_format_get():
    return {'format': _log_writer.fmt}


@app.post('/api/logging/format')
async def logging_format_set(body: LogFormatPayload):
    if _logging_on:
        raise HTTPException(status_code=409, detail='Cannot change format while logging')
    _log_writer.set_format(body.format)
    return {'format': _log_writer.fmt}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await _broadcaster.connect(ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive; ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        await _broadcaster.disconnect(ws)


# ── Phase 2: sensor API endpoints ────────────────────────────────────────────

class SensorTarget(BaseModel):
    target_ip: str
    mac:       str
    password:  str = ''


class SensorConfigPayload(SensorTarget):
    full_scale:     Optional[str] = None
    axes:           Optional[str] = None
    odr_div:        Optional[str] = None
    filter_enabled: Optional[str] = None
    filter_cutoff:  Optional[str] = None
    fft_size:       Optional[str] = None
    fft_precision:  Optional[str] = None
    dc_removal:     Optional[str] = None


class NetworkConfigPayload(SensorTarget):
    ip:            Optional[str] = None
    netmask:       Optional[str] = None
    gateway:       Optional[str] = None
    server_ip:     Optional[str] = None
    server_port:   Optional[int] = None
    ntp_server_ip: Optional[str] = None
    ntp_interval_s: Optional[int] = None
    ntp_offset_us:  Optional[int] = None
    ntp_min_ms_error_to_update: Optional[int] = None
    ntp_mode:      Optional[str] = None
    dhcp:          Optional[str] = None
    data_stream:   Optional[str] = None
    fft_stream:    Optional[str] = None
    sample_transport: Optional[str] = None
    fft_transport:    Optional[str] = None


def _sensor_api():
    import sensor_api
    return sensor_api


def _http_from(e: Exception) -> HTTPException:
    """Map a sensor-API error to an HTTP response. HMAC failures (wrong/missing
    password) get a distinct, machine-detectable detail ('invalid_hmac: ...')
    so the UI can tell the operator to check the password instead of showing a
    generic failure or assuming the sensor is broken."""
    import sensor_api
    if isinstance(e, sensor_api.SensorAuthError):
        return HTTPException(
            status_code=403,
            detail='invalid_hmac: Wrong or missing password (the sensor rejected the command).')
    msg = f'{type(e).__name__}: {e}' if str(e) else type(e).__name__
    return HTTPException(status_code=502, detail=msg)


@app.post('/api/sensor/info')
async def sensor_info(body: SensorTarget):
    try:
        info = await _sensor_api().get_sensor_info(body.target_ip, body.mac, body.password)
        # The firmware version is the only way to tell which FFT magnitude
        # normalisation a device uses — 0x1033 changed the numeric value of
        # every bin without changing the wire format. Hand it to the decoder so
        # older sensors get re-normalised onto the current scale, and report
        # back which scale is in force so the UI can say so.
        protobuf_decoder.note_firmware_version(body.mac, info.get('firmware_version', 0))
        info['fft_scaling'] = protobuf_decoder.fft_scaling_mode(body.mac)
        return info
    except Exception as e:
        raise _http_from(e)


@app.post('/api/sensor/config')
async def sensor_config_get(body: SensorTarget):
    try:
        cfg = await _sensor_api().get_sensor_config(body.target_ip, body.mac, body.password)
        _note_sensor_cfg(body.mac, cfg)
        return cfg
    except Exception as e:
        raise _http_from(e)


@app.post('/api/sensor/config/set')
async def sensor_config_set(body: SensorConfigPayload):
    try:
        await _sensor_api().set_sensor_config(
            body.target_ip, body.mac, body.password,
            full_scale=body.full_scale,
            axes=body.axes,
            odr_div=body.odr_div,
            filter_enabled=body.filter_enabled,
            filter_cutoff=body.filter_cutoff,
            fft_size=body.fft_size,
            fft_precision=body.fft_precision,
            dc_removal=body.dc_removal,
        )
        # Read the config back so capture metadata reflects what the sensor
        # actually accepted (it normalises and may bypass some settings) rather
        # than what was requested. Best-effort: the write already succeeded.
        try:
            _note_sensor_cfg(
                body.mac,
                await _sensor_api().get_sensor_config(body.target_ip, body.mac, body.password))
        except Exception as e:
            logging.getLogger('host').warning(
                'sensor config readback after set failed (capture metadata may '
                'be stale): %s', e)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


@app.post('/api/network/config')
async def network_config_get(body: SensorTarget):
    try:
        return await _sensor_api().get_network_config(body.target_ip, body.mac, body.password)
    except Exception as e:
        raise _http_from(e)


@app.post('/api/network/config/set')
async def network_config_set(body: NetworkConfigPayload):
    try:
        kwargs = {k: v for k, v in body.model_dump().items()
                  if k not in ('target_ip', 'mac', 'password') and v is not None}
        await _sensor_api().set_network_config(
            body.target_ip, body.mac, body.password, **kwargs)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


@app.post('/api/stream/start')
async def api_stream_start():
    _broadcaster.resume()
    return {'ok': True, 'streaming': True}


@app.post('/api/stream/stop')
async def api_stream_stop():
    _broadcaster.pause()
    return {'ok': True, 'streaming': False}


class BurstPayload(BaseModel):
    duration: float = 1.0


@app.post('/api/burst')
async def api_burst(body: BurstPayload):
    dur = max(0.1, min(body.duration, 30.0))
    _broadcaster.start_burst(dur)
    return {'ok': True, 'duration': dur}


@app.post('/api/burst/cancel')
async def api_burst_cancel():
    await _broadcaster.cancel_burst('cancelled')
    return {'ok': True}


@app.post('/api/stream/fft/start')
async def api_fft_start(body: SensorTarget):
    try:
        await _sensor_api().stream_fft_start(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


@app.post('/api/stream/fft/stop')
async def api_fft_stop(body: SensorTarget):
    try:
        await _sensor_api().stream_fft_stop(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


@app.post('/api/sensor/reset')
async def api_reset(body: SensorTarget):
    try:
        await _sensor_api().reset(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


@app.post('/api/sensor/boot')
async def api_boot_now(body: SensorTarget):
    # A firmware update lives inside the bootloader window: boot_now would jump
    # the sensor into the application and the upload would have nowhere to land.
    # The UI turns auto fast-boot off for the duration, but a second browser tab
    # (or a stale one) would not know that — so refuse it here as well.
    if fw_update.is_active():
        raise HTTPException(
            status_code=409,
            detail='A firmware update is in progress — fast-boot is blocked so the '
                   'sensor stays in its bootloader.')
    try:
        await _sensor_api().boot_now(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise _http_from(e)


# ── firmware update (TFTP) ───────────────────────────────────────────────────

class FwStartPayload(SensorTarget):
    file_id: str
    port:    int = fw_update.TFTP_PORT


@app.get('/api/fw/list')
async def api_fw_list():
    """Firmware files the server can offer, plus where it looked for them."""
    return {'files': fw_update.list_firmware(),
            'dirs':  [str(d) for d in fw_update.firmware_dirs()]}


@app.post('/api/fw/upload')
async def api_fw_upload(request: Request, name: str = ''):
    """Take a .sfb straight from the browser as a raw body (no multipart
    dependency) and keep it in the server's upload folder."""
    data = await request.body()
    try:
        return fw_update.save_upload(name, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post('/api/fw/start')
async def api_fw_start(body: FwStartPayload):
    try:
        return fw_update.start(
            target_ip=body.target_ip, mac=body.mac, password=body.password,
            file_id=body.file_id, port=body.port,
            sensor_api=_sensor_api(), mdns=_mdns, broadcaster=_broadcaster)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get('/api/fw/status')
async def api_fw_status():
    return fw_update.status()


@app.post('/api/fw/cancel')
async def api_fw_cancel():
    """Abandon a job that is still waiting (e.g. for a power cycle). Refused
    once the firmware is going over the wire."""
    try:
        return fw_update.cancel()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
