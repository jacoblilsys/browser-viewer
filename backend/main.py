"""
FastAPI application — wires together all backend components.

Start with:
    cd Utils/browser_viewer/backend
    uvicorn main:app --reload --port 8000
"""

import asyncio
import os
import sys

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
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from network_receiver import NetworkReceiver
from protobuf_decoder import decode_any, FrameData, FFTFrameData
from broadcaster import Broadcaster
from log_writer import LogManager
from mdns_scanner import MDNSScanner

# ── configuration ─────────────────────────────────────────────────────────────

_TCP_HOST   = '0.0.0.0'
_TCP_PORT   = int(os.environ.get('TCP_PORT', '8066'))
_LOG_DIR    = os.environ.get('LOG_DIR', './logs')
_WS_FPS     = int(os.environ.get('WS_FPS', '60'))
_NETWORK_IF = os.environ.get('NETWORK_IF', '')
_FRONTEND   = Path(__file__).parent.parent / 'frontend'


def _get_local_ip() -> str:
    """Return NETWORK_IF env var if set, otherwise auto-detect via routing table."""
    if _NETWORK_IF:
        return _NETWORK_IF
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
_broadcaster  = Broadcaster(fps=_WS_FPS)
_log_writer   = LogManager(output_dir=_LOG_DIR)
_mdns         = MDNSScanner()
_logging_on   = False

# ── counters ──────────────────────────────────────────────────────────────────
_stats = {
    'frames': 0,
    'samples': 0,
    'fft_frames': 0,
    'packets': 0,
    'errors': 0,
}


# ── lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _receiver

    _ip = _get_local_ip()
    _if = _get_interface_name(_ip)
    _if_label = f' ({_if})' if _if else ''
    _log_host = logging.getLogger('host')
    _log_host.info('Network interface: %s%s', _ip, _if_label)
    _log_host.info('Sensor TCP port: %d', _TCP_PORT)
    if not _NETWORK_IF:
        _log_host.info(
            'Tip: set NETWORK_IF=<ip> env var to force a specific interface'
        )

    loop = asyncio.get_event_loop()
    _receiver = NetworkReceiver(_TCP_HOST, _TCP_PORT, _queue, loop)
    _receiver.start()
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
    _mdns.stop()
    _log_writer.close_all()


app = FastAPI(lifespan=lifespan)

# Serve frontend static files
if _FRONTEND.exists():
    app.mount('/static', StaticFiles(directory=str(_FRONTEND)), name='static')


# ── queue drain ───────────────────────────────────────────────────────────────

async def _drain_queue():
    import logging as _logging
    _log = _logging.getLogger('drain')
    global _logging_on
    _last_device_id = ''
    while True:
        payload_bytes, recv_time_ns = await _queue.get()
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
            _last_device_id = result.device_id

            if _logging_on:
                try:
                    _log_writer.write(result)
                except Exception as e:
                    _log.warning('log_writer failed: %s', e)

            await _broadcaster.push(result)

            connected = _receiver.connected_clients > 0 if _receiver else False
            sensor_ip = _receiver.remote_ip or '' if _receiver else ''
            await _broadcaster.broadcast_status(
                connected=connected,
                logging=_logging_on,
                device_id=result.device_id,
                sensor_ip=sensor_ip,
                log_format=_log_writer.fmt,
                stats=dict(_stats),
            )

        elif isinstance(result, FFTFrameData):
            _stats['fft_frames'] += 1
            _last_device_id = result.device_id

            if _logging_on:
                try:
                    _log_writer.write_fft(result)
                except Exception as e:
                    _log.warning('log_writer fft failed: %s', e)

            await _broadcaster.push_fft(result)


async def _status_heartbeat():
    """Broadcast TCP connection state every second even without data frames."""
    import logging as _logging
    _log = _logging.getLogger('heartbeat')
    _last_device_id = ''
    while True:
        await asyncio.sleep(1.0)
        connected = _receiver.connected_clients > 0 if _receiver else False
        sensor_ip = _receiver.remote_ip or '' if _receiver else ''
        await _broadcaster.broadcast_status(
            connected=connected,
            logging=_logging_on,
            device_id=_last_device_id,
            sensor_ip=sensor_ip,
            log_format=_log_writer.fmt,
            stats=dict(_stats),
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
async def api_host():
    ip = _get_local_ip()
    return {'ip': ip, 'tcp_port': _TCP_PORT, 'if_name': _get_interface_name(ip)}


@app.get('/api/status')
async def api_status():
    connected = _receiver.connected_clients > 0 if _receiver else False
    return {
        'connected': connected,
        'logging':   _logging_on,
        'streaming': not _broadcaster.paused,
        'tcp_port':  _TCP_PORT,
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


@app.post('/api/stats/reset')
async def api_stats_reset():
    for k in _stats:
        _stats[k] = 0
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
    dhcp:          Optional[str] = None
    data_stream:   Optional[str] = None
    fft_stream:    Optional[str] = None


def _sensor_api():
    import sensor_api
    return sensor_api


@app.post('/api/sensor/info')
async def sensor_info(body: SensorTarget):
    try:
        return await _sensor_api().get_sensor_info(body.target_ip, body.mac, body.password)
    except Exception as e:
        msg = f'{type(e).__name__}: {e}' if str(e) else type(e).__name__
        raise HTTPException(status_code=502, detail=msg)


@app.post('/api/sensor/config')
async def sensor_config_get(body: SensorTarget):
    try:
        return await _sensor_api().get_sensor_config(body.target_ip, body.mac, body.password)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
        )
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/network/config')
async def network_config_get(body: SensorTarget):
    try:
        return await _sensor_api().get_network_config(body.target_ip, body.mac, body.password)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/network/config/set')
async def network_config_set(body: NetworkConfigPayload):
    try:
        kwargs = {k: v for k, v in body.model_dump().items()
                  if k not in ('target_ip', 'mac', 'password') and v is not None}
        await _sensor_api().set_network_config(
            body.target_ip, body.mac, body.password, **kwargs)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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


@app.post('/api/stream/fft/start')
async def api_fft_start(body: SensorTarget):
    try:
        await _sensor_api().stream_fft_start(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/stream/fft/stop')
async def api_fft_stop(body: SensorTarget):
    try:
        await _sensor_api().stream_fft_stop(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/sensor/reset')
async def api_reset(body: SensorTarget):
    try:
        await _sensor_api().reset(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/sensor/boot')
async def api_boot_now(body: SensorTarget):
    try:
        await _sensor_api().boot_now(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
