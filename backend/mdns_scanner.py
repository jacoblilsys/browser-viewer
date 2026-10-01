"""
mDNS sensor discovery using zeroconf.

Runs a persistent ServiceBrowser for '_nw-config._udp.local.' and maintains
a live dict of discovered sensors.  Follows the same TXT-record parsing as
Utils/Console/commands/cmd_scan.py.

Mode changes are picked up passively.  The bootloader and the application
register the *same* service name (vs<NNNN>._nw-config._udp.local.) and differ
only in TXT (mode=boot / mode=app, fw_bl / fw_app), and both call
mdns_resp_announce() as they start: three probes 250 ms apart (queries whose
authority section already carries the new A/SRV/TXT) and then one announcement.
Two passive paths read those, so the device table changes the moment the
sensor speaks — no query, no waiting for the browser's TTL refresh (~50 s with
the firmware's 60 s TTL) or the 30 s re-query:

  * _AnnouncementListener — every record of every response zeroconf receives;
  * _ProbeSniffer — its own socket on 5353, reading probes as well, so a
    transition has four packets to be seen by instead of one.
"""

import logging
import socket
import threading
import time
from typing import Optional

from zeroconf import (DNSIncoming, DNSText, RecordUpdateListener,
                      ServiceBrowser, ServiceStateChange, Zeroconf)

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


def _parse_txt(raw: bytes) -> dict[bytes, bytes]:
    """Decode a TXT rdata blob (length-prefixed 'key=value' strings)."""
    out: dict[bytes, bytes] = {}
    i = 0
    while i < len(raw):
        n = raw[i]
        item = raw[i + 1:i + 1 + n]
        i += 1 + n
        key, _, val = item.partition(b'=')
        if key and key not in out:
            out[key] = val
    return out


_TXT_FIELDS = ('mode', 'hw', 'fw_app', 'fw_bl', 'vendor_id', 'sensor_id')


class _AnnouncementListener(RecordUpdateListener):
    """Feeds every received _nw-config TXT record straight into the scanner.

    Registered with no question, so zeroconf hands it all records from all
    incoming responses — announcements included — on its event loop thread.
    """

    def __init__(self, scanner: 'MDNSScanner'):
        self._scanner = scanner

    def async_update_records(self, zc, now, records) -> None:
        for update in records:
            rec = update.new
            if (isinstance(rec, DNSText)
                    and rec.name.lower().endswith(SERVICE_TYPE.lower())
                    and not rec.is_expired(now)):
                self._scanner._on_txt(zc, rec.name, rec.text)


