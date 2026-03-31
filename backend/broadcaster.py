"""
WebSocket broadcaster + configurable-rate downsampler.

Accumulates FrameData samples in per-axis ring buffers.  On a timer (default
60 Hz), computes min/max/last for the accumulated chunk and broadcasts a JSON
envelope to every connected client.

FFT spectra are throttled to 30 Hz — each result is a complete snapshot so
no downsampling is needed, just rate-limiting.
"""

import asyncio
import json
import time
from collections import deque
from typing import Dict, Optional, Set

from fastapi import WebSocket
from protobuf_decoder import FrameData, FFTFrameData

class Broadcaster:
    def __init__(self, fps: int = 60):
        self._clients: Set[WebSocket] = set()
        # ring buffers keyed by axis label ('accel_x', 'accel_y', 'accel_z')
        self._buffers: Dict[str, deque] = {}
        self._last_frame: Optional[FrameData] = None
        self._stream_key: tuple = ()   # (device_id, stream_uid)
        self._units: Dict[str, str] = {}
        self._fps = fps
        self._max_fps = fps
        self._interval = 1.0 / fps
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

        # FFT state
        self._fft_frame: Optional[FFTFrameData] = None
        self._fft_dirty: bool = False
        self._fft_lock = asyncio.Lock()
        self._fft_task: Optional[asyncio.Task] = None
        self._fft_interval = 1.0 / 30  # 30 Hz throttle

        # Pause flag — when True, buffers are drained but nothing is sent
        self._paused: bool = False

        # Burst capture state
        self._burst_active: bool = False
        self._burst_samples: Dict[str, list] = {}
        self._burst_t_start_ns: int = 0
        self._burst_duration: float = 0.0
        self._burst_start_time: float = 0.0
        self._burst_fft_frames: list = []
        self._was_paused: bool = False

    # ── client management ────────────────────────────────────────────────────

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    # ── pause / resume ─────────────────────────────────────────────────────

    def pause(self):
        self._paused = True
        # Clear buffers so no stale data is sent when resumed
        self._buffers.clear()

    def resume(self):
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    # ── burst capture ──────────────────────────────────────────────────────

    def start_burst(self, duration: float):
        self._burst_samples.clear()
        self._burst_fft_frames = []
        self._burst_t_start_ns = 0
        self._burst_duration = duration
        self._burst_start_time = time.monotonic()
        self._burst_active = True
        # Pause decimated streaming so it doesn't fight with burst data on frontend
        self._was_paused = self._paused
        self._paused = True

    @property
    def burst_active(self) -> bool:
        return self._burst_active

    # ── data ingestion ───────────────────────────────────────────────────────

    async def push(self, frame: FrameData):
        burst_done = False
        async with self._lock:
            new_key = (frame.device_id, frame.stream_uid)
            if new_key != self._stream_key:
                self._buffers.clear()
                self._stream_key = new_key
            self._last_frame = frame
            self._units.update(frame.units)
            # Adapt broadcast rate: full speed for decimated, slower for raw passthrough
            if frame.sample_rate_hz > 0 and frame.sample_rate_hz < 500:
                # Raw passthrough mode — emit at ~5 FPS to batch samples
                self._interval = 0.2
            else:
                self._interval = 1.0 / self._max_fps
            for label, arr in frame.columns.items():
                if label not in self._buffers:
                    self._buffers[label] = deque()
                self._buffers[label].extend(arr.tolist())

            # Accumulate for burst capture
            if self._burst_active:
                if self._burst_t_start_ns == 0:
                    self._burst_t_start_ns = frame.recv_time_ns
                for label, arr in frame.columns.items():
                    if label not in self._burst_samples:
                        self._burst_samples[label] = []
                    self._burst_samples[label].extend(arr.tolist())
                if time.monotonic() - self._burst_start_time >= self._burst_duration:
                    burst_done = True

        if burst_done:
            await self._finish_burst(frame)

    async def push_fft(self, fft: FFTFrameData):
        async with self._fft_lock:
            self._fft_frame = fft
            self._fft_dirty = True
            if self._burst_active:
                self._burst_fft_frames.append({
                    'seq':        fft.seq,
                    'fft_bins':   fft.fft_bins,
                    'fft_size':   fft.fft_size,
                    'freq_hz':    fft.freq_hz,
                    'magnitudes': fft.magnitudes,
                    'psd':        fft.psd,
                    'unit':       fft.unit,
                })

    async def _finish_burst(self, frame: FrameData):
        self._burst_active = False
        # Keep streaming paused after burst so data isn't immediately overwritten.
        # User clicks Stream ▶ to resume when done inspecting.
        msg = json.dumps({
            'type':           'burst',
            'sample_rate_hz': frame.sample_rate_hz,
            'stream_uid':     frame.stream_uid,
            't_start_ns':     self._burst_t_start_ns,
            't_end_ns':       frame.recv_time_ns,
            'samples':        self._burst_samples,
            'units':          dict(self._units),
            'fft_snapshots':  self._burst_fft_frames,
        })
        self._burst_samples = {}
        self._burst_fft_frames = []
        await self._broadcast(msg)

    # ── broadcast loop ───────────────────────────────────────────────────────

    def start(self):
        self._task = asyncio.create_task(self._loop())
        self._fft_task = asyncio.create_task(self._fft_loop())

    async def _loop(self):
        while True:
            t0 = time.monotonic()
            await self._emit()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self._interval - elapsed))

    async def _fft_loop(self):
        while True:
            t0 = time.monotonic()
            await self._emit_fft()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, self._fft_interval - elapsed))

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

        if self._paused:
            return  # drain buffers but don't send

        t_end_ns       = frame.recv_time_ns  if frame else 0
        device_time_ns = frame.timestamp_ns   if frame else 0
        rate_hz        = frame.sample_rate_hz if frame else 0.0
        stream_uid     = frame.stream_uid     if frame else 0

        # At very low sample rates, send all raw samples instead of decimating
        if rate_hz > 0 and rate_hz < 500:
            msg = json.dumps({
                'type':           'frame',
                't_end_ns':       t_end_ns,
                'device_time_ns': device_time_ns,
                'rate_hz':        rate_hz,
                'stream_uid':     stream_uid,
                'raw_samples':    snapshot,
                'units':          dict(self._units),
            })
        else:
            # Build per-axis envelope (decimated)
            axes_data: Dict[str, dict] = {}
            for label, samples in snapshot.items():
                if not samples:
                    continue
                mn = float(min(samples))
                mx = float(max(samples))
                last = float(samples[-1])
                axes_data[label] = {'min': mn, 'max': mx, 'last': last, 'n': len(samples)}

            msg = json.dumps({
                'type':           'frame',
                't_end_ns':       t_end_ns,
                'device_time_ns': device_time_ns,
                'rate_hz':        rate_hz,
                'stream_uid':     stream_uid,
                'axes':           axes_data,
                'units':          dict(self._units),
            })

        await self._broadcast(msg)

    async def _emit_fft(self):
        if not self._clients:
            return

        async with self._fft_lock:
            if not self._fft_dirty:
                return
            fft = self._fft_frame
            self._fft_dirty = False

        if fft is None:
            return

        if self._paused:
            return  # dirty flag already cleared; skip send

        msg = json.dumps({
            'type':           'fft',
            'fft_bins':       fft.fft_bins,
            'fft_size':       fft.fft_size,
            'freq_hz':        fft.freq_hz,
            'magnitudes':     fft.magnitudes,
            'psd':            fft.psd,
            'unit':           fft.unit,
            'seq':            fft.seq,
            't_end_ns':       fft.recv_time_ns,
            'device_time_ns': fft.timestamp_ns,
        })

        await self._broadcast(msg)

    async def broadcast_status(self, connected: bool, logging: bool,
                               device_id: str = '', sensor_ip: str = '',
                               log_format: str = 'tsv',
                               stats: dict | None = None):
        envelope = {
            'type':       'status',
            'connected':  connected,
            'logging':    logging,
            'streaming':  not self._paused,
            'burst':      self._burst_active,
            'device_id':  device_id,
            'sensor_ip':  sensor_ip,
            'log_format': log_format,
        }
        if stats:
            envelope['stats'] = stats
        msg = json.dumps(envelope)
        await self._broadcast(msg)

    async def _broadcast(self, msg: str):
        dead: Set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead
