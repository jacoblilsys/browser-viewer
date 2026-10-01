"""Settings persistence across reboots, in application and bootloader mode.

Bench tool for the redundant-EEPROM firmware test plan (app 0x1039 and later,
bootloader 0x19/0x1A). It covers the parts of
that test plan that are safe to run on any unit, any number of times:

  readback   full config snapshot (network, sensor, info, factory info) to JSON
  compare    diff two snapshots, ignoring live values (temperatures, time, CPU)
  app        step 2 — change harmless settings in the app, reboot, read back;
             repeat so the writes alternate between EEPROM slots A and B
  bl         step 3 — reset, and inside the bootloader wait window send
             set_network_config with harmless NTP fields; boot the app and
             expect it to report them, after one more reboot too; then change
             them from the app, reboot, and confirm the bootloader's change
             does not come back
  all        readback -> app -> bl -> restore -> readback, compare with entry

Firmware flashing (steps 1, 4, 5) is done with the viewer's Update Firmware
dialog, and calibration (step 6) by hand; run `readback` around them.

Every run restores the NTP fields and FFT size it touched. The harmless
fields are ntp_offset_us and ntp_min_ms_error_to_update (both used by the
sensor's SNTP client only) and fft_size (app tests only).

The bootloader test needs the bootloader's wait window: turn OFF "Auto
fast-boot sensors in bootloader" in every open viewer tab first, or the
viewer boots the sensor out of the window before this tool can write.

Example:
    python tools/eeprom_persistence_check.py all --ip <sensor-ip> \\
        --mac <sensor-mac> --password <app-password> --factory-password <factory-password>
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sensor_api
from mdns_scanner import MDNSScanner

# Values that change on their own between two readbacks of an unchanged unit.
LIVE_KEYS = {'info.temp1', 'info.temp2', 'info.temp_core', 'info.utc_time',
             'info.cpu_usage', 'info.debug_str', 'info.error_bits'}

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'logs')


def hx(v):
    return f'0x{v:X}' if isinstance(v, int) else str(v)


class Bench:
    def __init__(self, args):
        self.ip, self.mac, self.pw = args.ip, args.mac, args.password
        self.factory_pw = args.factory_password
        self.boot_timeout = args.boot_timeout
        self.results = []
        self.t0 = time.monotonic()
        self.mdns = MDNSScanner(args.interface or '')
        self.mdns.start()

    def say(self, msg):
        print(f'[{time.monotonic() - self.t0:7.1f}s] {msg}', flush=True)

    def check(self, step, name, ok, detail=''):
        self.results.append({'step': step, 'check': name, 'ok': bool(ok),
                             'detail': detail})
        self.say(f'{"PASS" if ok else "FAIL"}  {step}: {name}'
                 + (f' — {detail}' if detail else ''))
        return ok

    # ── sensor access ──────────────────────────────────────────────────────
    async def call(self, fn, *a, retries=3, **kw):
        last = None
        for _ in range(retries):
            try:
                return await fn(self.ip, self.mac, self.pw, *a, **kw)
            except sensor_api.SensorAuthError:
                raise
            except Exception as e:                     # noqa: BLE001
                last = e
                await asyncio.sleep(0.5)
        raise last

    async def readback(self):
        snap = {'mac': self.mac, 'ip': self.ip,
                'time': datetime.now().isoformat(timespec='seconds')}
        snap['info'] = await self.call(sensor_api.get_sensor_info)
        snap['network'] = await self.call(sensor_api.get_network_config)
        snap['sensor'] = await self.call(sensor_api.get_sensor_config)
        if not self.factory_pw:
            snap['factory'] = {'skipped': 'no --factory-password given'}
        else:
            try:
                snap['factory'] = await get_factory_info(self.ip, self.mac, self.factory_pw)
            except Exception as e:                     # noqa: BLE001
                snap['factory'] = {'error': str(e)}
        snap['mdns'] = self.mdns.get_device_by_mac(self.mac)
        return snap

    def mode(self):
        d = self.mdns.get_device_by_mac(self.mac)
        if d and d.get('ip') and d['ip'] != self.ip:
            self.say(f'sensor now announces {d["ip"]} (was {self.ip})')
            self.ip = d['ip']
        return d and d.get('mode')

    async def wait_mode(self, want, timeout, since=None):
        """Wait for an mDNS announcement of `want` newer than `since`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d = self.mdns.get_device_by_mac(self.mac)
            fresh = d and (since is None or time.monotonic() - d['age_s'] > since)
            if fresh and d.get('mode') == want:
                self.mode()
                return True
            await asyncio.sleep(0.05)
        return False

    async def wait_app_ready(self, timeout):
        """Until Get Info answers with a non-zero application version."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                info = await sensor_api.get_sensor_info(self.ip, self.mac, self.pw)
                if info.get('firmware_version'):
                    return info
            except sensor_api.SensorAuthError:
                raise
            except Exception:                          # noqa: BLE001
                pass
            await asyncio.sleep(1.0)
        return None

    async def reset(self):
        t = time.monotonic()
        try:
            await sensor_api.reset(self.ip, self.mac, self.pw)
        except sensor_api.SensorAuthError:
            raise
        except Exception as e:                         # noqa: BLE001
            self.say(f'reset: no reply ({e}) — continuing, watching mDNS')
        return t

    async def boot_now(self):
        # Bootloader 0x17 jumps to the app without answering boot_now, so a
        # timeout here is normal; the app's announcement is the real answer.
        try:
            await sensor_api.boot_now(self.ip, self.mac, self.pw)
        except sensor_api.SensorAuthError:
            raise
        except Exception:                              # noqa: BLE001
            pass

    async def reboot(self, fast=True):
        """Reset, follow boot -> app over mDNS, return Get Info once the app runs."""
        t = await self.reset()
        if await self.wait_mode('boot', 20, since=t):
            self.say('bootloader announced')
            if fast:
                await self.boot_now()
        else:
            self.say('bootloader announcement not seen (continuing)')
        if not await self.wait_mode('app', self.boot_timeout, since=t):
            raise RuntimeError('application did not announce after reset')
        info = await self.wait_app_ready(30)
        if not info:
            raise RuntimeError('application announced but Get Info does not answer')
        self.say(f'app up {time.monotonic() - t:.1f}s after reset: '
                 f'fw {hx(info["firmware_version"])} bl {hx(info["bootloader_version"])}')
        return info

    async def ntp_fields(self):
        nc = await self.call(sensor_api.get_network_config)
        return nc['ntp_offset_us'], nc['ntp_min_ms_error_to_update']

    # ── step 2: app-mode changes persist, slots alternate ──────────────────
    async def test_app(self, cycles):
        entry_sc = await self.call(sensor_api.get_sensor_config)
        sizes = ['FFT_SIZE_512', 'FFT_SIZE_1024']
        for i in range(cycles):
            off, mn = 1000 + 111 * (i + 1), 20 + i
            size = sizes[(i + (entry_sc['fft_size'] == sizes[0])) % 2]
            await self.call(sensor_api.set_network_config, ntp_offset_us=off,
                            ntp_min_ms_error_to_update=mn)
            await self.call(sensor_api.set_sensor_config, fft_size=size)
            await self.reboot()
            got = await self.ntp_fields()
            got_size = (await self.call(sensor_api.get_sensor_config))['fft_size']
            self.check('app', f'cycle {i + 1}/{cycles} persists after reboot',
                       got == (off, mn) and got_size == size,
                       f'wrote ntp_offset_us={off} min_ms={mn} {size}; '
                       f'read {got[0]} {got[1]} {got_size}')
        await self.call(sensor_api.set_sensor_config, fft_size=entry_sc['fft_size'])

    # ── step 3: bootloader-mode change reaches the app ─────────────────────
    async def test_bl(self, bl_offset=12345, bl_min=77):
        before = await self.ntp_fields()
        if before == (bl_offset, bl_min):
            await self.call(sensor_api.set_network_config, ntp_offset_us=1,
                            ntp_min_ms_error_to_update=1)
        t = await self.reset()
        if not await self.wait_mode('boot', 20, since=t):
            return self.check('bl', 'bootloader window caught', False,
                              'no mode=boot announcement within 20 s')
        self.say('bootloader announced — writing network config in bootloader mode')
        bl = {}
        try:
            bl['info'] = await sensor_api.get_sensor_info(self.ip, self.mac, self.pw)
            self.say(f'  bootloader Get Info: fw {hx(bl["info"]["firmware_version"])} '
                     f'bl {hx(bl["info"]["bootloader_version"])}')
        except Exception as e:                         # noqa: BLE001
            bl['info_error'] = str(e)
        try:
            await sensor_api.set_network_config(
                self.ip, self.mac, self.pw, ntp_offset_us=bl_offset,
                ntp_min_ms_error_to_update=bl_min)
            bl['set'] = 'ok'
        except Exception as e:                         # noqa: BLE001
            bl['set'] = f'error: {e}'
        try:
            bl['network'] = await sensor_api.get_network_config(self.ip, self.mac, self.pw)
        except Exception as e:                         # noqa: BLE001
            bl['network_error'] = str(e)
        still_boot = self.mode() == 'boot'
        self.check('bl', 'set_network_config accepted in bootloader mode',
                   bl['set'] == 'ok' and still_boot,
                   bl['set'] + ('' if still_boot else
                                ' — sensor left the bootloader during the write '
                                '(auto fast-boot on in a viewer tab?)'))
        await self.boot_now()
        if not await self.wait_mode('app', self.boot_timeout, since=t):
            return self.check('bl', 'app starts after boot_now', False)
        await self.wait_app_ready(30)
        got = await self.ntp_fields()
        self.check('bl', 'app reports the bootloader-written values',
                   got == (bl_offset, bl_min), f'read {got[0]} {got[1]}')
        await self.reboot()
        got = await self.ntp_fields()
        self.check('bl', 'bootloader-written values persist after a reboot',
                   got == (bl_offset, bl_min), f'read {got[0]} {got[1]}')
        app_vals = (bl_offset + 1, bl_min + 1)
        await self.call(sensor_api.set_network_config, ntp_offset_us=app_vals[0],
                        ntp_min_ms_error_to_update=app_vals[1])
        await self.reboot()
        got = await self.ntp_fields()
        self.check('bl', 'later app change wins; bootloader change does not return',
                   got == app_vals, f'wrote {app_vals[0]} {app_vals[1]}; read {got[0]} {got[1]}')
        return bl


async def get_factory_info(target_ip, mac, password):
    """FactoryInfo (calibration offsets, serial). Needs the factory password."""
    pb = sensor_api._pb
    req = pb.Request(msg_version=1)
    req.get_factory_info.SetInParent()
    resp = sensor_api._check(await sensor_api._send_recv(
        target_ip, req.SerializeToString(), mac, password))
    f = resp.factory_info
    return {'cX': f.cX, 'cY': f.cY, 'cZ': f.cZ, 'serialNumber': f.serialNumber,
            'hardwareVersion': f.hardwareVersion, 'year_month_day': f.year_month_day}


def flatten(d, prefix=''):
    out = {}
    for k, v in d.items():
        key = f'{prefix}{k}'
        if isinstance(v, dict):
            out.update(flatten(v, key + '.'))
        else:
            out[key] = v
    return out


def diff(a, b, ignore=()):
    fa, fb = flatten({k: a[k] for k in ('info', 'network', 'sensor', 'factory') if k in a}), \
             flatten({k: b[k] for k in ('info', 'network', 'sensor', 'factory') if k in b})
    skip = LIVE_KEYS | set(ignore)
    return [(k, fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb))
            if k not in skip and fa.get(k) != fb.get(k)]


def save(obj, name):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, name)
    with open(path, 'w') as fh:
        json.dump(obj, fh, indent=2, default=str)
    return path


def print_snapshot(s):
    i = s['info']
    print(f'  MAC {s["mac"]}  IP {s["ip"]}  app {hx(i["firmware_version"])}  '
          f'bootloader {hx(i["bootloader_version"])}  hw {i["hardware_version"]}')
    for sect in ('network', 'sensor', 'factory'):
        print(f'  {sect}: ' + ', '.join(f'{k}={v}' for k, v in flatten(s[sect]).items()))


async def run(args):
    if args.cmd == 'compare':
        a, b = (json.load(open(p)) for p in args.files)
        d = diff(a, b)
        for k, x, y in d:
            print(f'  {k}: {x!r} -> {y!r}')
        print('identical (live values ignored)' if not d else f'{len(d)} differences')
        return 0 if not d else 1

    b = Bench(args)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    tag = args.mac.replace(':', '')[-6:]
    entry = None
    try:
        await asyncio.sleep(2)                 # let mDNS see the sensor once
        entry = await b.readback()
        p = save(entry, f'eeprom_{tag}_{stamp}_entry.json')
        print_snapshot(entry)
        b.say(f'entry snapshot: {p}')
        if args.cmd == 'readback':
            return 0
        if args.cmd == 'reboot':
            await b.reboot(fast=not args.slow)
            after = await b.readback()
            d = diff(entry, after)
            b.check('reboot', 'config equal after reboot', not d,
                    '; '.join(f'{k}: {x!r}->{y!r}' for k, x, y in d))
        try:
            if args.cmd in ('app', 'all'):
                await b.test_app(args.cycles)
            if args.cmd in ('bl', 'all'):
                bl = await b.test_bl()
                save(bl, f'eeprom_{tag}_{stamp}_bootloader_view.json')
        finally:
            if args.cmd in ('app', 'bl', 'all'):
                n = entry['network']
                await b.call(sensor_api.set_network_config,
                             ntp_offset_us=n['ntp_offset_us'] or 0,
                             ntp_min_ms_error_to_update=n['ntp_min_ms_error_to_update'] or 0)
                await b.call(sensor_api.set_sensor_config,
                             fft_size=entry['sensor']['fft_size'])
                b.say('NTP fields and FFT size restored to entry values')
        if args.cmd in ('app', 'bl', 'all'):
            await b.reboot()
            final = await b.readback()
            p = save(final, f'eeprom_{tag}_{stamp}_final.json')
            d = diff(entry, final)
            b.check('final', 'config equals entry snapshot after restore + reboot', not d,
                    '; '.join(f'{k}: {x!r}->{y!r}' for k, x, y in d))
            b.say(f'final snapshot: {p}')
    finally:
        b.mdns.stop()
        if b.results:
            save({'entry': entry, 'results': b.results},
                 f'eeprom_{tag}_{stamp}_results.json')
    bad = [r for r in b.results if not r['ok']]
    print(f'\n{len(b.results) - len(bad)}/{len(b.results)} checks passed')
    return 0 if not bad else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('cmd', choices=['readback', 'reboot', 'app', 'bl', 'all', 'compare'])
    p.add_argument('files', nargs='*', help='compare: two snapshot JSON files')
    p.add_argument('--ip')
    p.add_argument('--mac')
    p.add_argument('--password', default='')
    p.add_argument('--factory-password', default=os.environ.get('SENSOR_FACTORY_PASSWORD', ''),
                   help='factory HMAC password, for FactoryInfo readback '
                        '(default: $SENSOR_FACTORY_PASSWORD; without it FactoryInfo is skipped)')
    p.add_argument('--interface', default=os.environ.get('NETWORK_IF', ''),
                   help='local IP to bind mDNS to (default: NETWORK_IF or all)')
    p.add_argument('--cycles', type=int, default=4, help='app: reboot cycles')
    p.add_argument('--boot-timeout', type=float, default=90.0)
    p.add_argument('--slow', action='store_true',
                   help='reboot: let the bootloader window run out instead of boot_now')
    args = p.parse_args()
    if args.cmd == 'compare':
        if len(args.files) != 2:
            p.error('compare needs two snapshot files')
    elif not (args.ip and args.mac):
        p.error('--ip and --mac are required')
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
