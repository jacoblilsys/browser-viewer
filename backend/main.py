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
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from network_receiver import NetworkReceiver
from protobuf_decoder import decode_frame
from broadcaster import Broadcaster
from log_writer import LogWriter

# ── configuration ─────────────────────────────────────────────────────────────

_TCP_HOST   = '0.0.0.0'
_TCP_PORT   = 8066
_LOG_DIR    = os.environ.get('LOG_DIR', './logs')
_FRONTEND   = Path(__file__).parent.parent / 'frontend'

# ── global state ─────────────────────────────────────────────────────────────

_queue:    asyncio.Queue = asyncio.Queue(maxsize=4096)
_receiver: Optional[NetworkReceiver] = None
_broadcaster  = Broadcaster()
_log_writer   = LogWriter(output_dir=_LOG_DIR)
_logging_on   = False


# ── lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _receiver

    loop = asyncio.get_event_loop()
    _receiver = NetworkReceiver(_TCP_HOST, _TCP_PORT, _queue, loop)
    _receiver.start()
    _broadcaster.start()

    asyncio.create_task(_drain_queue())
    asyncio.create_task(_status_heartbeat())

    yield

    _receiver.stop()
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
        try:
            frame = decode_frame(payload_bytes, recv_time_ns)
        except Exception as e:
            _log.warning('decode_frame failed: %s', e)
            continue

        if frame is None:
            continue

        _last_device_id = frame.device_id

        if _logging_on:
            try:
                _log_writer.write(frame)
            except Exception as e:
                _log.warning('log_writer failed: %s', e)

        await _broadcaster.push(frame)

        connected = _receiver.connected_clients > 0 if _receiver else False
        await _broadcaster.broadcast_status(
            connected=connected,
            logging=_logging_on,
            device_id=frame.device_id,
        )


async def _status_heartbeat():
    """Broadcast TCP connection state every second even without data frames."""
    import logging as _logging
    _log = _logging.getLogger('heartbeat')
    _last_device_id = ''
    while True:
        await asyncio.sleep(1.0)
        connected = _receiver.connected_clients > 0 if _receiver else False
        await _broadcaster.broadcast_status(
            connected=connected,
            logging=_logging_on,
            device_id=_last_device_id,
        )


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get('/')
async def index():
    index_path = _FRONTEND / 'index.html'
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({'message': 'Browser Viewer backend running. Frontend not found.'})


@app.get('/api/status')
async def api_status():
    connected = _receiver.connected_clients > 0 if _receiver else False
    return {
        'connected': connected,
        'logging':   _logging_on,
        'tcp_port':  _TCP_PORT,
    }


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


class NetworkConfigPayload(SensorTarget):
    ip:          Optional[str] = None
    netmask:     Optional[str] = None
    gateway:     Optional[str] = None
    dhcp:        Optional[str] = None
    data_stream: Optional[str] = None


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
async def api_stream_start(body: SensorTarget):
    try:
        await _sensor_api().stream_start(body.target_ip, body.mac, body.password)
        return {'ok': True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post('/api/stream/stop')
async def api_stream_stop(body: SensorTarget):
    try:
        await _sensor_api().stream_stop(body.target_ip, body.mac, body.password)
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
