"""Noise floor vs decimation (odr_div), measured per axis on real hardware.

The firmware decimates with a non-overlapping boxcar: it sums R samples and
arithmetic-shifts by log2(R) (sensor_imu.c, boxcar3_push), R a power of two.

What that predicts, and what this tool checks:

  density   For white input noise, averaging R samples divides the variance by
            R and divides the bandwidth by R. Those cancel exactly, so the
            noise DENSITY in ug/sqrt(Hz) should stay CONSTANT with odr_div.
            Decimation does not buy a lower density — this is the result most
            people expect to go the other way.
  rms       Total in-band RMS noise SHOULD fall as 1/sqrt(R), because the
            band itself shrinks by R. That is the real benefit.
  droop     A boxcar is a sinc filter, so the response sags toward the new
            Nyquist (-3.9 dB at Nyquist for large R) while aliasing folds
            noise back in. The measurement band is therefore kept well below
            Nyquist by default.

Quantisation is worth knowing about but should not dominate: the boxcar output
is re-quantised to int16, so its noise density RISES as sqrt(R). At 2 g full
scale that is ~2.5 ug/sqrt(Hz) even at odr_div 256, against a sensor floor
around 75 ug/sqrt(Hz).

The sensor must be at rest and quiet. A tone playing nearby will alias into
the band at high odr_div (432 Hz folds to 17.6 Hz at odr_div 256), which is
why the floor is estimated with a median and the in-band peak is reported
alongside it — if peak/median is large, something is contaminating the run.

Usage:
    python -m tools.noise_floor_check --ip <sensor-ip> \
        --mac <sensor-mac> --password <app-password> --filter auto
"""
import argparse
import asyncio
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'protobuf'))

import numpy as np

import sensor_api
import protobuf_decoder as dec
from network_receiver import NetworkReceiver
from udp_receiver import UDPReceiver
from tools.hw_common import AUTO_CUTOFF, G, ODR_ENUM, port_in_use, welch



class Capture:
    def __init__(self, port):
        self.port = port
        self.q: asyncio.Queue = asyncio.Queue()
        self.tcp = self.udp = None

    async def start(self, loop):
        self.tcp = NetworkReceiver('0.0.0.0', self.port, self.q, loop)
        self.udp = UDPReceiver('0.0.0.0', self.port, self.q, loop)
        self.tcp.start(); self.udp.start()

    def stop(self):
        if self.tcp:
            self.tcp.stop()
        if self.udp:
            self.udp.stop()

    async def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    async def raw(self, want_samples, timeout):
        loop = asyncio.get_running_loop()
        frames = []
        got = 0
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
                got += len(next(iter(f.columns.values())))
        return frames


