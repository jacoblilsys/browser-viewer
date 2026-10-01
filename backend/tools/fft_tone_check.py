"""Hardware FFT validation against a known acoustic tone.

Requires a real sensor and a steady tone (default 432 Hz) played near it.
Sweeps every fft_size and both precisions, logs everything to one HDF5 file,
and checks each capture:

  duplicates   consecutive FFT payloads must never be byte-identical
               (firmware <= 0x1032 emitted every frame twice at fft_size 256)
  sequence     sequence_number contiguous, no gaps or resets
  frequency    the peak bin must land on the tone within one bin
  amplitude    tone amplitude must agree across every fft_size and both
               precisions, and with the same estimate computed from the raw
               time-series stream (this is what firmware 0x1033 promises:
               a tone of amplitude A reads A regardless of geometry)
  dc           bin 0 must agree with the raw stream's DC (gravity)

Amplitude is estimated as sqrt(sum(a_k^2 over the main lobe) / ENBW) with
Hann ENBW = 1.5 bins. That is immune to scalloping loss — a peak-bin reading
alone varies by up to 15 % purely with where the tone falls between bins,
which would otherwise look like a scaling error. It also needs no bin width,
so it is unaffected by the actual_frame_rate_hz defect at fft_size 256.

Usage:
    python -m tools.fft_tone_check --ip <sensor-ip> --mac <sensor-mac> \
        --password <app-password> --tone 432 --axis x
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
import message_pb2
from network_receiver import NetworkReceiver
from udp_receiver import UDPReceiver
from tools.hw_common import (ENBW, G, firmware_spectrum, port_in_use,
                             tone_amplitude)
SIZE_ENUM = {256: 'FFT_SIZE_256', 512: 'FFT_SIZE_512',
             1024: 'FFT_SIZE_1024', 2048: 'FFT_SIZE_2048'}
PREC_ENUM = {'float32': 'FFT_PRECISION_FLOAT32', 'q15': 'FFT_PRECISION_Q15'}

# tolerances
TOL_FREQ_BINS = 1.0             # peak within this many bins of the tone
TOL_AMP_REL = 0.20              # amplitude spread across combos / vs raw
TOL_DC_REL = 0.05               # bin 0 vs raw DC


class Capture:
    def __init__(self, args):
        self.a = args
        self.q: asyncio.Queue = asyncio.Queue()
        self.tcp = None
        self.udp = None

    async def start(self, loop):
        self.tcp = NetworkReceiver('0.0.0.0', self.a.port, self.q, loop)
        self.udp = UDPReceiver('0.0.0.0', self.a.port, self.q, loop)
        self.tcp.start()
        self.udp.start()

    def stop(self):
        if self.tcp:
            self.tcp.stop()
        if self.udp:
            self.udp.stop()

    async def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    async def grab(self, want, timeout, kind, fft_size=None):
        """Collect decoded frames of one kind ('raw' or 'fft')."""
        loop = asyncio.get_running_loop()
        out, seqs = [], []
        deadline = loop.time() + timeout
        while len(out) < want and loop.time() < deadline:
            try:
                item = await asyncio.wait_for(
                    self.q.get(), timeout=max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if kind == 'fft':
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
                out.append(f)
                seqs.append(h.sequence_number)
            else:
                f = dec.decode_any(item[0], item[1])
                if isinstance(f, dec.FrameData):
                    out.append(f)
                    seqs.append(f.seq)
        return out, seqs


async def run(args):
    loop = asyncio.get_running_loop()
    cap = Capture(args)
    await cap.start(loop)
    results = []
    col = f'accel_{args.axis}'

    info = await sensor_api.get_sensor_info(args.ip, args.mac, args.password)
    dec.note_firmware_version(args.mac, info['firmware_version'])
    before = await sensor_api.get_sensor_config(args.ip, args.mac, args.password)
    fw = info['firmware_version']
    print(f'# sensor {args.mac}  firmware 0x{fw:04x}  hw {info["hardware_version"]}  '
          f'core {info["temp_core"]:.1f} C')
    print(f'# config on entry: {before}')
    print(f'# tone {args.tone} Hz, expected mainly on {args.axis}; '
          f'scaling mode = {dec.fft_scaling_mode(args.mac)}')

    import h5py
    out_path = os.path.join(args.out, time.strftime('fft_tone_check_%Y%m%d_%H%M%S.h5'))
    os.makedirs(args.out, exist_ok=True)
    hf = h5py.File(out_path, 'w')
    hf.attrs['firmware_version'] = fw
    hf.attrs['tone_hz'] = args.tone
    hf.attrs['axis'] = args.axis
    hf.attrs['device_id'] = args.mac
    for k, v in before.items():
        hf.attrs[f'config_{k}'] = str(v)

    try:
        # ── raw time series: gives fs, the DC reference and the tone reference ──
        await sensor_api.stream_fft_stop(args.ip, args.mac, args.password)
        await sensor_api.stream_stop(args.ip, args.mac, args.password)
        await asyncio.sleep(0.6)
        await cap.drain()
        await sensor_api.stream_start(args.ip, args.mac, args.password)
        raw, _ = await cap.grab(args.raw_frames, 30.0, 'raw')
        await sensor_api.stream_stop(args.ip, args.mac, args.password)
        if not raw:
            print('FAIL: no raw frames — is the sensor streaming to this host?')
            return 1
        fs = raw[0].sample_rate_hz
        x = np.concatenate([f.columns[col] for f in raw]).astype(np.float64)
        raw_dc = {ax.split('_')[-1]: float(np.mean(np.concatenate(
            [f.columns[ax] for f in raw]))) for ax in raw[0].columns}
        g = hf.create_group('raw')
        g.attrs['sample_rate_hz'] = fs
        for ax in raw[0].columns:
            g.create_dataset(ax, data=np.concatenate(
                [f.columns[ax] for f in raw]).astype(np.float32),
                compression='gzip', compression_opts=4)
        print(f'\n# raw: {len(raw)} frames, {x.size} samples/axis, fs = {fs:.2f} Hz')
        print('# raw DC: ' + '  '.join(f'{k}={v:+.4f}' for k, v in raw_dc.items()))

        # reference tone amplitude from the raw stream, per N
        ref = {}
        for n in args.sizes:
            if x.size < n:
                continue
            spec = firmware_spectrum(x, n)
            df = fs / n
            kp = int(np.argmax(spec[1:])) + 1
            ref[n] = {'amp': tone_amplitude(spec, kp), 'peak_hz': kp * df, 'df': df}
            print(f'#   reference from raw @N={n:5d}: peak {ref[n]["peak_hz"]:7.1f} Hz  '
                  f'amp {ref[n]["amp"]:.4f} m/s2')

        # ── sweep ──────────────────────────────────────────────────────────
        print(f'\n{"combo":18s} {"df":>8s} {"peak_hz":>9s} {"amp":>9s} {"bin0":>8s} '
              f'{"dupes":>6s} {"seq":>5s}  verdict')
        for pname in args.precisions:
            for n in args.sizes:
                await sensor_api.set_sensor_config(
                    args.ip, args.mac, args.password,
                    fft_size=SIZE_ENUM[n], fft_precision=PREC_ENUM[pname])
                await asyncio.sleep(1.0)
                await cap.drain()
                await sensor_api.stream_fft_start(args.ip, args.mac, args.password)
                frames, seqs = await cap.grab(args.frames, 40.0, 'fft', fft_size=n)
                await sensor_api.stream_fft_stop(args.ip, args.mac, args.password)
                await asyncio.sleep(0.3)
                label = f'{pname}/{n}'
                if len(frames) < 4:
                    print(f'{label:18s} {"":>8s} {"":>9s} {"":>9s} {"":>8s} '
                          f'{"":>6s} {"":>5s}  FAIL no frames ({len(frames)})')
                    results.append({'combo': label, 'ok': False,
                                    'why': f'only {len(frames)} frames'})
                    continue

                df = fs / n                       # from the raw stream, not the header
                m = np.array([fr.magnitudes[args.axis] for fr in frames], dtype=np.float64)
                mean_spec = m.mean(axis=0)
                kp = int(np.argmax(mean_spec[1:])) + 1
                peak_hz = kp * df
                amps = [tone_amplitude(fr.magnitudes[args.axis], kp) for fr in frames]
                amp = float(np.median(amps))
                bin0 = float(np.mean(m[:, 0]))

                dupes = sum(1 for i in range(len(frames) - 1)
                            if np.array_equal(m[i], m[i + 1]))
                seq_ok = all(seqs[i + 1] - seqs[i] == 1 for i in range(len(seqs) - 1))
                freq_err_bins = abs(peak_hz - args.tone) / df
                dc_ref = raw_dc.get(args.axis, 0.0)
                dc_err = (abs(bin0 - abs(dc_ref)) / abs(dc_ref)) if dc_ref else float('nan')

                fails = []
                if dupes:
                    fails.append(f'{dupes} duplicate payload pairs')
                if not seq_ok:
                    fails.append('sequence gap')
                if freq_err_bins > TOL_FREQ_BINS:
                    fails.append(f'peak off by {freq_err_bins:.1f} bins')
                if n in ref and abs(amp - ref[n]['amp']) / ref[n]['amp'] > TOL_AMP_REL:
                    fails.append(f'amp {amp:.4f} vs raw ref {ref[n]["amp"]:.4f}')
                verdict = 'PASS' if not fails else 'FAIL ' + '; '.join(fails)
                print(f'{label:18s} {df:8.2f} {peak_hz:9.1f} {amp:9.4f} {bin0:8.4f} '
                      f'{dupes:6d} {str(seq_ok):>5s}  {verdict}')

                grp = hf.create_group(f'fft/{pname}_{n}')
                grp.attrs.update({'fft_size': n, 'precision': pname, 'delta_f': df,
                                  'peak_hz': peak_hz, 'tone_amplitude': amp,
                                  'bin0': bin0, 'duplicate_pairs': dupes,
                                  'seq_contiguous': seq_ok, 'frames': len(frames)})
                grp.create_dataset('freq_hz', data=(np.arange(n // 2) * df).astype(np.float32))
                for ax in frames[0].magnitudes:
                    grp.create_dataset(f'mag_{ax}', data=np.array(
                        [fr.magnitudes[ax] for fr in frames], dtype=np.float32),
                        compression='gzip', compression_opts=4)
                    grp.create_dataset(f'psd_{ax}', data=np.array(
                        [fr.psd[ax] for fr in frames], dtype=np.float32),
                        compression='gzip', compression_opts=4)
                grp.create_dataset('seq', data=np.array(seqs, dtype=np.int64))
                results.append({'combo': label, 'ok': not fails, 'amp': amp,
                                'peak_hz': peak_hz, 'bin0': bin0, 'dupes': dupes,
                                'why': '; '.join(fails)})
    finally:
        await sensor_api.set_sensor_config(
            args.ip, args.mac, args.password,
            fft_size=before['fft_size'], fft_precision=before['fft_precision'])
        cap.stop()
        hf.close()

    # ── cross-combo consistency: the whole point of the 0x1033 normalisation ──
    print()
    good = [r for r in results if r.get('ok') and 'amp' in r]
    if len(good) >= 2:
        a = np.array([r['amp'] for r in good])
        spread = (a.max() - a.min()) / a.mean()
        print(f'tone amplitude across {len(good)} combos: '
              f'{a.min():.4f} .. {a.max():.4f} m/s2  spread {spread*100:.1f} %'
              f'  [{"PASS" if spread <= TOL_AMP_REL else "FAIL"}]')
        b = np.array([r['bin0'] for r in good])
        print(f'bin 0 (DC) across combos: {b.min():.4f} .. {b.max():.4f} m/s2  '
              f'raw DC {abs(raw_dc.get(args.axis, 0.0)):.4f}')
    nfail = sum(1 for r in results if not r.get('ok'))
    print(f'\nlogged to {out_path}')
    print(f'{len(results) - nfail}/{len(results)} combinations passed')
    for r in results:
        if not r.get('ok'):
            print(f'  FAIL {r["combo"]}: {r["why"]}')
    print(f'# config restored to {before["fft_size"]} / {before["fft_precision"]}')
    return 1 if nfail else 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ip', required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--password', default='')
    p.add_argument('--port', type=int, default=int(os.environ.get('TCP_PORT', '8066')))
    p.add_argument('--tone', type=float, default=432.0, help='tone frequency in Hz')
    p.add_argument('--axis', default='x', choices=['x', 'y', 'z'])
    p.add_argument('--frames', type=int, default=24, help='FFT frames per combination')
    p.add_argument('--raw-frames', type=int, default=40, help='raw frames for the reference')
    p.add_argument('--sizes', type=int, nargs='+', default=[256, 512, 1024, 2048])
    p.add_argument('--precisions', nargs='+', default=['float32', 'q15'],
                   choices=['float32', 'q15'])
    p.add_argument('--out', default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'logs'))
    p.add_argument('--force', action='store_true',
                   help='run even if the sensor port is already held (the viewer '
                        'backend will stop receiving for the duration)')
    args = p.parse_args()
    if port_in_use(args.port) and not args.force:
        print(f'ERROR: port {args.port} is already in use — the viewer backend is '
              f'probably running.\n'
              f'This tool would take the sensor\'s stream away from it and also '
              f'change fft_size/precision\nunderneath it. Stop the backend first, '
              f'or pass --force if you accept that.')
        sys.exit(2)
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
