"""
mDNS sensor discovery using zeroconf.

Runs a persistent ServiceBrowser for '_nw-config._udp.local.' and maintains
a live dict of discovered sensors.  Follows the same TXT-record parsing as
Utils/Console/commands/cmd_scan.py.
"""

import logging
import socket
import threading
import time
from typing import Optional

from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

_log = logging.getLogger('mdns')

SERVICE_TYPE = '_nw-config._udp.local.'


def _get_txt(props: dict[bytes, bytes], key: str, default: str = '') -> str:
    bkey = key.encode()
    val = props.get(bkey)
    if val is not None:
        try:
            return val.decode(errors='replace')
        except Exception:
            return default
    return default


def _ip_str(raw: bytes) -> str:
    return socket.inet_ntoa(raw)


_DEVICE_TTL = 30  # seconds — remove devices not seen for this long


class MDNSScanner:
    def __init__(self):
        self._devices: dict[str, dict] = {}   # keyed by service name
        self._lock = threading.Lock()
        self._zc: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None

    def start(self):
        self._zc = Zeroconf()
        self._browser = ServiceBrowser(
            self._zc, SERVICE_TYPE, handlers=[self._on_change])
        _log.info('mDNS scanner started for %s', SERVICE_TYPE)

    def stop(self):
        if self._zc:
            self._zc.close()
            self._zc = None
            self._browser = None
            _log.info('mDNS scanner stopped')

    def get_devices(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            # Prune stale devices
            stale = [name for name, d in self._devices.items()
                     if now - d.get('_seen', 0) > _DEVICE_TTL]
            for name in stale:
                removed = self._devices.pop(name)
                _log.info('Sensor expired (not seen for %ds): %s',
                          _DEVICE_TTL, removed.get('mac', '?'))
            return [
                {k: v for k, v in d.items() if not k.startswith('_')}
                for d in self._devices.values()
            ]

    def _on_change(self, zeroconf: Zeroconf, service_type: str,
                   name: str, state_change: ServiceStateChange):
        if state_change == ServiceStateChange.Removed:
            with self._lock:
                removed = self._devices.pop(name, None)
            if removed:
                _log.info('Sensor removed: %s (%s)', removed.get('mac', '?'), name)
            return

        # Added or Updated
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return

        txt = info.properties or {}
        if b'mac' not in txt:
            return

        mac = _get_txt(txt, 'mac')
        ip_list = [_ip_str(a) for a in info.addresses] if info.addresses else []
        ip = ip_list[0] if ip_list else ''

        entry = {
            'mac':       mac,
            'ip':        ip,
            'hostname':  (info.server or '').rstrip('.'),
            'port':      info.port,
            'mode':      _get_txt(txt, 'mode'),
            'hw':        _get_txt(txt, 'hw'),
            'fw_app':    _get_txt(txt, 'fw_app'),
            'fw_bl':     _get_txt(txt, 'fw_bl'),
            'vendor_id': _get_txt(txt, 'vendor_id'),
            'sensor_id': _get_txt(txt, 'sensor_id'),
        }

        entry['_seen'] = time.monotonic()
        with self._lock:
            is_new = name not in self._devices
            self._devices[name] = entry

        if is_new:
            _log.info('Sensor discovered: %s  IP=%s  mode=%s', mac, ip, entry['mode'])
        else:
            _log.debug('Sensor updated: %s  IP=%s', mac, ip)
