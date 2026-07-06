"""
UDP listener — 0.0.0.0:<port> (same port the sensor targets, i.e. server_port).

The sensor may stream raw-sample and/or FFT frames over UDP as an alternative
to TCP (see the firmware UDP transport note, 2026-07-02). Each datagram carries a
12-byte little-endian chunk header before a slice of the *same* length-prefixed
protobuf frame the TCP transport delivers:

    [ udp_chunk_hdr_t ][ chunk payload (up to 1400 bytes) ]

    struct udp_chunk_hdr_t {   // packed, little-endian, 12 bytes
        uint32_t packet_id;    // monotonic per logical encoded packet
        uint16_t chunk_idx;    // 0-based index within the packet
        uint16_t chunk_count;  // total chunks for this logical packet
        uint16_t chunk_size;   // payload bytes in THIS datagram (excl. header)
        uint16_t total_size;   // total bytes of the original encoded packet
    };

This receiver reassembles the chunks into the complete length-prefixed frame,
strips the 4-byte big-endian length prefix, and pushes the naked protobuf
payload onto the same asyncio.Queue the TCP receiver uses — everything
downstream (decode_any, broadcaster, logging) is transport-agnostic.

Runs in a background thread; pushes (payload_bytes, recv_time_ns) to the queue.
"""

import asyncio
import logging
import selectors
import socket
import struct
import threading
import time
from collections import Counter, deque
from typing import Optional

_log = logging.getLogger('udp')

# packet_id(uint32), chunk_idx(uint16), chunk_count(uint16),
# chunk_size(uint16), total_size(uint16) — packed little-endian.
_HDR = struct.Struct('<IHHHH')
_HDR_LEN = _HDR.size  # 12

# Max datagram = 12-byte header + up to 1400-byte payload; round up for safety.
_RECV_BUF = 2048

# Drop a partially-reassembled packet if it hasn't completed within this window.
_PACKET_TIMEOUT_NS = 100_000_000  # 100 ms

# Safety cap on concurrent in-flight packets per receiver (memory bound under
# heavy loss). Normal operation keeps only a handful (raw completes instantly,
# FFT within a few ms), so this is never hit in practice.
_MAX_PARTIALS = 256


class _Partial:
    """Accumulates chunks for one in-flight logical packet."""
    __slots__ = ('chunks', 'chunk_count', 'total_size', 'first_ns')

    def __init__(self, chunk_count: int, total_size: int, first_ns: int):
        self.chunks: dict[int, bytes] = {}
        self.chunk_count = chunk_count
        self.total_size = total_size
        self.first_ns = first_ns


