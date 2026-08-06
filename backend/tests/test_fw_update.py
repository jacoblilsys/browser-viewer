"""
Synthetic tests for the TFTP firmware updater (fw_update).

No sensor and no TFTP server are needed: the sensor API, mDNS scanner and
broadcaster are replaced by stubs, and the blocking TFTP push is stubbed out, so
the whole prepare → reboot → bootloader → upload → restart → verify state machine
runs in-process. The waits are shortened to keep the suite fast.

Run standalone:
    /home/jacob/venv-browser-viewer/bin/python backend/tests/test_fw_update.py
(also discoverable by pytest as test_* functions).
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── path bootstrap: backend/ and backend/protobuf/ ──────────────────────────
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_PROTO = os.path.join(_BACKEND, 'protobuf')
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

import fw_update
import sensor_api


# ── firmware file naming / listing ───────────────────────────────────────────

def test_version_from_name():
    cases = {
        'A2E-TRI_APP_1033.sfb':          0x1033,
        'A2E-TRI_APP_0x1032.sfb':        0x1032,
        'something_APP_18.bin':          0x18,
        'A2E-TRI_SBSFU_BL_0x18_APP_1033.sfb': 0x1033,
        'A2E-TRI_1033.sfb':              None,   # no _APP_ marker
        'notes.txt':                     None,
        '':                              None,
    }
    for name, want in cases.items():
        got = fw_update.version_from_name(name)
        assert got == want, f'{name!r}: got {got!r}, want {want!r}'
    print('  ok  version parsed from the firmware file name (hex, _APP_ marker)')


def test_has_bootloader():
    """Combined bootloader+app images go over SWD, not into a running bootloader,
    so the updater has to be able to spot one by name and warn."""
    for name in ('A2E-TRI_SBSFU_APP_1033.sfb', 'x-sbsfu-app.sfb',
                 'A2E-TRI_BL_0x18_APP_1033.sfb'):
        assert fw_update.has_bootloader(name), f'{name} should be flagged'
    for name in ('A2E-TRI_APP_1033.sfb', 'updates_no_bootloader_APP_1033.sfb'):
        assert not fw_update.has_bootloader(name), f'{name} must not be flagged'
    print('  ok  combined SBSFU / bootloader images are recognised by name')


def test_list_and_resolve_firmware():
    tmp = Path(tempfile.mkdtemp())
    uploads = Path(tempfile.mkdtemp())
    orig_upload = fw_update.UPLOAD_DIR
    orig_env = os.environ.get('FIRMWARE_DIR')
    try:
        fw_update.UPLOAD_DIR = uploads
        os.environ['FIRMWARE_DIR'] = str(tmp)
        (tmp / 'sub').mkdir()
        (tmp / 'A2E-TRI_APP_1032.sfb').write_bytes(b'\x00' * 10)
        (tmp / 'sub' / 'A2E-TRI_APP_1033.sfb').write_bytes(b'\x00' * 20)
        (tmp / 'readme.txt').write_text('not firmware')
        os.utime(tmp / 'A2E-TRI_APP_1032.sfb', (1000, 1000))
        os.utime(tmp / 'sub' / 'A2E-TRI_APP_1033.sfb', (2000, 2000))

        files = fw_update.list_firmware()
        names = [f['name'] for f in files]
        assert names == ['A2E-TRI_APP_1033.sfb', 'A2E-TRI_APP_1032.sfb'], \
            f'expected newest-first .sfb only, got {names}'
        assert files[0]['version'] == 0x1033 and files[0]['version_hex'] == '0x1033'
        assert files[0]['size'] == 20
        assert all(f['uploaded'] is False for f in files)

        # An id from the browser must resolve back only inside the allowed dirs.
        assert fw_update.resolve_file(files[0]['id']).name == 'A2E-TRI_APP_1033.sfb'
        outside = Path(tempfile.mkdtemp()) / 'evil.sfb'
        outside.write_bytes(b'x')
        for bad, why in ((str(outside), 'outside the firmware dirs'),
                         (str(tmp / 'nope.sfb'), 'missing file'),
                         ('', 'empty id')):
            try:
                fw_update.resolve_file(bad)
                assert False, f'resolve_file accepted {why}: {bad}'
            except ValueError:
                pass
    finally:
        fw_update.UPLOAD_DIR = orig_upload
        if orig_env is None:
            os.environ.pop('FIRMWARE_DIR', None)
        else:
            os.environ['FIRMWARE_DIR'] = orig_env
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(uploads, ignore_errors=True)
    print('  ok  firmware listing (newest first, .sfb/.bin only) and id resolution')


def test_save_upload():
    uploads = Path(tempfile.mkdtemp())
    orig = fw_update.UPLOAD_DIR
    try:
        fw_update.UPLOAD_DIR = uploads
        entry = fw_update.save_upload('A2E-TRI_APP_1033.sfb', b'\x01\x02\x03')
        assert entry['uploaded'] is True and entry['size'] == 3
        assert entry['version'] == 0x1033
        assert (uploads / 'A2E-TRI_APP_1033.sfb').read_bytes() == b'\x01\x02\x03'

        # A name from the browser must not be able to escape the upload folder.
        entry = fw_update.save_upload('../../etc/evil_APP_1033.sfb', b'\x01')
        assert Path(entry['id']).parent == uploads.resolve(), \
            f"upload escaped its folder: {entry['id']}"

        for name, data, why in (('firmware.exe', b'\x01', 'wrong extension'),
                                ('firmware.sfb', b'', 'empty body')):
            try:
                fw_update.save_upload(name, data)
                assert False, f'save_upload accepted {why}'
            except ValueError:
                pass
    finally:
        fw_update.UPLOAD_DIR = orig
        shutil.rmtree(uploads, ignore_errors=True)
    print('  ok  browser upload stored, name sanitised, bad files rejected')


# ── the job state machine ────────────────────────────────────────────────────

class _FakeApi:
    """Stands in for the sensor_api module.

    `before` and `after` are what get_sensor_info reports before and after the
    upload — one entry per call, the last entry repeating. Splitting on the
    upload rather than counting calls keeps the tests independent of how many
    times the updater happens to probe (it races a probe task against mDNS).
    None = the bootloader answering (firmware 0x0), 'down' = no answer at all.
    """

    SensorAuthError = sensor_api.SensorAuthError

    def __init__(self, before, after=None):
        self.before = list(before)
        self.after = list(after if after is not None else before[-1:])
        self.uploaded = False
        self.calls = []

    async def get_sensor_info(self, ip, mac, password):
        seq = self.after if self.uploaded else self.before
        v = seq[0] if len(seq) == 1 else seq.pop(0)
        self.calls.append(('info', v))
        if v == 'down':
            raise TimeoutError('no response')
        return {'firmware_version': 0 if v is None else v}

    async def _noop(self, ip, mac, password, _name=''):
        self.calls.append((_name, ip))

    async def reset(self, ip, mac, password):        await self._noop(ip, mac, password, 'reset')
    async def boot_now(self, ip, mac, password):     await self._noop(ip, mac, password, 'boot_now')
    async def stream_start(self, ip, mac, password): await self._noop(ip, mac, password, 'stream_start')
    async def stream_stop(self, ip, mac, password):  await self._noop(ip, mac, password, 'stream_stop')
    async def stream_fft_stop(self, ip, mac, password):
        await self._noop(ip, mac, password, 'stream_fft_stop')


class _FakeMdns:
    def __init__(self, mode='boot', ip='192.168.0.133'):
        self.mode, self.ip = mode, ip
        self.fast = 0

    def get_device_by_mac(self, mac):
        return {'mode': self.mode, 'ip': self.ip}

    def requery_now(self):
        pass

    def start_fast_discovery(self):
        self.fast += 1

    def stop_fast_discovery(self):
        self.fast -= 1


class _FakeBroadcaster:
    def __init__(self):
        self.paused = 0

    def pause(self):
        self.paused += 1

    def resume(self):
        self.paused -= 1


def _make_job(api, mdns=None, bc=None, name='A2E-TRI_APP_1033.sfb'):
    tmp = Path(tempfile.mkdtemp())
    f = tmp / name
    f.write_bytes(b'\xAA' * 2048)
    job = fw_update.FwUpdateJob(
        target_ip='192.168.0.133', mac='02:a0:12:68:4e:06', password='',
        file_path=f, port=6969, sensor_api=api,
        mdns=mdns or _FakeMdns(), broadcaster=bc or _FakeBroadcaster())

    def _fake_push():                            # no real TFTP push
        api.uploaded = True                      # the sensor now has the image
    job._upload_blocking = _fake_push
    return job, tmp


def _fast_waits():
    """Shrink every wait so the state machine runs in well under a second."""
    saved = {k: getattr(fw_update, k) for k in
             ('BOOT_SETTLE_S', 'BOOT_WAIT_S', 'APP_WAIT_S',
              'BOOT_NOW_SETTLE_S', 'BOOT_NOW_RETRY_S', 'POWER_CYCLE_WAIT_S')}
    fw_update.BOOT_SETTLE_S = 0.0
    fw_update.BOOT_WAIT_S = 0.6
    fw_update.APP_WAIT_S = 0.6
    fw_update.BOOT_NOW_SETTLE_S = 0.0
    fw_update.BOOT_NOW_RETRY_S = 0.1
    fw_update.POWER_CYCLE_WAIT_S = 0.6
    return saved


def _restore_waits(saved):
    for k, v in saved.items():
        setattr(fw_update, k, v)


def test_job_happy_path():
    """0x1032 → flash → 0x1033: every step ok, verify passes, streams restored."""
    saved = _fast_waits()
    api = _FakeApi(before=[0x1032,   # prepare: the application is up
                           None],    # after the reset: the bootloader answers
                   after=[0x1033])   # once flashed: the new application
    bc = _FakeBroadcaster()
    mdns = _FakeMdns()
    job, tmp = _make_job(api, mdns, bc)
    try:
        asyncio.run(job.run())
        st = job.status()
        assert st['ok'] is True, f"job failed: {st['error']}\n" + '\n'.join(st['log'])
        assert st['phase'] == 'done'
        assert st['new_version'] == 0x1033 and st['expected_version'] == 0x1033
        assert st['current_version'] == 0x1032
        assert st['progress'] == 100 and st['bytes_sent'] == st['bytes_total']
        assert [s['state'] for s in st['steps']] == ['ok'] * 6, \
            [(s['id'], s['state']) for s in st['steps']]
        # Streams were stopped for the flash, so they must be started again, and
        # the viewer's pause must be lifted however the job ended.
        names = [c[0] for c in api.calls]
        assert 'stream_stop' in names and 'stream_fft_stop' in names
        assert 'stream_start' in names, 'the sensor stream was never restarted'
        assert bc.paused == 0, 'broadcaster left paused'
        assert mdns.fast == 0, 'fast mDNS discovery left running'
        assert fw_update.is_active() is False
    finally:
        _restore_waits(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  happy path: reboot → upload → restart → verify, state restored')


def test_job_restart_timeout_reports_bootloader():
    """If the sensor never leaves its bootloader after the upload, the operator
    must get the "stayed in its bootloader" timeout and the restart-step advice —
    not an internal error from building the message."""
    saved = _fast_waits()
    api = _FakeApi(before=[0x1032, None], after=[None])   # never leaves the bootloader
    job, tmp = _make_job(api)
    try:
        asyncio.run(job.run())
        st = job.status()
        assert st['ok'] is False and st['phase'] == 'failed'
        assert 'TimeoutError' in st['error'], f"wrong error type: {st['error']}"
        assert 'bootloader' in st['error'], f"unhelpful error: {st['error']}"
        assert 'NameError' not in st['error'], f'internal error leaked: {st["error"]}'
        assert st['step'] == 'restart'
        assert 'never started an application' in st['cause']
        # The upload itself did complete, so it must not be blamed.
        by_id = {s['id']: s for s in st['steps']}
        assert by_id['upload']['state'] == 'ok'
        assert by_id['restart']['state'] == 'fail'
    finally:
        _restore_waits(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  restart timeout reports the stuck bootloader, not an internal error')


def test_job_version_mismatch_fails_verify():
    """A sensor that comes back on a different version than the .sfb promises is
    a failure — silently accepting it would report a flash that did not happen."""
    saved = _fast_waits()
    api = _FakeApi(before=[0x1032, None], after=[0x1031])  # back on the OLD version
    job, tmp = _make_job(api)
    try:
        asyncio.run(job.run())
        st = job.status()
        assert st['ok'] is False and st['step'] == 'verify'
        assert '0x1031' in st['error'] and '0x1032' not in st['error']
        assert 'expected 0x1033' in st['error'], st['error']
    finally:
        _restore_waits(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  verify fails when the running version is not the flashed one')


def test_job_skips_reboot_when_already_in_bootloader():
    """A sensor already sitting in its bootloader must not be reset again — the
    reset would restart the bootloader's window, or start the application."""
    saved = _fast_waits()
    api = _FakeApi(before=[None],    # prepare: the bootloader answers (firmware 0x0)
                   after=[0x1033])
    job, tmp = _make_job(api)
    try:
        asyncio.run(job.run())
        st = job.status()
        assert st['ok'] is True, f"job failed: {st['error']}"
        by_id = {s['id']: s for s in st['steps']}
        assert by_id['reboot']['state'] == 'skip', by_id['reboot']
        assert 'reset' not in [c[0] for c in api.calls], 'reset must not be sent'
        # Nothing was streaming, so nothing should be restarted either.
        assert 'stream_start' not in [c[0] for c in api.calls]
    finally:
        _restore_waits(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  a sensor already in its bootloader is not reset again')


def test_job_old_firmware_waits_for_power_cycle():
    """Firmware < 0x1030 ACKs reset_device and keeps running, so the updater must
    ask for a power cycle instead of uploading into a live application."""
    saved = _fast_waits()
    # 0x102F, then it goes away (power off), then the bootloader, then the new app.
    api = _FakeApi(before=[0x102F,    # prepare: old application is up
                           'down',    # operator pulled the power
                           None],     # back up, in the bootloader
                   after=[0x1033])
    job, tmp = _make_job(api)
    try:
        asyncio.run(job.run())
        st = job.status()
        assert st['ok'] is True, f"job failed: {st['error']}\n" + '\n'.join(st['log'])
        assert 'reset' not in [c[0] for c in api.calls], \
            'a reset must not be sent to firmware that cannot honour it'
        by_id = {s['id']: s for s in st['steps']}
        assert 'Power-cycle' in by_id['reboot']['label'], by_id['reboot']['label']
        assert st['prompt'] == '', 'the operator prompt must be cleared when done'
        assert any('power-cycle' in line.lower() for line in st['log'])
    finally:
        _restore_waits(saved)
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  pre-0x1030 firmware is power-cycled by the operator, never reset')


def test_cancel_guards():
    """Cancelling is fine until the firmware starts going over the wire; after
    that, interrupting is how a sensor ends up with no working image."""
    api = _FakeApi([0x1032])
    job, tmp = _make_job(api)
    orig_job = fw_update._job
    try:
        # No job at all.
        fw_update._job = None
        try:
            fw_update.cancel()
            assert False, 'cancel() must refuse when nothing is running'
        except RuntimeError:
            pass

        fw_update._job = job
        job.phase = 'running'
        for step in fw_update.CANCELLABLE_STEPS:
            job.step = step
            job._cancel_requested = False
            assert job.status()['cancellable'] is True, step
            fw_update.cancel()
            assert job._cancel_requested is True, f'cancel ignored at {step}'

        for step in ('upload', 'restart', 'verify'):
            job.step = step
            assert job.status()['cancellable'] is False, step
            try:
                fw_update.cancel()
                assert False, f'cancel() must refuse at the {step} step'
            except RuntimeError as e:
                assert 'Too late' in str(e), str(e)
    finally:
        fw_update._job = orig_job
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  cancel allowed before the first write, refused once flashing')


def test_boot_now_blocked_during_update():
    """boot_now would jump the sensor out of the bootloader mid-update, so the
    route must refuse it no matter which browser tab asks."""
    import main
    from fastapi import HTTPException

    api = _FakeApi([0x1032])
    job, tmp = _make_job(api)
    orig_job = fw_update._job
    try:
        fw_update._job = job
        job.phase = 'running'
        job.step = 'bootloader'
        assert fw_update.is_active() is True
        try:
            asyncio.run(main.api_boot_now(main.SensorTarget(
                target_ip='192.168.0.133', mac='02:a0:12:68:4e:06', password='')))
            assert False, 'boot_now must be refused while an update is running'
        except HTTPException as e:
            assert e.status_code == 409, e.status_code
            assert 'firmware update' in e.detail.lower(), e.detail

        # Once the job is over the route is available again.
        job.phase = 'done'
        assert fw_update.is_active() is False
    finally:
        fw_update._job = orig_job
        shutil.rmtree(tmp, ignore_errors=True)
    print('  ok  /api/sensor/boot refuses fast-boot while a flash is in progress')


def test_status_idle_shape():
    """The UI polls status before anything runs; it must get the step list and
    safe defaults rather than a half-built object."""
    orig = fw_update._job
    try:
        fw_update._job = None
        st = fw_update.status()
        assert st['active'] is False and st['phase'] == 'idle'
        assert st['cancellable'] is False and st['prompt'] == ''
        assert [s['id'] for s in st['steps']] == [i for i, _ in fw_update._STEPS]
        assert all(s['state'] == 'pending' for s in st['steps'])
    finally:
        fw_update._job = orig
    print('  ok  idle status carries the full step list with safe defaults')


# ── standalone runner ───────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    print(f'Running {len(tests)} tests...\n')
    for t in tests:
        t()
    print(f'\nAll {len(tests)} tests passed.')


if __name__ == '__main__':
    _run_all()
