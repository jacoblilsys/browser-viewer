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


_DEVICE_TTL    = 120  # seconds — remove devices not seen for this long
_REQUERY_INTERVAL = 30  # seconds — actively re-query known services


class MDNSScanner:
    def __init__(self, interface_ip: str = ''):
        self._devices: dict[str, dict] = {}   # keyed by service name
        self._lock = threading.Lock()
        self._zc: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._interface_ip = interface_ip  # bind mDNS to this IP ('' = all)
        self._requery_stop: Optional[threading.Event] = None
        self._requery_thread: Optional[threading.Thread] = None
        self._fast_browser: Optional[ServiceBrowser] = None

    def start(self):
        if self._interface_ip:
            self._zc = Zeroconf(interfaces=[self._interface_ip])
            _log.info('mDNS scanner bound to %s', self._interface_ip)
        else:
            self._zc = Zeroconf()
        self._browser = ServiceBrowser(
            self._zc, SERVICE_TYPE, handlers=[self._on_change])
        _log.info('mDNS scanner started for %s', SERVICE_TYPE)
        # Start periodic re-query thread to keep devices alive on Windows
        self._requery_stop = threading.Event()
        self._requery_thread = threading.Thread(
            target=self._requery_loop, daemon=True, name='mdns-requery')
        self._requery_thread.start()

    def stop(self):
        if self._requery_stop:
            self._requery_stop.set()
        self.stop_fast_discovery()
        if self._zc:
            self._zc.close()
            self._zc = None
            self._browser = None
            _log.info('mDNS scanner stopped')

    def _requery_loop(self):
        """Periodically re-query known services to refresh _seen timestamps.

        On Windows, zeroconf often misses passive mDNS announcements when bound
        to a specific interface.  This active polling keeps devices alive.
        """
        while not self._requery_stop.wait(_REQUERY_INTERVAL):
            with self._lock:
                names = list(self._devices.keys())
            if not names or not self._zc:
                continue
            for name in names:
                try:
                    info = self._zc.get_service_info(SERVICE_TYPE, name, timeout=3000)
                    if info:
                        # Trigger the same update path as the browser callback
                        self._on_change(
                            self._zc, SERVICE_TYPE, name,
                            ServiceStateChange.Updated)
                except Exception:
                    pass

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

    def get_device_by_mac(self, mac: str) -> Optional[dict]:
        """The freshest record for one MAC, plus how old it is.

        A sensor announces a different service record in bootloader mode than in
        application mode, and the one it just left lingers until its TTL expires
        — so picking the most recently seen record is what tells you which mode
        it is in right now.  Used by the firmware updater to follow a sensor
        through reset -> boot -> app.
        """
        want = (mac or '').lower()
        with self._lock:
            cands = [d for d in self._devices.values()
                     if (d.get('mac') or '').lower() == want]
            if not cands:
                return None
            best = max(cands, key=lambda d: d.get('_seen', 0))
            out = {k: v for k, v in best.items() if not k.startswith('_')}
            out['age_s'] = round(time.monotonic() - best.get('_seen', 0), 1)
            return out

    def requery_now(self):
        """Actively re-query every known service right now (blocking).

        The periodic re-query runs every 30 s, which is too slow to watch a
        sensor reboot.  Callers that are waiting on a mode change poke this.
        """
        if not self._zc:
            return
        with self._lock:
            names = list(self._devices.keys())
        for name in names:
            try:
                info = self._zc.get_service_info(SERVICE_TYPE, name, timeout=1500)
                if info:
                    self._on_change(self._zc, SERVICE_TYPE, name,
                                    ServiceStateChange.Updated)
            except Exception:
                pass

    def start_fast_discovery(self):
        """Add a second, short-lived ServiceBrowser.

        A browser queries aggressively when it starts (1 s, 2 s, 4 s …) and then
        backs off, so a long-running one is slow to notice a service *name* it
        has never seen — which is exactly what a sensor publishes when it drops
        into its bootloader.  The firmware updater starts one of these for the
        duration of a flash and cancels it afterwards.  Discoveries land in the
        same `_on_change`, so the device table is unaffected either way.
        """
        if self._zc is None or self._fast_browser is not None:
            return
        try:
            self._fast_browser = ServiceBrowser(
                self._zc, SERVICE_TYPE, handlers=[self._on_change])
            _log.debug('fast mDNS discovery started')
        except Exception as e:
            _log.warning('fast mDNS discovery failed to start: %s', e)
            self._fast_browser = None

    def stop_fast_discovery(self):
        browser, self._fast_browser = self._fast_browser, None
        if browser is not None:
            try:
                browser.cancel()
                _log.debug('fast mDNS discovery stopped')
            except Exception:
                pass

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
