"""
Firmware update over TFTP — client-push model.

This mirrors the flow proven by the Qt6 scanner's "Update FW" dialog:

  1. prepare     read the sensor's current firmware, stop every stream
  2. reboot      Command.reset_device — the sensor restarts into its bootloader
  3. bootloader  wait for the mDNS record to report mode=boot
  4. upload      the PC is the TFTP *client* and PUSHES the .sfb into the
                 bootloader's TFTP server (WRQ) — the bootloader never fetches
  5. restart     wait for the sensor to flash the image and come back as mode=app
  6. verify      re-read the firmware version and compare with the .sfb

Auto fast-boot must be OFF for the whole run: a boot_now command sent while the
sensor sits in the bootloader jumps it straight into the application and the
upload has nowhere to land. `is_active()` lets the /api/sensor/boot route
refuse boot_now while a job is running, and the UI disables the setting too.

Only one job runs at a time — flashing two sensors at once from one PC is not
something the bench does, and serialising it keeps the stream pause/resume
bookkeeping honest.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger('fwupdate')

_HERE = Path(__file__).resolve().parent

# Where firmware files are looked up. FIRMWARE_DIR (os.pathsep-separated) wins;
# otherwise try the production Firmwares tree this viewer ships next to, then a
# local firmware/ folder.
_DEFAULT_DIRS = [
    _HERE.parents[3] / 'Firmwares',     # <A2E-TRI Production>/Firmwares
    _HERE.parent / 'firmware',          # <browser_viewer>/firmware
]

# Firmware uploaded from the browser lands here (kept, not deleted, so a repeat
# flash of the same file doesn't need a second upload).
UPLOAD_DIR = _HERE / 'fw_uploads'

TFTP_PORT      = int(os.environ.get('TFTP_PORT', '69'))
# Bind the TFTP client socket to this local IP ('' = let the OS route). Only
# needed on hosts where the default route picks the wrong adapter.
TFTP_LOCAL_IP  = os.environ.get('TFTP_LOCAL_IP', '').strip()

BOOT_WAIT_S    = 25.0    # after reset, how long to wait for the bootloader
BOOT_SETTLE_S  = 1.0     # let the bootloader's TFTP server settle before WRQ
APP_WAIT_S     = 180.0   # after upload, how long to wait for the application

# Once the image is in, the bootloader still sits out its ~42 s timer before
# starting the application. boot_now skips that. It is only ever sent *after* the
# upload, and only once the bootloader has been answering for a moment — never
# during the window we need it to stay in.
BOOT_NOW_SETTLE_S = 5.0    # bootloader must have been replying this long first
BOOT_NOW_RETRY_S  = 15.0   # UDP: resend if it is still sitting there
BOOT_NOW_TRIES    = 3

# Firmware older than this ACKs Command.reset_device but never actually reboots
# (a firmware bug) — 0x102F included, confirmed on the bench. Those units can
# only be put into the bootloader by pulling their power, so the updater asks the
# operator to do it and waits. This list is just a shortcut: a reset that turns
# out to do nothing on any other version falls back to the same prompt.
RESET_MIN_VERSION  = 0x1030
POWER_CYCLE_WAIT_S = 300.0   # a human has to walk to the sensor and back

# Steps during which nothing has been written to the sensor yet, so the operator
# may still call the whole thing off.
CANCELLABLE_STEPS = ('prepare', 'reboot', 'bootloader')

_STEPS = [
    ('prepare',    'Prepare — read firmware version, stop streams'),
    ('reboot',     'Reboot sensor into bootloader'),
    ('bootloader', 'Wait for bootloader mode'),
    ('upload',     'Upload firmware over TFTP'),
    ('restart',    'Wait for sensor to flash and restart'),
    ('verify',     'Verify new firmware version'),
]


# ── firmware files ───────────────────────────────────────────────────────────

def version_from_name(name: str) -> Optional[int]:
    """Version encoded in a firmware file name (hex): APP_1033 / APP_0x18 -> int."""
    m = re.search(r'_APP_(?:0x)?([0-9A-Fa-f]+)\.(?:sfb|bin)$', name or '', re.I)
    return int(m.group(1), 16) if m else None


def has_bootloader(name: str) -> bool:
    """True for a combined SBSFU image (bootloader + application).

    Those are flashed over SWD/ST-Link, not pushed into a running bootloader —
    the updater lists them but warns before letting one through.
    """
    return bool(re.search(r'(^|[_-])(SBSFU|BL_0x)', name or '', re.I))


def _entry(path: Path, size: int, mtime: float, uploaded: bool) -> dict:
    v = version_from_name(path.name)
    return {
        'id':          str(path),
        'name':        path.name,
        'dir':         str(path.parent),
        'uploaded':    uploaded,
        'size':        size,
        'mtime':       mtime,
        'version':     v,
        'version_hex': f'0x{v:X}' if v is not None else None,
        'has_bootloader': has_bootloader(path.name),
    }


def firmware_dirs() -> list[Path]:
    env = os.environ.get('FIRMWARE_DIR', '').strip()
    dirs = [Path(p) for p in env.split(os.pathsep) if p.strip()] if env else list(_DEFAULT_DIRS)
    dirs.append(UPLOAD_DIR)
    out, seen = [], set()
    for d in dirs:
        try:
            r = d.resolve()
        except Exception:
            continue
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def list_firmware() -> list[dict]:
    """Every .sfb/.bin under the configured firmware directories, newest first."""
    files: list[dict] = []
    for d in firmware_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.rglob('*')):
            if not p.is_file() or p.suffix.lower() not in ('.sfb', '.bin'):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append(_entry(p.resolve(), st.st_size, st.st_mtime,
                                uploaded=(d == UPLOAD_DIR.resolve())))
    files.sort(key=lambda f: f['mtime'], reverse=True)
    return files


def resolve_file(file_id: str) -> Path:
    """Map a file id from the browser back to a real path, refusing anything
    outside the configured firmware directories (the id travels over HTTP)."""
    if not file_id:
        raise ValueError('No firmware file selected')
    p = Path(file_id)
    try:
        p = p.resolve(strict=True)
    except OSError:
        raise ValueError(f'Firmware file not found: {file_id}')
    if not p.is_file():
        raise ValueError(f'Not a file: {file_id}')
    for d in firmware_dirs():
        if p == d or d in p.parents:
            return p
    raise ValueError(f'Firmware file is outside the allowed directories: {p}')


def save_upload(name: str, data: bytes) -> dict:
    """Store a firmware file uploaded from the browser and return its entry."""
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', Path(name or 'firmware.sfb').name) or 'firmware.sfb'
    if Path(safe).suffix.lower() not in ('.sfb', '.bin'):
        raise ValueError('Firmware must be a .sfb or .bin file')
    if not data:
        raise ValueError('Uploaded firmware file is empty')
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / safe
    dest.write_bytes(data)
    return _entry(dest.resolve(), len(data), dest.stat().st_mtime, uploaded=True)


# ── the job ──────────────────────────────────────────────────────────────────

def _diagnose(detail: str, step: str) -> tuple[str, str]:
    """Turn a raw exception string into (cause, fix) for the operator."""
    low = (detail or '').lower()
    if 'invalid_hmac' in low or 'auth' in low:
        return ('The sensor rejected the command — wrong or missing password.',
                'Check the Password field for this sensor and try again.')
    if step == 'upload' and ('timeout' in low or 'timed-out' in low or 'no response' in low):
        return ('The sensor did not answer the TFTP upload.',
                'It is probably not in bootloader mode, or the bootloader window '
                'closed before the upload started. Reset the sensor, confirm the '
                'device list shows mode "boot", and run the update again. Check '
                'the LAN cable and PoE power.')
    if 'timeout' in low or 'timed-out' in low or 'no response' in low:
        return ('The sensor did not respond to the command.',
                'Check that the sensor IP is correct and reachable from this host '
                '(same subnet), and that the password is right.')
    if 'not found' in low or 'no such file' in low or 'permission' in low or 'access' in low:
        return ('The firmware file could not be read.',
                'Move the .sfb file into the Firmwares folder (or re-upload it) '
                'and select it again.')
    if 'refused' in low or 'unreachable' in low:
        return ('The sensor could not be reached over the network.',
                'Check the sensor IP is on the same subnet as this host, and that '
                'the LAN cable / PoE power is connected.')
    if step == 'restart':
        return ('The sensor never started an application after the upload.',
                'The firmware itself may have transferred fine — the sensor is just '
                'sitting in its bootloader (it reports firmware 0x0 there). Auto '
                'fast-boot is back on now, so it should be started shortly; otherwise '
                'power-cycle it and check the FW column. If it stays in "boot", the '
                'bootloader rejected the image — flash a known-good .sfb.')
    if step == 'verify':
        return ('The sensor restarted but is not running the expected version.',
                'The bootloader may have rejected the image (wrong signature or '
                'a bootloader-included build). Check the .sfb is an "Updates no '
                'bootloader" build for this hardware, then flash again.')
    return ('The firmware update failed.',
            'See the log below, then reset the sensor and try again.')


class OperatorCancelled(Exception):
    """The operator stopped the update before anything was sent to the sensor."""


class FwUpdateJob:
    def __init__(self, *, target_ip: str, mac: str, password: str, file_path: Path,
                 port: int, sensor_api, mdns, broadcaster):
        self.ip         = target_ip
        self.mac        = mac
        self.password   = password
        self.file       = file_path
        self.port       = port
        self.api        = sensor_api
        self.mdns       = mdns
        self.broadcaster = broadcaster

        self.size            = file_path.stat().st_size
        self.expected        = version_from_name(file_path.name)
        self.current_version: Optional[int] = None
        self.new_version:     Optional[int] = None

        self.steps = [{'id': i, 'label': l, 'state': 'pending', 'detail': ''}
                      for i, l in _STEPS]
        self.step        = _STEPS[0][0]
        self.phase       = 'running'          # running | done | failed
        self.ok: Optional[bool] = None
        self.error       = ''
        self.cause       = ''
        self.fix         = ''
        self.sent        = 0
        self.log: list[str] = []
        self.prompt      = ''             # what the operator has to do, right now
        self.cancelled   = False
        self.started_at  = time.time()
        self.finished_at: Optional[float] = None
        self._t0         = time.monotonic()
        self._stopped_stream = False
        self._cancel_requested = False

    # ── state / logging ──────────────────────────────────────────────────────

    def _say(self, msg: str):
        line = f'[{time.monotonic() - self._t0:6.1f}s] {msg}'
        self.log.append(line)
        _log.info('%s', msg)

    def _set_step(self, step_id: str, state: str, detail: str = '', label: str = ''):
        self.step = step_id
        for s in self.steps:
            if s['id'] == step_id:
                s['state'] = state
                if detail:
                    s['detail'] = detail
                if label:
                    s['label'] = label

    def request_cancel(self):
        self._cancel_requested = True

    def _check_cancelled(self):
        if self._cancel_requested:
            raise OperatorCancelled()

    def status(self) -> dict:
        pct = int(self.sent * 100 / self.size) if self.size else 0
        return {
            'active':      self.phase == 'running',
            'phase':       self.phase,
            'step':        self.step,
            'steps':       self.steps,
            'prompt':      self.prompt,
            'cancellable': self.phase == 'running' and self.step in CANCELLABLE_STEPS,
            'cancelled':   self.cancelled,
            'ok':          self.ok,
            'error':       self.error,
            'cause':       self.cause,
            'fix':         self.fix,
            'progress':    min(pct, 100),
            'bytes_sent':  self.sent,
            'bytes_total': self.size,
            'log':         self.log,
            'target':      {'mac': self.mac, 'ip': self.ip},
            'file':        self.file.name,
            'expected_version':     self.expected,
            'expected_version_hex': f'0x{self.expected:X}' if self.expected is not None else None,
            'current_version':      self.current_version,
            'new_version':          self.new_version,
            'new_version_hex':      f'0x{self.new_version:X}' if self.new_version is not None else None,
            'started_at':  self.started_at,
            'finished_at': self.finished_at,
            'elapsed_s':   round((self.finished_at or time.time()) - self.started_at, 1),
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    def _mdns_mode(self) -> tuple[str, str]:
        """(mode, ip) from the freshest mDNS record for this MAC."""
        try:
            d = self.mdns.get_device_by_mac(self.mac) or {}
        except Exception:
            return '', ''
        return (d.get('mode') or '').lower(), d.get('ip') or ''

    async def _nudge_mdns(self):
        """Force an active re-query — on Windows the passive announcement that
        follows a reboot is easy to miss."""
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.mdns.requery_now)
        except Exception:
            pass

    async def _probe(self) -> tuple[bool, Optional[int]]:
        """Ask the sensor what it is: (answered, application version).

        Both the application and the bootloader answer get_sensor_info; only the
        application reports a version, so `(True, None)` means "in the
        bootloader" and `(False, None)` means "not on the network at all".
        """
        try:
            info = await self.api.get_sensor_info(self.ip, self.mac, self.password)
            return True, (info.get('firmware_version') or None)
        except self.api.SensorAuthError:
            raise
        except Exception:
            return False, None

    async def _wait_for_bootloader(self, timeout: float,
                                   app_alive: bool = True) -> tuple[bool, bool]:
        """Wait until the sensor is sitting in its bootloader.

        Returns (in_bootloader, application_still_answering). The second value is
        what tells a reset that quietly did nothing apart from a sensor that has
        gone quiet for some other reason.

        Two signals, because neither is sufficient alone: a config reply with no
        firmware version is definitive but costs up to one 10 s timeout per
        attempt, while mDNS is instant but can hand back a record the resolver
        cached before the reboot. So mDNS only counts once the application has
        stopped answering. Probes run as a task so the mDNS check keeps ticking
        while one is in flight.
        """
        deadline = time.monotonic() + timeout
        probe: Optional[asyncio.Task] = None
        nudge = 0.0
        try:
            while time.monotonic() < deadline:
                self._check_cancelled()
                if probe is not None and probe.done():
                    answered, version = probe.result()
                    probe = None
                    if answered and version is None:
                        return True, False               # the bootloader replied
                    app_alive = answered and version is not None
                if probe is None:
                    probe = asyncio.create_task(self._probe())
                mode, ip = self._mdns_mode()
                if mode == 'boot' and not app_alive:
                    if ip and ip != self.ip:
                        self._say(f'sensor IP is {ip} in bootloader mode')
                        self.ip = ip
                    return True, False
                if time.monotonic() >= nudge:
                    nudge = time.monotonic() + 4.0
                    await self._nudge_mdns()
                await asyncio.sleep(0.5)
            return False, app_alive
        finally:
            if probe is not None and not probe.done():
                probe.cancel()

    async def _wait_for_power_cycle(self):
        """Old firmware cannot reboot itself — talk the operator through pulling
        the power, and pick the sensor up again when it lands in its bootloader."""
        deadline = time.monotonic() + POWER_CYCLE_WAIT_S
        self.prompt = ('Unplug the sensor now, then plug it straight back in.\n'
                       'This firmware cannot restart itself, so a power cycle is the '
                       'only way into the bootloader. The upload starts on its own — '
                       'nothing else to press.')
        self._say('⏏ waiting for the operator to power-cycle the sensor')

        # It has to go away first: while the application is still answering, any
        # "bootloader" signal would be about the state we are trying to leave.
        while time.monotonic() < deadline:
            self._check_cancelled()
            answered, version = await self._probe()
            if not answered:
                self._say('sensor stopped answering — power is off')
                break
            if version is None:
                self._say('the bootloader is answering — the sensor is already in it')
                self.prompt = ''
                return
            await asyncio.sleep(1.0)
        else:
            raise TimeoutError('the sensor was never powered down')

        self.prompt = ('Plug the sensor back in.\n'
                       'The upload starts as soon as it appears in its bootloader.')
        remaining = max(deadline - time.monotonic(), 30.0)
        found, _ = await self._wait_for_bootloader(remaining, app_alive=False)
        if not found:
            raise TimeoutError('the sensor never came back in its bootloader')
        self.prompt = ''

    def _upload_blocking(self):
        """TFTP WRQ push — the sensor's bootloader is the server, we are the client."""
        from tftp.TftpClient import TftpClient
        from tftp.TftpPacketTypes import TftpPacketDAT

        last_pct = [-1]

        def hook(pkt):
            # cycle() also feeds us the ACKs coming back; only DATs are progress.
            if not isinstance(pkt, TftpPacketDAT):
                return
            self.sent += len(pkt.data)
            pct = min(int(self.sent * 100 / self.size), 100) if self.size else 0
            if pct >= last_pct[0] + 10:
                last_pct[0] = pct - (pct % 10)
                self._say(f'   upload {pct}%  ({self.sent:,}/{self.size:,} bytes)')

        client = TftpClient(self.ip, self.port, localip=TFTP_LOCAL_IP)
        with open(self.file, 'rb') as f:
            client.upload(self.file.name, input=f, packethook=hook)

    # ── the run ──────────────────────────────────────────────────────────────

    async def run(self):
        try:
            await self._run()
        except OperatorCancelled:
            self.ok = False
            self.cancelled = True
            self.phase = 'failed'
            self.error = 'Cancelled by the operator'
            self.cause = 'The update was cancelled before any firmware was sent.'
            self.fix = ('Nothing was written to the sensor. It may have been left in '
                        'its bootloader — auto fast-boot is back on, so it should '
                        'start on its own; otherwise power-cycle it.')
            self._set_step(self.step, 'skip', 'cancelled')
            self._say('⏹ cancelled by the operator')
        except Exception as e:                       # noqa: BLE001 — reported to the UI
            detail = f'{type(e).__name__}: {e}' if str(e) else type(e).__name__
            self.ok = False
            self.phase = 'failed'
            self.error = detail
            self.cause, self.fix = _diagnose(detail, self.step)
            self._set_step(self.step, 'fail', detail)
            self._say(f'✖ {detail}')
        finally:
            self.prompt = ''
            await self._restore()
            self.finished_at = time.time()
            if self.phase == 'running':
                self.phase = 'done' if self.ok else 'failed'

    async def _run(self):
        api = self.api
        self._say(f'Firmware update — sensor {self.mac} ({self.ip})')
        self._say(f'file {self.file.name}  ({self.size:,} bytes)'
                  + (f'  → expects FW 0x{self.expected:X}' if self.expected is not None else ''))

        # ── 1. prepare ───────────────────────────────────────────────────────
        self._set_step('prepare', 'active')
        # Watch the sensor closely for the whole run: it publishes a different
        # mDNS record in the bootloader than in the application.
        try:
            self.mdns.start_fast_discovery()
        except Exception:
            pass
        if has_bootloader(self.file.name):
            self._say('⚠ this file name looks like a combined SBSFU image '
                      '(bootloader + application) — those are meant for SWD/ST-Link')
        mode, ip = self._mdns_mode()
        if ip and ip != self.ip:
            self._say(f'using IP {ip} from the current mDNS record')
            self.ip = ip

        # Is the application running? The bootloader answers get_sensor_info too
        # — it just reports firmware_version 0, because the application version
        # only exists once the application is running. So a *non-zero* version is
        # the dependable "not in the bootloader" signal; an mDNS record alone is
        # not, since it can be a stale one served from the resolver cache.
        running_app = False
        answered = False
        try:
            info = await api.get_sensor_info(self.ip, self.mac, self.password)
            answered = True
            self.current_version = info.get('firmware_version') or None
            running_app = self.current_version is not None
            if running_app:
                self._say(f'current firmware 0x{self.current_version:X} '
                          f'({self.current_version})')
            else:
                self._say('the sensor answered with firmware 0x0 — it is in its '
                          'bootloader, not running an application')
        except api.SensorAuthError:
            raise                                       # wrong password — stop now
        except Exception as e:
            self._say(f'⚠ the sensor did not answer get_sensor_info ({e})')
        already_boot = (not running_app) and (answered or mode == 'boot')
        if already_boot:
            self._say('sensor is already in the bootloader — skipping the reboot')

        # Nothing may be streaming while the flash is written. Only worth asking
        # an application that answered a moment ago — otherwise each command just
        # burns its 10 s timeout while the bootloader window ticks away.
        if running_app:
            self._say('stopping streams (sensor + viewer)')
            for name, call in (('FFT', api.stream_fft_stop), ('raw', api.stream_stop)):
                try:
                    await call(self.ip, self.mac, self.password)
                    self._stopped_stream = True
                except Exception as e:
                    self._say(f'⚠ could not stop the {name} stream ({e})')
        else:
            self._say('pausing the viewer stream')
        try:
            self.broadcaster.pause()
        except Exception:
            pass
        self._set_step('prepare', 'ok')

        # ── 2. reboot into the bootloader ────────────────────────────────────
        # Firmware older than 0x102F ACKs reset_device and then carries on
        # running, so for those the only way in is the operator pulling the plug.
        needs_power_cycle = (self.current_version is not None
                             and self.current_version < RESET_MIN_VERSION)
        self._set_step('reboot', 'active')
        if already_boot:
            self._set_step('reboot', 'skip', 'already in bootloader')
        elif needs_power_cycle:
            self._set_step(
                'reboot', 'active',
                detail='waiting for the operator',
                label=f'Power-cycle the sensor — firmware 0x{self.current_version:X} '
                      'cannot reset itself')
            self._say(f'firmware 0x{self.current_version:X} is older than '
                      f'0x{RESET_MIN_VERSION:X}: its reset command does not reboot '
                      'the sensor, so it has to be power-cycled by hand')
            await self._wait_for_power_cycle()
            self._set_step('reboot', 'ok', 'power-cycled')
        else:
            self._say('rebooting the sensor into its bootloader...')
            try:
                await api.reset(self.ip, self.mac, self.password)
                self._set_step('reboot', 'ok')
            except Exception as e:
                if running_app:
                    raise           # it was up a moment ago — this is a real failure
                # It never answered get_sensor_info either, so it is most likely
                # already sitting in a bootloader that mDNS hasn't shown us.
                self._say(f'⚠ reset was not acknowledged ({e}) — the sensor is not '
                          'answering as an application either, so trying the upload')
                self._set_step('reboot', 'skip', 'not acknowledged')

        # ── 3. wait for bootloader mode ──────────────────────────────────────
        self._set_step('bootloader', 'active')
        if already_boot or needs_power_cycle:
            # Both of those paths already established it — a power cycle only
            # returns from _wait_for_power_cycle() once the bootloader is up.
            self._say('✔ sensor is in its bootloader')
            self._set_step('bootloader', 'ok')
        else:
            found, app_alive = await self._wait_for_bootloader(
                BOOT_WAIT_S, app_alive=running_app)
            if found:
                self._say('✔ sensor is in its bootloader')
                self._set_step('bootloader', 'ok')
            elif app_alive:
                # The reset was acknowledged and the application is still running
                # — the firmware bug, on a version we did not have on the list.
                # Fall back to the manual route rather than uploading into a
                # sensor that is not listening for it.
                v = (f'0x{self.current_version:X}' if self.current_version is not None
                     else 'this firmware')
                self._say(f'⚠ the sensor accepted the reset but is still running the '
                          f'application — {v} cannot restart itself either')
                self._set_step('reboot', 'skip', 'reset had no effect',
                               label='Power-cycle the sensor — the reset had no effect')
                self._set_step('bootloader', 'active')
                await self._wait_for_power_cycle()
                self._say('✔ sensor is in its bootloader')
                self._set_step('bootloader', 'ok')
            else:
                # Gone quiet without confirming the bootloader. It only waits
                # ~42 s before starting the application, so don't burn the window
                # chasing confirmation — try the upload, like the Qt tool does.
                self._say('⚠ could not confirm bootloader mode — trying the upload anyway')
                self._set_step('bootloader', 'skip', 'not confirmed')
        await asyncio.sleep(BOOT_SETTLE_S)

        # ── 4. upload ────────────────────────────────────────────────────────
        # The upload is plain unicast, unlike the config commands (which also go
        # to multicast and are matched on MAC), so the address has to be right.
        # A power cycle can hand the sensor a different DHCP lease, so take the
        # address off the bootloader's own record if there is one.
        boot_mode, boot_ip = self._mdns_mode()
        if boot_mode == 'boot' and boot_ip and boot_ip != self.ip:
            self._say(f'bootloader announces {boot_ip} — uploading there')
            self.ip = boot_ip
        self._set_step('upload', 'active')
        self._say(f'⬆ pushing {self.file.name} to {self.ip}:{self.port}')
        self.sent = 0
        await asyncio.get_running_loop().run_in_executor(None, self._upload_blocking)
        self.sent = self.size
        self._say('✔ upload complete (100%)')
        self._set_step('upload', 'ok')

        # ── 5. wait for the sensor to flash and restart ──────────────────────
        # The application version is what we are waiting for, and only the
        # application knows it: the bootloader answers get_sensor_info as well
        # but reports 0x0. So poll until a *non-zero* version comes back — that
        # is proof the sensor installed the image and started the application.
        # mDNS is not proof; it can still be serving the record from before the
        # reset.
        self._set_step('restart', 'active')
        self._say('waiting for the sensor to install the image and restart '
                  '(the bootloader waits ~42 s before starting the application)')
        deadline = time.monotonic() + APP_WAIT_S
        version = None
        last_err = None
        boot_since = 0.0        # when the bootloader first answered after the upload
        boots_sent = 0
        next_boot = 0.0
        while time.monotonic() < deadline:
            mode, ip = self._mdns_mode()
            if ip and ip != self.ip:
                self._say(f'sensor IP is now {ip}')
                self.ip = ip
            try:
                info = await api.get_sensor_info(self.ip, self.mac, self.password)
                version = info.get('firmware_version') or None
                if version is not None:
                    break
                now = time.monotonic()
                if not boot_since:
                    boot_since = now
                    next_boot = now + BOOT_NOW_SETTLE_S
                    self._say('the bootloader is answering (firmware 0x0) — waiting '
                              'for it to start the new application')
                # Skip the bootloader's countdown rather than sitting through it.
                # Deliberately not gated on the UI's auto fast-boot setting: that
                # one is off precisely so nothing interrupts the flash, and the
                # flash is over. Held back until the bootloader has been replying
                # for a moment, so we cannot cut into the image install.
                elif boots_sent < BOOT_NOW_TRIES and now >= next_boot:
                    next_boot = now + BOOT_NOW_RETRY_S
                    boots_sent += 1
                    try:
                        await api.boot_now(self.ip, self.mac, self.password)
                        self._say('⏩ fast-boot sent — skipping the bootloader wait'
                                  + (f' (attempt {boots_sent})' if boots_sent > 1 else ''))
                    except Exception as e:
                        self._say(f'⚠ fast-boot was not accepted ({e}) — letting the '
                                  'bootloader time out on its own instead')
                        boots_sent = BOOT_NOW_TRIES
            except api.SensorAuthError:
                raise
            except Exception as e:
                last_err = e
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.mdns.requery_now)
            except Exception:
                pass
            await asyncio.sleep(2.0)
        if version is None:
            raise TimeoutError(
                f'the sensor did not report a running application within '
                f'{int(APP_WAIT_S)}s'
                + (f' — it stayed in its bootloader (firmware 0x0)' if said_boot
                   else f' ({last_err})'))
        self._say('✔ sensor is back in application mode')
        self._set_step('restart', 'ok')

        # ── 6. verify ────────────────────────────────────────────────────────
        self._set_step('verify', 'active')
        self.new_version = version
        self._say(f'sensor now reports firmware 0x{self.new_version:X} ({self.new_version})')
        if self.expected is not None and self.new_version != self.expected:
            self.ok = False
            self.phase = 'failed'
            self.error = (f'sensor reports 0x{self.new_version:X}, '
                          f'expected 0x{self.expected:X}')
            self.cause, self.fix = _diagnose('', 'verify')
            self._set_step('verify', 'fail', self.error)
            self._say(f'✖ {self.error}')
            return
        self.ok = True
        self.phase = 'done'
        self._set_step('verify', 'ok', f'0x{self.new_version:X}')
        self._say('✔ firmware update complete')

    async def _restore(self):
        """Put the sensor and the viewer back the way we found them. Best effort:
        the flash verdict is already decided, this must never raise."""
        try:
            self.mdns.stop_fast_discovery()
        except Exception:
            pass
        if self._stopped_stream:
            try:
                await self.api.stream_start(self.ip, self.mac, self.password)
                self._say('restarted the sensor data stream')
            except Exception as e:
                self._say(f'⚠ could not restart the sensor data stream ({e}) — '
                          'use Stream ▶ once the sensor is back')
        try:
            self.broadcaster.resume()
        except Exception:
            pass