async def run(args):
    loop = asyncio.get_running_loop()
    cap = Capture(args.port)
    await cap.start(loop)

    info = await sensor_api.get_sensor_info(args.ip, args.mac, args.password)
    dec.note_firmware_version(args.mac, info['firmware_version'])
    before = await sensor_api.get_sensor_config(args.ip, args.mac, args.password)
    print(f'# sensor {args.mac}  firmware 0x{info["firmware_version"]:04x}  '
          f'full_scale {before["full_scale"]}  core {info["temp_core"]:.1f} C')
    print(f'# filter mode: {args.filter} (entry filter {before["filter"]})')
    print(f'# band for the floor estimate: {args.band_lo:.2f}-{args.band_hi:.2f} '
          f'x Nyquist, median over bins\n')

    import h5py
    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, time.strftime('noise_floor_%Y%m%d_%H%M%S.h5'))
    hf = h5py.File(out_path, 'w')
    hf.attrs['firmware_version'] = info['firmware_version']
    hf.attrs['full_scale'] = before['full_scale']
    hf.attrs['filter_mode'] = args.filter

    rows = []
    hdr = (f'{"odr_div":>7s} {"fs_Hz":>9s} {"nyq_Hz":>8s} {"n":>7s} '
           f'{"axis":>4s} {"floor_ug/rtHz":>13s} {"rms_ug":>9s} {"peak/med":>9s} '
           f'{"segs":>5s} {"+-%":>5s}')
    try:
        for R in args.odr:
            kwargs = {'odr_div': ODR_ENUM[R]}
            if args.filter == 'auto':
                kwargs['filter_enabled'] = 'FILTER_LOW_PASS2'
                kwargs['filter_cutoff'] = AUTO_CUTOFF[R]
            await sensor_api.stream_stop(args.ip, args.mac, args.password)
            await asyncio.sleep(0.4)
            await sensor_api.set_sensor_config(args.ip, args.mac, args.password, **kwargs)
            await asyncio.sleep(1.0)
            await cap.drain()
            await sensor_api.stream_start(args.ip, args.mac, args.password)

            fs_nom = 26667.0 / R
            # Keep the statistical quality comparable across settings: a
            # fixed capture time gives 256x fewer samples at odr_div 256, and
            # the resulting estimator scatter looks like a real trend.
            want = max(int(fs_nom * args.seconds), args.min_samples)
            frames = await cap.raw(want, timeout=args.seconds * 3 + 25)
            await sensor_api.stream_stop(args.ip, args.mac, args.password)
            await asyncio.sleep(0.3)
            if len(frames) < 2:
                print(f'{R:7d}  no data ({len(frames)} frames)')
                continue

            fs = frames[0].sample_rate_hz
            grp = hf.create_group(f'odr_{R}')
            grp.attrs['odr_div'] = R
            grp.attrs['sample_rate_hz'] = fs
            grp.attrs['filter_cutoff'] = kwargs.get('filter_cutoff', 'kept')
            print(hdr if R == args.odr[0] else '')
            for col in sorted(frames[0].columns):
                ax = col.split('_')[-1]
                x = np.concatenate([f.columns[col] for f in frames]).astype(np.float64)
                grp.create_dataset(col, data=x.astype(np.float32),
                                   compression='gzip', compression_opts=4)
                f_hz, P, nseg = welch(x, fs, nperseg=args.nperseg)
                if not len(f_hz):
                    continue
                nyq = fs / 2.0
                sel = (f_hz >= args.band_lo * nyq) & (f_hz <= args.band_hi * nyq)
                if sel.sum() < 4:
                    continue
                med = float(np.median(P[sel]))
                peak = float(np.max(P[sel]))
                floor_ug = math.sqrt(med) / G * 1e6
                rms_ug = float(np.std(x - x.mean())) / G * 1e6
                rows.append({'R': R, 'axis': ax, 'fs': fs, 'floor': floor_ug,
                             'rms': rms_ug, 'ratio': math.sqrt(peak / med) if med else 0,
                             'n': x.size})
                # 1-sigma on a Welch density estimate is ~1/(2*sqrt(nseg))
                # in amplitude terms; print it so a wide spread at high odr_div
                # is not mistaken for a physical effect.
                err_pct = 100.0 / (2.0 * math.sqrt(nseg)) if nseg else float('nan')
                print(f'{R:7d} {fs:9.2f} {nyq:8.1f} {x.size:7d} {ax:>4s} '
                      f'{floor_ug:13.1f} {rms_ug:9.1f} '
                      f'{math.sqrt(peak/med) if med else 0:9.1f} {nseg:5d} '
                      f'{err_pct:5.1f}')
                d = grp.create_dataset(f'psd_{ax}', data=P.astype(np.float64))
                d.attrs['floor_ug_rthz'] = floor_ug
                d.attrs['rms_ug'] = rms_ug
            if 'freq_hz' not in grp:
                grp.create_dataset('freq_hz', data=f_hz.astype(np.float64))
    finally:
        restore = {'odr_div': before['odr_div'],
                   'filter_enabled': before['filter']['filter_enabled'],
                   'filter_cutoff': before['filter']['filter_cutoff']}
        await sensor_api.set_sensor_config(args.ip, args.mac, args.password, **restore)
        cap.stop()
        hf.close()

    # ── verdict ────────────────────────────────────────────────────────────
    print('\n# density should be FLAT vs odr_div; RMS should fall as 1/sqrt(R)')
    print(f'{"axis":>4s} {"floor min..max ug/rtHz":>26s} {"spread":>8s} '
          f'{"rms(R=1)/rms(Rmax)":>19s} {"expected":>9s}')
    ok = True
    for ax in sorted({r['axis'] for r in rows}):
        a = [r for r in rows if r['axis'] == ax]
        fl = np.array([r['floor'] for r in a])
        spread = (fl.max() - fl.min()) / fl.mean() if fl.mean() else float('nan')
        r1 = next((r for r in a if r['R'] == min(args.odr)), None)
        rn = next((r for r in a if r['R'] == max(args.odr)), None)
        ratio = (r1['rms'] / rn['rms']) if (r1 and rn and rn['rms']) else float('nan')
        exp = math.sqrt(max(args.odr) / min(args.odr))
        print(f'{ax:>4s} {fl.min():11.1f} ..{fl.max():11.1f} {spread*100:7.1f}% '
              f'{ratio:19.2f} {exp:9.2f}')
        if spread > args.tol_flat:
            ok = False
    print(f'\nlogged to {out_path}')
    print('# NOTE: a large peak/med column means something narrowband was in the '
          'band\n#       (a tone, or an alias of one) and that run\'s floor is suspect.')
    print(f'# config restored: odr_div={before["odr_div"]}, filter={before["filter"]}')
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ip', required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--password', default='')
    p.add_argument('--port', type=int, default=int(os.environ.get('TCP_PORT', '8066')))
    p.add_argument('--odr', type=int, nargs='+',
                   default=[1, 2, 4, 8, 16, 32, 64, 128, 256], choices=list(ODR_ENUM))
    p.add_argument('--filter', choices=['keep', 'auto'], default='auto',
                   help="'auto' sets the LP2 cutoff recommended for each odr_div; "
                        "'keep' leaves the entry filter alone")
    p.add_argument('--seconds', type=float, default=10.0, help='capture per setting')
    p.add_argument('--nperseg', type=int, default=None, help='Welch segment length')
    p.add_argument('--min-samples', type=int, default=4096,
                   help='floor on samples per setting, so high odr_div runs still '
                        'get enough Welch segments to be comparable')
    p.add_argument('--band-lo', type=float, default=0.10,
                   help='floor band start, as a fraction of Nyquist')
    p.add_argument('--band-hi', type=float, default=0.40,
                   help='floor band end (kept below the boxcar sinc droop)')
    p.add_argument('--tol-flat', type=float, default=0.35,
                   help='max relative spread of the density across odr_div')
    p.add_argument('--out', default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'logs'))
    p.add_argument('--force', action='store_true')
    args = p.parse_args()
    args.odr = sorted(args.odr)
    if port_in_use(args.port) and not args.force:
        print(f'ERROR: port {args.port} is in use — stop the viewer backend first '
              f'(or pass --force).')
        sys.exit(2)
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