class UDPReceiver:
    def __init__(
        self,
        host: str,
        port: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ):
        self.host = host
        self.port = port
        self._queue = queue
        self._loop = loop
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.remote_ip: Optional[str] = None
        # Diagnostics (surfaced in the status/stats broadcast).
        self.datagrams: int = 0
        self.reassembled: int = 0
        self.lost_packets: int = 0
        self.lost_chunks: int = 0
        # Deep diagnostics for troubleshooting reassembly (/api/udp/debug):
        #  - _diag_idx: how many datagrams arrived for each chunk_idx. If, say,
        #    idx 2 is far rarer than 0/1, the sensor/network isn't delivering it.
        #  - _diag_count: distribution of chunk_count values seen.
        #  - _diag_lost: recent lost packets with exactly which chunk_idx were
        #    present vs missing (tells us WHICH chunk goes astray).
        self._diag_idx: Counter = Counter()
        self._diag_count: Counter = Counter()
        self._diag_size = {'min_total': None, 'max_total': 0, 'max_chunk': 0}
        self._diag_lost: deque = deque(maxlen=40)
        # Reassembly buffers keyed by (source_ip, packet_id).
        self._partials: dict[tuple, _Partial] = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='udp-receiver')
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ── stats snapshot for the status broadcast ──────────────────────────────
    def stats(self) -> dict:
        return {
            'udp_datagrams': self.datagrams,
            'udp_packets': self.reassembled,
            'udp_lost_packets': self.lost_packets,
            'udp_lost_chunks': self.lost_chunks,
        }

    def reset_stats(self):
        """Zero the diagnostic counters (for a fresh monitoring session)."""
        self.datagrams = 0
        self.reassembled = 0
        self.lost_packets = 0
        self.lost_chunks = 0
        self._diag_idx.clear()
        self._diag_count.clear()
        self._diag_size = {'min_total': None, 'max_total': 0, 'max_chunk': 0}
        self._diag_lost.clear()

    def _run(self):
        sel = selectors.DefaultSelector()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Enlarge the receive buffer to survive high-rate bursts.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        sock.bind((self.host, self.port))
        sock.setblocking(False)
        sel.register(sock, selectors.EVENT_READ, data=None)
        _log.info('Listening for sensor on UDP %s:%d', self.host, self.port)

        try:
            while not self._stop.is_set():
                for _key, _ in sel.select(timeout=0.1):
                    self._drain_socket(sock)
                # Timeout sweep runs even when idle so stale partials don't leak.
                self._evict_stale()
        finally:
            sock.close()
            sel.close()

    def _drain_socket(self, sock: socket.socket):
        # Pull all currently-queued datagrams before returning to select().
        while True:
            try:
                data, addr = sock.recvfrom(_RECV_BUF)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not data:
                return
            self.datagrams += 1
            self.remote_ip = addr[0]
            self._handle(data, addr[0])

    def _handle(self, data: bytes, src_ip: str):
        if len(data) < _HDR_LEN:
            return
        packet_id, chunk_idx, chunk_count, chunk_size, total_size = _HDR.unpack_from(data, 0)
        # Diagnostics: record what actually arrived on the wire (before any
        # validation), so we can see per-chunk_idx arrival and header values.
        self._diag_idx[chunk_idx] += 1
        self._diag_count[chunk_count] += 1
        if total_size:
            mt = self._diag_size['min_total']
            self._diag_size['min_total'] = total_size if mt is None else min(mt, total_size)
            self._diag_size['max_total'] = max(self._diag_size['max_total'], total_size)
        self._diag_size['max_chunk'] = max(self._diag_size['max_chunk'], chunk_size)

        if chunk_count == 0 or chunk_idx >= chunk_count:
            self._diag_lost.append({'packet_id': packet_id, 'reason': 'malformed_header',
                                    'chunk_idx': chunk_idx, 'chunk_count': chunk_count,
                                    'chunk_size': chunk_size, 'datalen': len(data)})
            return  # malformed header
        payload = data[_HDR_LEN:_HDR_LEN + chunk_size]

        # Fast path: single-chunk packet needs no buffering.
        if chunk_count == 1:
            self._complete(payload)
            return

        now = time.monotonic_ns()
        key = (src_ip, packet_id)
        part = self._partials.get(key)
        if part is None:
            part = _Partial(chunk_count, total_size, now)
            self._partials[key] = part
            # Memory safety only: if an implausible number of packets are in
            # flight (heavy loss), drop the oldest and count it lost.
            if len(self._partials) > _MAX_PARTIALS:
                oldest = min(self._partials, key=lambda k: self._partials[k].first_ns)
                if oldest != key:
                    p = self._partials.pop(oldest)
                    self.lost_packets += 1
                    self.lost_chunks += max(0, p.chunk_count - len(p.chunks))
                    self._record_lost(oldest[1], p, 'overflow')
        part.chunks[chunk_idx] = payload

        if len(part.chunks) >= part.chunk_count:
            frame = b''.join(part.chunks[i] for i in range(part.chunk_count))
            del self._partials[key]
            self._complete(frame)

        # NOTE: we intentionally do NOT discard lower packet_ids here. Raw and
        # FFT share one monotonically-increasing packet_id space on the same UDP
        # port, so a higher packet_id usually belongs to the OTHER stream (e.g. a
        # 1-chunk raw packet arriving mid-assembly of a multi-chunk FFT packet)
        # and does NOT mean the in-progress packet was lost. Discarding on a
        # newer id (the old behaviour) dropped every FFT packet ≥3 chunks,
        # regardless of data rate. Genuinely incomplete packets are reclaimed by
        # the 100 ms timeout in _evict_stale().

    def _evict_stale(self):
        if not self._partials:
            return
        now = time.monotonic_ns()
        for k in list(self._partials.keys()):
            part = self._partials[k]
            if now - part.first_ns > _PACKET_TIMEOUT_NS:
                self._partials.pop(k)
                self.lost_packets += 1
                self.lost_chunks += max(0, part.chunk_count - len(part.chunks))
                self._record_lost(k[1], part, 'timeout')
                _log.debug('UDP packet %d from %s timed out (%d/%d chunks)',
                           k[1], k[0], len(part.chunks), part.chunk_count)

    def _record_lost(self, packet_id: int, part: '_Partial', reason: str):
        """Note which chunk_idx were present vs missing for a lost packet."""
        present = sorted(part.chunks.keys())
        missing = sorted(set(range(part.chunk_count)) - set(part.chunks.keys()))
        self._diag_lost.append({
            'packet_id': packet_id, 'reason': reason,
            'chunk_count': part.chunk_count,
            'present': present, 'missing': missing,
            'total_size': part.total_size,
            'recv_bytes': sum(len(c) for c in part.chunks.values()),
        })

    def debug_info(self) -> dict:
        """Deep reassembly diagnostics for /api/udp/debug."""
        return {
            'datagrams': self.datagrams,
            'reassembled': self.reassembled,
            'lost_packets': self.lost_packets,
            'lost_chunks': self.lost_chunks,
            'in_flight': len(self._partials),
            'chunk_count_hist': dict(self._diag_count),
            'chunk_idx_hist': dict(sorted(self._diag_idx.items())),
            'sizes': dict(self._diag_size),
            'recent_lost': list(self._diag_lost),
        }

    def _complete(self, frame: bytes):
        """Strip the 4-byte length prefix and queue the naked protobuf payload."""
        if len(frame) < 4:
            return
        (plen,) = struct.unpack_from('>I', frame, 0)
        body = frame[4:4 + plen]
        if len(body) < plen:
            self._diag_lost.append({'reason': 'truncated_frame',
                                    'declared': plen, 'got': len(body),
                                    'frame_len': len(frame)})
            _log.warning('UDP frame truncated: declared %d, got %d', plen, len(body))
            return
        self.reassembled += 1
        recv_time_ns = time.time_ns()
        asyncio.run_coroutine_threadsafe(
            self._queue.put((body, recv_time_ns, 'udp')),
            self._loop,
        )