# ── module-level single job ──────────────────────────────────────────────────

_job: Optional[FwUpdateJob] = None
_task: Optional[asyncio.Task] = None


def is_active() -> bool:
    return _job is not None and _job.phase == 'running'


def status() -> dict:
    if _job is None:
        return {'active': False, 'phase': 'idle', 'prompt': '', 'cancellable': False,
                'steps': [{'id': i, 'label': l, 'state': 'pending', 'detail': ''}
                          for i, l in _STEPS]}
    return _job.status()


def cancel() -> dict:
    """Stop a job that has not sent anything to the sensor yet.

    Refused once the upload starts: interrupting a flash write is how you get a
    sensor that boots into nothing.
    """
    if not is_active():
        raise RuntimeError('No firmware update is running')
    if _job.step not in CANCELLABLE_STEPS:
        raise RuntimeError(
            f'Too late to cancel — the update is at the "{_job.step}" step. '
            'Interrupting now could leave the sensor without a working firmware.')
    _job.request_cancel()
    return _job.status()


def start(*, target_ip: str, mac: str, password: str, file_id: str,
          port: int, sensor_api, mdns, broadcaster) -> dict:
    """Validate, then kick off the flash as a background task."""
    global _job, _task
    if is_active():
        raise RuntimeError('A firmware update is already running')
    path = resolve_file(file_id)
    _job = FwUpdateJob(target_ip=target_ip, mac=mac, password=password,
                       file_path=path, port=port, sensor_api=sensor_api,
                       mdns=mdns, broadcaster=broadcaster)
    _task = asyncio.create_task(_job.run())
    return _job.status()
