"""Shared harness for the hardware test tools.

Holds the pieces every bench tool needs: a receiver/capture wrapper that keeps
the transport tag, a numpy Welch, the scalloping-immune tone estimator, the
firmware's own reference spectrum, and the config enum maps.
"""
import asyncio
import math
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'protobuf'))

import numpy as np

import protobuf_decoder as dec
import message_pb2
import sensor_api
from network_receiver import NetworkReceiver
from udp_receiver import UDPReceiver

G = 9.80665
ENBW = 1.5                                  # periodic Hann, in bins

ODR_ENUM = {1: 'ODR_DIV_1', 2: 'ODR_DIV_2', 4: 'ODR_DIV_4', 8: 'ODR_DIV_8',
            16: 'ODR_DIV_16', 32: 'ODR_DIV_32', 64: 'ODR_DIV_64',
            128: 'ODR_DIV_128', 256: 'ODR_DIV_256'}
SIZE_ENUM = {256: 'FFT_SIZE_256', 512: 'FFT_SIZE_512',
             1024: 'FFT_SIZE_1024', 2048: 'FFT_SIZE_2048'}
PREC_ENUM = {'float32': 'FFT_PRECISION_FLOAT32', 'q15': 'FFT_PRECISION_Q15'}

# AxisMask -> (number of payload columns, axes combined). Vector modes emit one
# unsigned column holding sqrt(sum of squares) of the listed axes.
AXIS_MODES = {
    'AXIS_X': (1, ('x',), False), 'AXIS_Y': (1, ('y',), False),
    'AXIS_Z': (1, ('z',), False), 'AXIS_XY': (2, ('x', 'y'), False),
    'AXIS_XZ': (2, ('x', 'z'), False), 'AXIS_YZ': (2, ('y', 'z'), False),
    'AXIS_XYZ': (3, ('x', 'y', 'z'), False),
    'AXIS_XY_VECTOR': (1, ('x', 'y'), True),
    'AXIS_XZ_VECTOR': (1, ('x', 'z'), True),
    'AXIS_YZ_VECTOR': (1, ('y', 'z'), True),
    'AXIS_XYZ_VECTOR': (1, ('x', 'y', 'z'), True),
}

# LP2 cutoff that keeps the anti-alias corner below each decimated Nyquist,
# per the guidance in the OdrDiv enum (cutoffs divide the fixed 26.667 kHz ODR).
AUTO_CUTOFF = {1: 'CUTOFF_6p66_KHZ', 2: 'CUTOFF_6p66_KHZ', 4: 'CUTOFF_2p66_KHZ',
               8: 'CUTOFF_1p33_KHZ', 16: 'CUTOFF_0p59_KHZ', 32: 'CUTOFF_0p26_KHZ',
               64: 'CUTOFF_0p13_KHZ', 128: 'CUTOFF_0p06_KHZ', 256: 'CUTOFF_0p03_KHZ'}


def port_in_use(port):
    """True if anything already holds the sensor port.

    The receivers set SO_REUSEADDR, so starting a bench tool while the viewer
    backend runs does NOT fail — the kernel hands the sensor's datagrams to one
    socket and the backend silently stops receiving. Refuse rather than steal.
    """
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        s = socket.socket(socket.AF_INET, kind)
        try:
            s.bind(('0.0.0.0', port))
        except OSError:
            return True
        finally:
            s.close()
    return False


