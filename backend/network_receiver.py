"""
TCP listener — 0.0.0.0:8066
Frame format: 4-byte big-endian uint32 length prefix + raw protobuf bytes.
Runs in a background thread; pushes (payload_bytes, recv_time_ns) to an asyncio.Queue.
"""

import asyncio
import logging
import selectors
import socket
import struct
import threading
import time
from typing import Optional

_log = logging.getLogger('tcp')


class _ConnState:
    """Per-connection framing state machine."""
    __slots__ = ('buf', 'expected_len', 'recv_time_ns')

    def __init__(self):
        self.buf: bytearray = bytearray()
        self.expected_len: Optional[int] = None
        self.recv_time_ns: int = 0


class NetworkReceiver:
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
        self.connected_clients: int = 0
        self.remote_ip: Optional[str] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='tcp-receiver')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sel = selectors.DefaultSelector()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(4)
        server.setblocking(False)
        sel.register(server, selectors.EVENT_READ, data=None)
        _log.info('Listening for sensor on TCP %s:%d', self.host, self.port)

        states: dict[socket.socket, _ConnState] = {}

        try:
            while not self._stop.is_set():
                events = sel.select(timeout=0.1)
                for key, _ in events:
                    if key.data is None:
                        conn, addr = server.accept()
                        conn.setblocking(False)
                        state = _ConnState()
                        sel.register(conn, selectors.EVENT_READ, data=state)
                        states[conn] = state
                        self.connected_clients += 1
                        self.remote_ip = addr[0]
                        _log.info('Sensor connected from %s:%d', addr[0], addr[1])
                    else:
                        self._read(key.fileobj, key.data, sel, states)
        finally:
            for sock in list(states):
                sock.close()
            server.close()
            sel.close()

    def _read(self, sock: socket.socket, state: _ConnState, sel, states):
        try:
            data = sock.recv(65536)
        except (ConnectionError, OSError):
            data = b''

        if not data:
            sel.unregister(sock)
            states.pop(sock, None)
            sock.close()
            self.connected_clients = max(0, self.connected_clients - 1)
            _log.info('Sensor disconnected')
            return

        state.buf.extend(data)
        self._parse(state)

    def _parse(self, state: _ConnState):
        while True:
            if state.expected_len is None:
                if len(state.buf) < 4:
                    break
                state.expected_len = struct.unpack('>I', bytes(state.buf[:4]))[0]
                state.recv_time_ns = time.time_ns()
                del state.buf[:4]

            if len(state.buf) < state.expected_len:
                break

            payload = bytes(state.buf[:state.expected_len])
            del state.buf[:state.expected_len]
            state.expected_len = None

            asyncio.run_coroutine_threadsafe(
                self._queue.put((payload, state.recv_time_ns, 'tcp')),
                self._loop,
            )