class _ProbeSniffer:
    """Plain multicast listener on 5353 that never sends anything.

    zeroconf only feeds *responses* to its listeners; a sensor's probes are
    queries and are dropped.  Sharing the port with SO_REUSEADDR (multicast is
    delivered to every such socket) lets us read them too.  Best effort: if the
    port cannot be shared, discovery still works through zeroconf.
    """

    def __init__(self, scanner: 'MDNSScanner', interface_ip: str):
        self._scanner = scanner
        self._if_ip = interface_ip
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, 'SO_REUSEPORT'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(('', 5353))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton('224.0.0.251')
                         + socket.inet_aton(self._if_ip or '0.0.0.0'))
            s.settimeout(1.0)
        except OSError as e:
            _log.warning('mDNS probe sniffer unavailable (%s); relying on '
                         'zeroconf announcements only', e)
            return
        self._sock = s
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='mdns-sniffer')
        self._thread.start()

    def stop(self):
        sock, self._sock = self._sock, None
        if sock:
            sock.close()

    def _run(self):
        suffix = SERVICE_TYPE.lower()
        while self._sock is not None:
            try:
                data, src = self._sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = DNSIncoming(data, src)
                if not msg.valid:
                    continue
                # Probe records sit in the authority section, which DNSIncoming
                # exposes through answers() alongside the answer section.
                for rec in msg.answers():
                    if isinstance(rec, DNSText) and rec.name.lower().endswith(suffix):
                        self._scanner._on_txt(self._scanner._zc, rec.name,
                                              rec.text, src_ip=src[0])
            except Exception:
                _log.debug('unparsable mDNS packet from %s', src[0], exc_info=True)


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
        self._listener: Optional[_AnnouncementListener] = None
        self._sniffer: Optional[_ProbeSniffer] = None

    def start(self):
        if self._interface_ip:
            self._zc = Zeroconf(interfaces=[self._interface_ip])
            _log.info('mDNS scanner bound to %s', self._interface_ip)
        else:
            self._zc = Zeroconf()
        self._browser = ServiceBrowser(
            self._zc, SERVICE_TYPE, handlers=[self._on_change])
        _log.info('mDNS scanner started for %s', SERVICE_TYPE)
        self._listener = _AnnouncementListener(self)
        self._zc.add_listener(self._listener, None)
        self._sniffer = _ProbeSniffer(self, self._interface_ip)
        self._sniffer.start()
        # Start periodic re-query thread to keep devices alive on Windows
        self._requery_stop = threading.Event()
        self._requery_thread = threading.Thread(
            target=self._requery_loop, daemon=True, name='mdns-requery')
        self._requery_thread.start()

    def stop(self):
        if self._requery_stop:
            self._requery_stop.set()
        self.stop_fast_discovery()
        if self._sniffer:
            self._sniffer.stop()
            self._sniffer = None
        if self._zc:
            if self._listener:
                self._zc.remove_listener(self._listener)
                self._listener = None
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

    def _on_txt(self, zeroconf: Zeroconf, name: str, raw: bytes,
                src_ip: str = ''):
        """Apply a TXT record exactly as received (zeroconf loop or sniffer).

        Reads the record itself rather than going through get_service_info():
        with the cache-flush bit the previous mode's TXT stays in the cache for
        up to a second, and the address/port are unchanged across a reboot
        anyway.  A name not seen before is resolved fully in a worker thread
        (get_service_info blocks, which the event loop must not).
        """
        txt = _parse_txt(raw)
        if b'mac' not in txt:
            return
        with self._lock:
            entry = self._devices.get(name)
            if entry is not None:
                old_mode = entry.get('mode')
                entry['mac'] = _get_txt(txt, 'mac')
                for k in _TXT_FIELDS:
                    entry[k] = _get_txt(txt, k)
                entry['bl_seen'] = entry['fw_bl'] or entry.get('bl_seen', '')
                entry['_seen'] = entry['_txt_at'] = time.monotonic()
                if src_ip and entry.get('ip') != src_ip:
                    entry['ip'] = src_ip    # e.g. DHCP lease changed on reboot
        if entry is None:
            if zeroconf is not None:
                threading.Thread(
                    target=self._on_change, daemon=True, name='mdns-resolve',
                    args=(zeroconf, SERVICE_TYPE, name, ServiceStateChange.Added),
                ).start()
        elif old_mode != entry['mode']:
            _log.info('Sensor %s announced mode=%s', entry['mac'], entry['mode'])

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
            prev = self._devices.get(name)
            is_new = prev is None
            # Once a TXT has come off the wire (_on_txt), it is the newest
            # truth: every later response reaches _on_txt too.  A cache read
            # here can be older — the previous mode's TXT lingers ~1 s after a
            # cache-flush, or for its whole 60 s TTL if the sensor's single
            # announcement was lost and only its probes were seen — so it only
            # refreshes address, port and liveness.
            if prev and '_txt_at' in prev:
                for k in ('mac',) + _TXT_FIELDS:
                    entry[k] = prev[k]
                entry['_txt_at'] = prev['_txt_at']
            # Only the bootloader announces its version (fw_bl), and apps before
            # a 0x1A bootloader cannot report it (SensorInfo says 0) — so keep
            # the last one heard through the app's run.
            entry['bl_seen'] = entry['fw_bl'] or (prev or {}).get('bl_seen', '')
            self._devices[name] = entry

        if is_new:
            _log.info('Sensor discovered: %s  IP=%s  mode=%s', mac, ip, entry['mode'])
        else:
            _log.debug('Sensor updated: %s  IP=%s', mac, ip)