def welch(x, fs, nperseg=None, overlap=0.5):
    """One-sided PSD in (unit)^2/Hz, plus the segment count. Hann, DC removed."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if nperseg is None:
        nperseg = int(min(4096, max(64, 2 ** math.floor(math.log2(max(64, n // 8))))))
    nperseg = min(nperseg, n)
    step = max(1, int(nperseg * (1.0 - overlap)))
    w = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(nperseg) / nperseg))
    U = np.sum(w ** 2)
    acc, count = None, 0
    for s in range(0, n - nperseg + 1, step):
        seg = x[s:s + nperseg]
        seg = seg - seg.mean()
        P = (np.abs(np.fft.rfft(seg * w)) ** 2) / (fs * U)
        P[1:-1] *= 2.0
        acc = P if acc is None else acc + P
        count += 1
    if not count:
        return np.array([]), np.array([]), 0
    return np.fft.rfftfreq(nperseg, 1.0 / fs), acc / count, count


def tone_amplitude(mag, k_peak, lobe=2):
    """Amplitude of a tone from an 0x1033 amplitude spectrum, immune to
    scalloping loss (a peak-bin reading alone varies up to 15 % with where the
    tone falls between bins) and needing no bin width."""
    lo = max(0, k_peak - lobe)
    hi = min(len(mag), k_peak + lobe + 1)
    return math.sqrt(float(np.sum(np.asarray(mag[lo:hi], dtype=np.float64) ** 2)) / ENBW)


def firmware_spectrum(x, n):
    """The firmware's reference implementation, averaged over whole segments."""
    k = np.arange(n)
    w = 0.5 * (1.0 - np.cos(2.0 * np.pi * k / n)) * (4.0 / n)
    seg = int(len(x) // n)
    acc = None
    for i in range(seg):
        X = np.fft.rfft(x[i * n:(i + 1) * n] * w)
        X[0] *= 0.5
        a = np.abs(X[:n // 2])
        acc = a if acc is None else acc + a
    return acc / max(1, seg)


class Capture:
    """Runs both receivers and hands back decoded frames with their transport."""

    def __init__(self, port):
        self.port = port
        self.q: asyncio.Queue = asyncio.Queue()
        self.tcp = self.udp = None

    async def start(self, loop=None):
        loop = loop or asyncio.get_running_loop()
        self.tcp = NetworkReceiver('0.0.0.0', self.port, self.q, loop)
        self.udp = UDPReceiver('0.0.0.0', self.port, self.q, loop)
        self.tcp.start()
        self.udp.start()

    def stop(self):
        for r in (self.tcp, self.udp):
            if r:
                r.stop()

    async def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    async def raw(self, want_samples, timeout):
        """Collect raw frames until want_samples per column. Returns
        (frames, transports)."""
        loop = asyncio.get_running_loop()
        frames, transports, got = [], [], 0
        deadline = loop.time() + timeout
        while got < want_samples and loop.time() < deadline:
            try:
                item = await asyncio.wait_for(
                    self.q.get(), timeout=max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            f = dec.decode_any(item[0], item[1])
            if isinstance(f, dec.FrameData):
                frames.append(f)
                transports.append(item[2] if len(item) > 2 else '?')
                got += len(next(iter(f.columns.values()), []))
        return frames, transports

    async def fft(self, want, timeout, fft_size=None):
        """Collect FFT frames. Returns (frames, headers, transports)."""
        loop = asyncio.get_running_loop()
        frames, headers, transports = [], [], []
        deadline = loop.time() + timeout
        while len(frames) < want and loop.time() < deadline:
            try:
                item = await asyncio.wait_for(
                    self.q.get(), timeout=max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            h = message_pb2.Header()
            try:
                h.ParseFromString(item[0])
            except Exception:
                continue
            if not h.HasField('fft_stream'):
                continue
            if fft_size and h.fft_stream.fft_bins * 2 != fft_size:
                continue
            f = dec.decode_fft_frame(item[0], item[1])
            if f is None:
                continue
            frames.append(f)
            headers.append(h)
            transports.append(item[2] if len(item) > 2 else '?')
        return frames, headers, transports


def discover_ip(mac, timeout=8.0, interface_ip=''):
    """Current IP of a sensor, by MAC, via mDNS. Returns None if not seen."""
    import time as _t
    from mdns_scanner import MDNSScanner
    sc = MDNSScanner(interface_ip)
    sc.start()
    try:
        deadline = _t.monotonic() + timeout
        want = mac.lower()
        while _t.monotonic() < deadline:
            for d in sc.get_devices():
                if d.get('mac', '').lower() == want and d.get('ip'):
                    return d['ip']
            _t.sleep(0.5)
    finally:
        sc.stop()
    return None


async def wait_for_sensor(ip, mac, password, timeout=90.0, poll=3.0):
    """Poll until the sensor answers again after a reset.

    Returns (info, ip). The IP is re-discovered by MAC over mDNS, because a
    sensor on DHCP frequently comes back on a DIFFERENT address after a reboot
    — talking to the old one produces a stream of confusing timeouts.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        try:
            return await sensor_api.get_sensor_info(ip, mac, password), ip
        except Exception as e:                      # noqa: BLE001 - any API error
            last = e
            found = await asyncio.get_running_loop().run_in_executor(
                None, discover_ip, mac, 4.0)
            if found and found != ip:
                ip = found
                continue
            await asyncio.sleep(poll)
    raise TimeoutError(f'sensor did not come back within {timeout:.0f}s ({last})')
