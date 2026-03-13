"""
WebSocket broadcaster + configurable-rate downsampler.

Accumulates FrameData samples in per-axis ring buffers.  On a timer (default
60 Hz), computes min/max/last for the accumulated chunk and broadcasts a JSON
envelope to every connected client.
"""

import asyncio
import json
import time
from collections import deque
from typing import Dict, Optional, Set

from fastapi import WebSocket
from protobuf_decoder import FrameData

class Broadcaster:
    def __init__(self, fps: int = 60):
        self._clients: Set[WebSocket] = set()
        # ring buffers keyed by axis label ('accel_x', 'accel_y', 'accel_z')
        self._buffers: Dict[str, deque] = {}
        self._last_frame: Optional[FrameData] = None
        self._stream_key: tuple = ()   # (device_id, stream_uid)
        self._units: Dict[str, str] = {}
        self._fps = fps
        self._interval = 1.0 / fps
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    # ── client management ────────────────────────────────────────────────────

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    # ── data ingestion ───────────────────────────────────────────────────────

    async def push(self, frame: FrameData):
        async with self._lock:
            new_key = (frame.device_id, frame.stream_uid)
            if new_key != self._stream_key:
                # Stream restarted (ODR/FSR change) — flush stale samples
                self._buffers.clear()
                self._stream_key = new_key
            self._last_frame = frame
            self._units.update(frame.units)
            for label, arr in frame.columns.items():
                if label not in self._buffers:
                    self._buffers[label] = deque()
                self._buffers[label].extend(arr.tolist())

    # ── broadcast loop ───────────────────────────────────────────────────────

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            t0 = time.monotonic()
            await self._emit()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self._interval - elapsed))

    async def _emit(self):
        if not self._clients:
            return

        async with self._lock:
            frame = self._last_frame
            snapshot: Dict[str, list] = {}
            for label, buf in self._buffers.items():
                snapshot[label] = list(buf)
                buf.clear()

        if not snapshot and frame is None:
            return

        # Build per-axis envelope
        axes_data: Dict[str, dict] = {}
        for label, samples in snapshot.items():
            if not samples:
                continue
            mn = float(min(samples))
            mx = float(max(samples))
            last = float(samples[-1])
            axes_data[label] = {'min': mn, 'max': mx, 'last': last, 'n': len(samples)}

        t_end_ns   = frame.recv_time_ns  if frame else 0
        rate_hz    = frame.sample_rate_hz if frame else 0.0
        stream_uid = frame.stream_uid     if frame else 0

        msg = json.dumps({
            'type':       'frame',
            't_end_ns':   t_end_ns,
            'rate_hz':    rate_hz,
            'stream_uid': stream_uid,
            'axes':       axes_data,
            'units':      dict(self._units),
        })

        await self._broadcast(msg)

    async def broadcast_status(self, connected: bool, logging: bool,
                               device_id: str = '', sensor_ip: str = '',
                               log_format: str = 'tsv'):
        msg = json.dumps({
            'type':       'status',
            'connected':  connected,
            'logging':    logging,
            'device_id':  device_id,
            'sensor_ip':  sensor_ip,
            'log_format': log_format,
        })
        await self._broadcast(msg)

    async def _broadcast(self, msg: str):
        dead: Set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead