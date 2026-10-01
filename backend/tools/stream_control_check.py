"""Stream control, TCP reconnect and UDP-FFT independence (firmware 0x103D+).

Bench tool for the firmware 0x103D stream-control / reconnect test plan.
It owns the sensor ports (stop the viewer backend first). It opens and closes
the raw TCP listener on purpose, and counts raw (TCP) and FFT (UDP) frames as
they arrive.

  t1  power-on defaults: FFT over UDP with no TCP listener; raw once one appears
  t2  runtime raw (Command.stream_data) — not saved; saved data_stream rules at boot
  t3  runtime FFT (Command.stream_fft) — not saved any more
  t4  saved data_stream / fft_stream apply immediately
  t5  host closes the raw connection while raw streams (x3, then accept-and-close)
  t6  host closes while raw is stopped, then raw restarted
  t8  FFT only, no TCP listener, longer than 10 minutes: no reset
  t9  FFTStream.duration_ns = fft_size / actual_frame_rate_hz

  t7  vanished host: block the sensor's TCP traffic (iptables, no FIN/RST) for
      40 s while raw is stopped; afterwards raw must come back on a NEW
      connection. Needs root:  sudo <venv>/bin/python tools/stream_control_check.py --tests t7 ...
The saved stream defaults are restored at the end.

Example:
    python tools/stream_control_check.py --ip <sensor-ip> \\
        --mac <sensor-mac> --password <app-password> --tests t1 t2 t3 t4 t5 t6 t9
"""
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hw_common import port_in_use                      # noqa: E402  (sets sys.path)
import message_pb2                                      # noqa: E402
import sensor_api                                       # noqa: E402
from network_receiver import NetworkReceiver            # noqa: E402
from udp_receiver import UDPReceiver                    # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
EN, DIS = 'FEATURE_ENABLED', 'FEATURE_DISABLED'


class Bench:
    def __init__(self, args):
        self.a = (args.ip, args.mac, args.password)
        self.port = args.port
        self.t0 = time.monotonic()
        self.events = []            # (mono, kind, frame_number, header)
        self.results = []
        self.tcp = None
        self.q: asyncio.Queue = asyncio.Queue()
        # Count accepted raw connections (the receiver logs each one).
        self.accepts = 0
        bench = self

        class _Count(logging.Handler):
            def emit(self, rec):
                if rec.getMessage().startswith('Sensor connected'):
                    bench.accepts += 1
        lg = logging.getLogger('tcp')
        lg.addHandler(_Count())
        lg.setLevel(logging.INFO)

    # ── plumbing ───────────────────────────────────────────────────────────
    def say(self, msg):
        print(f'[{time.monotonic() - self.t0:7.1f}s] {msg}', flush=True)

    def check(self, test, name, ok, detail=''):
        self.results.append({'test': test, 'check': name, 'ok': bool(ok), 'detail': detail})
        self.say(f'{"PASS" if ok else "FAIL"}  {test}: {name}' + (f' — {detail}' if detail else ''))

    async def start(self):
        loop = asyncio.get_running_loop()
        self.udp = UDPReceiver('0.0.0.0', self.port, self.q, loop)
        self.udp.start()
        self._consumer = asyncio.ensure_future(self._consume())

    async def _consume(self):
        while True:
            payload, recv_ns, transport = await self.q.get()
            h = message_pb2.Header()
            try:
                h.ParseFromString(payload)
            except Exception:                              # noqa: BLE001
                continue
            if h.HasField('fft_stream'):
                self.events.append((time.monotonic(), 'fft', h.fft_stream.frame_number, h))
            elif h.HasField('frame_stream'):
                self.events.append((time.monotonic(), 'raw', h.frame_stream.frame_number, None))

    def listener(self, on: bool):
        if on and self.tcp is None:
            self.tcp = NetworkReceiver('0.0.0.0', self.port, self.q, asyncio.get_running_loop())
            self.tcp.start()
        elif not on and self.tcp is not None:
            self.tcp.stop()
            self.tcp = None
            time.sleep(0.3)        # the receiver thread closes within ~0.1 s

    def count(self, kind, since, until=None):
        until = until or time.monotonic()
        return sum(1 for t, k, _, _ in self.events if k == kind and since <= t <= until)

    async def first(self, kind, since, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for t, k, _, _ in self.events:
                if k == kind and t >= since:
                    return t - since
            await asyncio.sleep(0.05)
        return None

    async def window(self, seconds, settle=1.0):
        """Frames per stream in a window starting `settle` s from now."""
        await asyncio.sleep(settle)
        t = time.monotonic()
        await asyncio.sleep(seconds)
        return self.count('raw', t), self.count('fft', t)

    async def cmd(self, fn, *args, **kw):
        for i in range(3):
            try:
                return await fn(*self.a, *args, **kw)
            except sensor_api.SensorAuthError:
                raise
            except Exception:                              # noqa: BLE001
                if i == 2:
                    raise
                await asyncio.sleep(1)

    async def saved(self):
        n = await self.cmd(sensor_api.get_network_config)
        return n['data_stream'], n['fft_stream']

    async def reboot(self):
        """Reset, fast-boot, return the monotonic time the app answered."""
        try:
            await sensor_api.reset(*self.a)
        except Exception:                                  # noqa: BLE001
            pass
        await asyncio.sleep(4)
        bn = asyncio.ensure_future(sensor_api.boot_now(*self.a))  # never answered
        bn.add_done_callback(lambda f: f.exception())
        for _ in range(90):
            try:
                if (await sensor_api.get_sensor_info(*self.a))['firmware_version']:
                    t = time.monotonic()
                    self.say('app up after reset')
                    return t
            except Exception:                              # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError('application did not come back after reset')

    # ── tests ──────────────────────────────────────────────────────────────
    async def t1(self):
        await self.cmd(sensor_api.set_network_config, data_stream=EN, fft_stream=EN)
        self.listener(False)
        up = await self.reboot()
        dt = await self.first('fft', up - 2, 15)
        self.check('t1', 'FFT over UDP arrives with no TCP listener',
                   dt is not None and dt < 8, f'first FFT {dt:.1f}s after app answered' if dt is not None else 'none in 15 s')
        self.check('t1', 'no raw without a listener', self.count('raw', up) == 0)
        t = time.monotonic()
        self.listener(True)
        dt = await self.first('raw', t, 40)
        self.check('t1', 'raw connects and streams once a listener appears (<= 30 s back-off)',
                   dt is not None and dt <= 32, f'{dt:.1f}s' if dt is not None else 'none in 40 s')

    async def _runtime(self, test, kind, start, stop, field):
        idx = 0 if kind == 'raw' else 1
        self.listener(True)
        await self.cmd(sensor_api.set_network_config, **{field: EN})
        await self.cmd(start)
        await asyncio.sleep(2)
        await self.cmd(stop)
        w = await self.window(4)
        sv = await self.saved()
        self.check(test, f'runtime stop stops {kind} at once', w[idx] == 0, f'{w[idx]} frames in 4 s')
        self.check(test, f'runtime stop is not saved ({field} still ENABLED)', sv[idx] == EN, sv[idx])
        up = await self.reboot()
        dt = await self.first(kind, up - 2, 35)
        self.check(test, f'after reset {kind} resumes from the saved default',
                   dt is not None, f'first frame {dt:.1f}s after app' if dt is not None else 'none in 35 s')
        await self.cmd(sensor_api.set_network_config, **{field: DIS})
        await asyncio.sleep(1)
        await self.cmd(start)
        w = await self.window(4)
        self.check(test, f'runtime start works with {field} saved DISABLED', w[idx] > 0, f'{w[idx]} frames in 4 s')
        up = await self.reboot()
        await asyncio.sleep(8)
        n = self.count(kind, up + 2)
        self.check(test, f'after reset {kind} stays off (runtime start not saved)', n == 0, f'{n} frames in ~8 s')
        await self.cmd(sensor_api.set_network_config, **{field: EN})

    async def t2(self):
        await self._runtime('t2', 'raw', sensor_api.stream_start, sensor_api.stream_stop, 'data_stream')

    async def t3(self):
        await self._runtime('t3', 'fft', sensor_api.stream_fft_start, sensor_api.stream_fft_stop, 'fft_stream')

    async def t4(self):
        self.listener(True)
        for field, idx, kind in (('data_stream', 0, 'raw'), ('fft_stream', 1, 'fft')):
            await self.cmd(sensor_api.set_network_config, **{field: DIS})
            off = (await self.window(4))[idx]
            await self.cmd(sensor_api.set_network_config, **{field: EN})
            on = (await self.window(4, settle=3))[idx]
            self.check('t4', f'saved {field} stops/starts {kind} without reset',
                       off == 0 and on > 0, f'off: {off}, on: {on} frames in 4 s')

    async def t5(self, repeats=3):
        self.listener(True)
        await self.cmd(sensor_api.set_network_config, data_stream=EN, fft_stream=EN)
        await self.cmd(sensor_api.stream_start)
        await self.cmd(sensor_api.stream_fft_start)
        await asyncio.sleep(3)
        for i in range(repeats + 1):
            accept_close = i == repeats
            if accept_close:
                # accept the sensor's connection and close it at once
                self.listener(False)
                import socket
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('0.0.0.0', self.port))
                s.listen(1)
                s.settimeout(35)

                def accept_close():
                    try:
                        c, _ = s.accept()
                        c.close()
                        return True
                    except socket.timeout:
                        return False
                    finally:
                        s.close()
                ok = await asyncio.get_running_loop().run_in_executor(None, accept_close)
                self.say('accepted the raw connection and closed it at once' if ok
                         else 'accept-and-close: sensor did not connect within 35 s')
            else:
                self.listener(False)
            t = time.monotonic()
            await asyncio.sleep(8)
            fft = self.count('fft', t + 1)
            label = 'accept-and-close' if accept_close else f'close {i + 1}/{repeats}'
            self.check('t5', f'{label}: FFT over UDP continues while TCP is down',
                       fft > 0, f'{fft} FFT frames in 7 s')
            t = time.monotonic()
            self.listener(True)
            dt = await self.first('raw', t, 40)
            self.check('t5', f'{label}: raw reconnects without a reset (2–30 s)',
                       dt is not None and dt <= 32, f'{dt:.1f}s' if dt is not None else 'none in 40 s')
            await asyncio.sleep(2)

    async def t6(self):
        self.listener(True)
        await self.cmd(sensor_api.stream_start)
        await asyncio.sleep(3)
        await self.cmd(sensor_api.stream_stop)
        await asyncio.sleep(1)
        self.listener(False)
        await asyncio.sleep(5)
        self.listener(True)
        t = time.monotonic()
        await self.cmd(sensor_api.stream_start)
        dt = await self.first('raw', t, 40)
        self.check('t6', 'host closed while raw stopped: restart reconnects within a few s',
                   dt is not None and dt <= 10, f'{dt:.1f}s' if dt is not None else 'none in 40 s')

    async def t7(self, hold_s=40):
        if os.geteuid() != 0:
            return self.check('t7', 'vanished host', False, 'needs root (iptables) — run with sudo')
        ip = self.a[0]
        rules = [['INPUT', '-s', ip, '-p', 'tcp', '--dport', str(self.port), '-j', 'DROP'],
                 ['OUTPUT', '-d', ip, '-p', 'tcp', '--sport', str(self.port), '-j', 'DROP']]
        self.listener(True)
        await self.cmd(sensor_api.stream_start)
        if await self.first('raw', time.monotonic() - 1, 35) is None:
            return self.check('t7', 'raw connected before the test', False)
        await self.cmd(sensor_api.stream_stop)
        await asyncio.sleep(2)
        before = self.accepts
        self.say(f'blocking TCP {ip} <-> :{self.port} for {hold_s} s (no FIN/RST reaches either side)')
        for r in rules:
            subprocess.run(['iptables', '-I'] + r, check=True)
        try:
            await asyncio.sleep(hold_s)
        finally:
            for r in rules:
                subprocess.run(['iptables', '-D'] + r, check=False)
        self.say('rules removed; starting raw')
        t = time.monotonic()
        await self.cmd(sensor_api.stream_start)
        dt = await self.first('raw', t, 40)
        new = self.accepts - before
        self.check('t7', 'after a silent outage raw returns on a NEW connection (keepalive dropped the old one)',
                   dt is not None and new >= 1,
                   (f'raw after {dt:.1f}s' if dt is not None else 'no raw in 40 s')
                   + f', {new} new connection(s)')
        await self.cmd(sensor_api.stream_stop if (await self.saved())[0] != EN else sensor_api.stream_start)

    async def t8(self, minutes):
        self.listener(False)
        await self.cmd(sensor_api.stream_stop)
        await self.cmd(sensor_api.stream_fft_start)
        t = time.monotonic()
        self.say(f'FFT only, no TCP listener, for {minutes:.0f} min...')
        last_report = t
        while time.monotonic() - t < minutes * 60:
            await asyncio.sleep(5)
            if time.monotonic() - last_report > 60:
                self.say(f'  {self.count("fft", t)} FFT frames so far')
                last_report = time.monotonic()
        fr = [n for tt, k, n, _ in self.events if k == 'fft' and tt >= t]
        steps = [b - a for a, b in zip(fr, fr[1:])]
        backwards = sum(1 for s in steps if s <= 0)
        gaps = [(b[0] - a[0]) for a, b in zip(
            [e for e in self.events if e[1] == 'fft' and e[0] >= t],
            [e for e in self.events if e[1] == 'fft' and e[0] >= t][1:])]
        self.check('t8', f'FFT only for {minutes:.0f} min with no TCP server: no reset',
                   len(fr) > 10 and backwards == 0 and max(gaps or [99]) < 5,
                   f'{len(fr)} frames, frame_number {fr[0] if fr else "-"}→{fr[-1] if fr else "-"}, '
                   f'{backwards} backward steps, longest gap {max(gaps or [0]):.1f}s')

    async def t9(self):
        await self.cmd(sensor_api.stream_fft_start)
        await asyncio.sleep(3)
        hs = [h for t, k, _, h in self.events if k == 'fft'][-10:]
        if not hs:
            return self.check('t9', 'duration_ns present', False, 'no FFT frames')
        bad = []
        for h in hs:
            f = h.fft_stream
            want = f.fft_bins * 2 / f.actual_frame_rate_hz * 1e9
            if not f.duration_ns or abs(f.duration_ns - want) > 1000:
                bad.append((f.duration_ns, round(want)))
        f = hs[-1].fft_stream
        self.check('t9', 'duration_ns = fft_size / actual_frame_rate_hz', not bad,
                   f'duration_ns {f.duration_ns} ({f.duration_ns / 1e6:.2f} ms) for fft_size '
                   f'{f.fft_bins * 2} at {f.actual_frame_rate_hz:.2f} Hz'
                   + (f'; mismatches {bad[:3]}' if bad else ''))


async def run(args):
    b = Bench(args)
    await b.start()
    entry = await b.saved()
    sc = await b.cmd(sensor_api.get_sensor_config)
    info = await b.cmd(sensor_api.get_sensor_info)
    b.say(f'fw 0x{info["firmware_version"]:X} bl 0x{info["bootloader_version"]:X}; '
          f'saved data_stream={entry[0]} fft_stream={entry[1]}; {sc["odr_div"]} {sc["fft_size"]}')
    try:
        for t in args.tests:
            b.say(f'── {t} ──')
            if t == 't8':
                await b.t8(args.t8_minutes)
            elif t == 't5':
                await b.t5(args.repeats)
            else:
                await getattr(b, t)()
    finally:
        b.listener(True)
        try:
            await b.cmd(sensor_api.set_network_config, data_stream=entry[0], fft_stream=entry[1])
            await b.cmd(sensor_api.stream_start if entry[0] == EN else sensor_api.stream_stop)
            await b.cmd(sensor_api.stream_fft_start if entry[1] == EN else sensor_api.stream_fft_stop)
            b.say(f'restored saved data_stream={entry[0]} fft_stream={entry[1]}')
        except Exception as e:                              # noqa: BLE001
            b.say(f'WARNING: could not restore stream defaults: {e}')
        b.listener(False)
        b.udp.stop()
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f'streamctl_{datetime.now():%Y%m%d_%H%M%S}.json')
        with open(path, 'w') as fh:
            json.dump({'firmware': info['firmware_version'], 'results': b.results}, fh, indent=2)
    bad = [r for r in b.results if not r['ok']]
    print(f'\n{len(b.results) - len(bad)}/{len(b.results)} checks passed  ({path})')
    return 0 if not bad else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ip', required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--password', default='')
    p.add_argument('--port', type=int, default=int(os.environ.get('TCP_PORT', '8066')))
    p.add_argument('--tests', nargs='+', default=['t1', 't2', 't3', 't4', 't5', 't6', 't9'],
                   choices=['t1', 't2', 't3', 't4', 't5', 't6', 't7', 't8', 't9'])
    p.add_argument('--repeats', type=int, default=3, help='t5: plain host closes')
    p.add_argument('--t8-minutes', type=float, default=11)
    p.add_argument('--force', action='store_true')
    args = p.parse_args()
    if port_in_use(args.port) and not args.force:
        sys.exit(f'port {args.port} is in use — stop the viewer backend first')
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
