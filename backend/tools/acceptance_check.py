"""Firmware acceptance suite — run against a real sensor after a FW release.

Checks, each selectable with --only / --skip:

  info        firmware / hardware / bootloader versions, optionally asserted
  config      every SensorConfig field round-trips through set -> get
  axes        all AxisMask modes: column count, labels, and DC consistent with
              the 3-axis reference (orientation-independent, so the sensor can
              sit any way up); vector modes must equal the RSS of their axes
  odr         every odr_div: advertised rate, and the rate actually delivered
  fft         every fft_size: bin count, bin width, contiguous sequence
              numbers, no byte-identical consecutive payloads (the duplicate
              defect from firmware <= 0x1032), and actual_frame_rate_hz sane
  format      both precisions: wire dtype in MetaData, and the two precisions
              must agree on the same spectrum
  transport   TCP and UDP for both streams, verified by which socket the
              frames actually arrive on. Requires reboots, so it is slow.
  noise       per-axis noise density at a low and a high odr_div

Exit code is 0 only if every selected check passes.

The sensor must be at rest for `axes` and `noise`. A tone may be playing; the
tone-specific validation lives in tools/fft_tone_check.py.

Usage:
    python -m tools.acceptance_check --ip <sensor-ip> \
        --mac <sensor-mac> --password <app-password> --expect-fw 0x1037
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import sensor_api
import protobuf_decoder as dec
from payloads.meta_data_pb2 import NumpyType
from tools.hw_common import (AUTO_CUTOFF, AXIS_MODES, Capture, G, ODR_ENUM,
                             PREC_ENUM, SIZE_ENUM, port_in_use, wait_for_sensor,
                             welch)

results = []


def record(check, name, ok, detail=''):
    results.append({'check': check, 'name': name, 'ok': bool(ok), 'detail': detail})
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' — {detail}' if detail else ''),
          flush=True)   # unbuffered: this suite runs for minutes behind a pipe
    return ok


def skip(check, name, why):
    """Precondition not met — not a firmware failure, and must not gate the run."""
    results.append({'check': check, 'name': name, 'ok': None, 'detail': why})
    print(f'  [SKIP] {name} — {why}', flush=True)


def rel(a, b):
    return abs(a - b) / abs(b) if b else float('inf')


async def settle_after_reboot(ctx, tries=6, delay=3.0):
    """Answering get_sensor_info is not the same as being ready — the next
    command still times out for several seconds after a reboot."""
    for attempt in range(tries):
        await asyncio.sleep(delay)
        try:
            await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
            return
        except Exception:                             # noqa: BLE001
            if attempt == tries - 1:
                raise


async def restart_stream(ctx, fft=False):
    await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
    await sensor_api.stream_fft_stop(ctx.ip, ctx.mac, ctx.pw)
    await asyncio.sleep(0.4)
    await ctx.cap.drain()
    if fft:
        await sensor_api.stream_fft_start(ctx.ip, ctx.mac, ctx.pw)
    else:
        await sensor_api.stream_start(ctx.ip, ctx.mac, ctx.pw)


# ── checks ──────────────────────────────────────────────────────────────────

async def check_info(ctx):
    info = await sensor_api.get_sensor_info(ctx.ip, ctx.mac, ctx.pw)
    fw = info['firmware_version']
    print(f'  firmware 0x{fw:04x}  hw {info["hardware_version"]}  '
          f'bootloader 0x{info["bootloader_version"]:02x}  core {info["temp_core"]:.1f} C')
    record('info', 'firmware version readable', fw > 0, f'0x{fw:04x}')
    if ctx.args.expect_fw:
        want = int(ctx.args.expect_fw, 0)
        record('info', f'firmware is 0x{want:04x}', fw == want, f'got 0x{fw:04x}')
    record('info', 'no error bits', info['error_bits'] == 0, f'error_bits={info["error_bits"]}')
    record('info', 'temperatures plausible',
           0 < info['temp_core'] < 100, f'{info["temp_core"]:.1f} C')


async def check_config(ctx):
    """Every SensorConfig field must survive a set -> get round trip."""
    trials = [
        ('full_scale', ['ACCEL_FS_2G', 'ACCEL_FS_4G', 'ACCEL_FS_8G', 'ACCEL_FS_16G']),
        ('odr_div', ['ODR_DIV_1', 'ODR_DIV_4', 'ODR_DIV_64']),
        ('fft_size', ['FFT_SIZE_256', 'FFT_SIZE_2048']),
        ('fft_precision', ['FFT_PRECISION_Q15', 'FFT_PRECISION_FLOAT32']),
        ('dc_removal', ['DC_REMOVAL_OFF', 'DC_REMOVAL_1_HZ', 'DC_REMOVAL_10_HZ']),
        ('axes', ['AXIS_XYZ', 'AXIS_X', 'AXIS_XYZ_VECTOR']),
    ]
    for field, values in trials:
        for v in values:
            await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw, **{field: v})
            await asyncio.sleep(0.35)
            got = await sensor_api.get_sensor_config(ctx.ip, ctx.mac, ctx.pw)
            record('config', f'{field} = {v}', got[field] == v, f'read back {got[field]}')
    # filter is nested
    await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw,
                                       filter_enabled='FILTER_LOW_PASS2',
                                       filter_cutoff='CUTOFF_1p33_KHZ')
    await asyncio.sleep(0.35)
    got = await sensor_api.get_sensor_config(ctx.ip, ctx.mac, ctx.pw)
    record('config', 'filter = LOW_PASS2 / 1p33',
           got['filter']['filter_enabled'] == 'FILTER_LOW_PASS2'
           and got['filter']['filter_cutoff'] == 'CUTOFF_1p33_KHZ', str(got['filter']))
    await sensor_api.set_sensor_config(
        ctx.ip, ctx.mac, ctx.pw, axes='AXIS_XYZ',
        filter_enabled=ctx.entry['filter']['filter_enabled'],
        filter_cutoff=ctx.entry['filter']['filter_cutoff'],
        full_scale=ctx.entry['full_scale'], odr_div=ctx.entry['odr_div'],
        dc_removal=ctx.entry['dc_removal'])


async def check_axes(ctx):
    """All AxisMask modes. Orientation-independent: everything is compared
    against the 3-axis DC measured first, so the sensor can sit any way up."""
    await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw, axes='AXIS_XYZ',
                                       odr_div='ODR_DIV_8')
    await asyncio.sleep(0.8)
    await restart_stream(ctx)
    frames, _ = await ctx.cap.raw(4096, 25)
    await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
    if len(frames) < 2:
        return record('axes', 'reference capture', False, 'no data')
    ref = {c.split('_')[-1]: float(np.mean(np.concatenate([f.columns[c] for f in frames])))
           for c in frames[0].columns}
    print(f'  reference DC: ' + '  '.join(f'{k}={v:+.3f}' for k, v in ref.items()))
    record('axes', 'AXIS_XYZ gives 3 columns', len(ref) == 3, f'{sorted(ref)}')

    for mode, (ncols, axes, is_vec) in AXIS_MODES.items():
        if mode == 'AXIS_XYZ':
            continue
        await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw, axes=mode)
        await asyncio.sleep(0.8)
        await restart_stream(ctx)
        fr, _ = await ctx.cap.raw(2048, 25)
        await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
        if len(fr) < 2:
            record('axes', f'{mode} streams', False, 'no data')
            continue
        cols = list(fr[0].columns)
        if not record('axes', f'{mode} column count', len(cols) == ncols,
                      f'expected {ncols}, got {len(cols)}: {cols}'):
            continue
        vals = {c: float(np.mean(np.concatenate([f.columns[c] for f in fr])))
                for c in cols}
        if is_vec:
            want = math.sqrt(sum(ref[a] ** 2 for a in axes))
            got = abs(list(vals.values())[0])
            record('axes', f'{mode} magnitude == RSS of {"".join(axes)}',
                   rel(got, want) < ctx.args.tol_axis,
                   f'{got:.3f} vs {want:.3f} m/s2')
        else:
            ok = True
            for a in axes:
                match = [v for c, v in vals.items() if c.endswith(a)]
                if not match:
                    ok = False
                    break
                ok &= abs(match[0] - ref[a]) < ctx.args.tol_axis_abs
            record('axes', f'{mode} DC matches reference', ok,
                   ' '.join(f'{c}={v:+.3f}' for c, v in vals.items()))
    await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw, axes='AXIS_XYZ')


async def check_odr(ctx):
    """Advertised rate and the rate actually delivered, for every divider."""
    for R in ctx.args.odr:
        await sensor_api.set_sensor_config(
            ctx.ip, ctx.mac, ctx.pw, odr_div=ODR_ENUM[R],
            filter_enabled='FILTER_LOW_PASS2', filter_cutoff=AUTO_CUTOFF[R])
        await asyncio.sleep(1.0)
        await restart_stream(ctx)
        want = max(int(26667.0 / R * 2.0), 1024)
        frames, _ = await ctx.cap.raw(want, timeout=40)
        await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
        if len(frames) < 3:
            record('odr', f'odr_div {R}', False, f'{len(frames)} frames')
            continue
        nominal = 26667.0 / R
        advertised = frames[0].sample_rate_hz
        n = sum(len(next(iter(f.columns.values()))) for f in frames)
        # Cadence from the device's own block timestamps, skipping the first
        # frame: wall-clock over the whole capture is biased by whatever was
        # already in flight when the stream started, which at 4-frame captures
        # showed up as a spurious ~19 % over-read.
        ts = [f.timestamp_ns for f in frames]
        per = len(next(iter(frames[0].columns.values())))
        span_s = (ts[-1] - ts[0]) / 1e9
        delivered = (per * (len(frames) - 1) / span_s) if span_s > 0 else 0
        ok_adv = rel(advertised, nominal) < 0.02
        ok_del = rel(delivered, advertised) < 0.15
        record('odr', f'odr_div {R} advertised rate', ok_adv,
               f'{advertised:.1f} Hz vs nominal {nominal:.1f}')
        record('odr', f'odr_div {R} delivered rate', ok_del,
               f'{delivered:.1f} Hz from {len(frames)} block timestamps ({n} samples)')
    await sensor_api.set_sensor_config(
        ctx.ip, ctx.mac, ctx.pw, odr_div=ctx.entry['odr_div'],
        filter_enabled=ctx.entry['filter']['filter_enabled'],
        filter_cutoff=ctx.entry['filter']['filter_cutoff'])


async def _fs_reference(ctx):
    await restart_stream(ctx)
    frames, _ = await ctx.cap.raw(4096, 25)
    await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
    return frames[0].sample_rate_hz if frames else 26667.0


async def check_fft(ctx):
    """Geometry, sequence integrity and the duplicate-payload regression."""
    fs = await _fs_reference(ctx)
    print(f'  raw sample rate reference: {fs:.2f} Hz')
    for N in ctx.args.sizes:
        await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw,
                                           fft_size=SIZE_ENUM[N],
                                           fft_precision='FFT_PRECISION_FLOAT32')
        await asyncio.sleep(1.0)
        await restart_stream(ctx, fft=True)
        frames, headers, _ = await ctx.cap.fft(ctx.args.fft_frames, 40, fft_size=N)
        await sensor_api.stream_fft_stop(ctx.ip, ctx.mac, ctx.pw)
        if len(frames) < 4:
            record('fft', f'fft_size {N}', False, f'{len(frames)} frames')
            continue
        f0 = frames[0]
        record('fft', f'fft_size {N} bin count', f0.fft_bins == N // 2,
               f'{f0.fft_bins} bins')
        df = f0.freq_hz[1] - f0.freq_hz[0]
        record('fft', f'fft_size {N} bin width', rel(df, fs / N) < 0.05,
               f'{df:.3f} Hz vs fs/N {fs/N:.3f}')
        seqs = [h.sequence_number for h in headers]
        record('fft', f'fft_size {N} sequence contiguous',
               all(seqs[i + 1] - seqs[i] == 1 for i in range(len(seqs) - 1)),
               f'{len(seqs)} frames')
        m = np.array([fr.magnitudes[next(iter(fr.magnitudes))] for fr in frames])
        dupes = sum(1 for i in range(len(m) - 1) if np.array_equal(m[i], m[i + 1]))
        record('fft', f'fft_size {N} no duplicate payloads', dupes == 0,
               f'{dupes} identical consecutive pairs')
        adv = headers[0].fft_stream.actual_frame_rate_hz
        record('fft', f'fft_size {N} actual_frame_rate_hz sane',
               rel(adv, fs) < 0.05, f'{adv:.1f} Hz vs raw fs {fs:.1f}')


async def check_format(ctx):
    """Wire dtype per precision, and agreement between the two."""
    fs = await _fs_reference(ctx)
    N = ctx.args.sizes[-1]
    spectra = {}
    for pname, penum in PREC_ENUM.items():
        await sensor_api.set_sensor_config(ctx.ip, ctx.mac, ctx.pw,
                                           fft_size=SIZE_ENUM[N], fft_precision=penum)
        await asyncio.sleep(1.0)
        await restart_stream(ctx, fft=True)
        frames, headers, _ = await ctx.cap.fft(12, 40, fft_size=N)
        await sensor_api.stream_fft_stop(ctx.ip, ctx.mac, ctx.pw)
        if len(frames) < 4:
            record('format', f'{pname} streams', False, f'{len(frames)} frames')
            continue
        md = headers[0].fft_stream.meta_data[0]
        want = ((NumpyType.NUMPY_TYPE_FLOATING_POINT, 4) if pname == 'float32'
                else (NumpyType.NUMPY_TYPE_INTEGER, 2))
        record('format', f'{pname} wire dtype',
               (md.data_numpy_type, md.data_numpy_bytes) == want,
               f'type={md.data_numpy_type} bytes={md.data_numpy_bytes}')
        ax = next(iter(frames[0].magnitudes))
        spectra[pname] = np.median(
            np.array([fr.magnitudes[ax] for fr in frames]), axis=0)
    if len(spectra) == 2:
        a, b = spectra['float32'], spectra['q15']
        band = a > np.median(a) * 5          # compare where there is signal
        if band.sum() >= 3:
            diff = float(np.median(np.abs(a[band] - b[band]) / a[band]))
            record('format', 'float32 and q15 agree', diff < ctx.args.tol_format,
                   f'median relative difference {diff*100:.1f} % over {int(band.sum())} bins')
        else:
            skip('format', 'float32 and q15 agree',
                 'no bins above the noise floor — needs a tone or excitation')


async def check_transport(ctx):
    """TCP vs UDP for both streams, verified by the receiving socket.

    Transport changes only take effect after a reboot, so this is slow.
    """
    net = await sensor_api.get_network_config(ctx.ip, ctx.mac, ctx.pw)
    entry = {'sample_transport': net['sample_transport'],
             'fft_transport': net['fft_transport']}
    try:
        for want, toggle in (('tcp', 'FEATURE_DISABLED'), ('udp', 'FEATURE_ENABLED')):
            await sensor_api.set_network_config(
                ctx.ip, ctx.mac, ctx.pw,
                sample_transport=toggle, fft_transport=toggle)
            await asyncio.sleep(0.5)
            print(f'  rebooting for {want.upper()} …')
            try:
                await sensor_api.reset(ctx.ip, ctx.mac, ctx.pw)
            except Exception:
                pass                                  # reset may not answer
            await asyncio.sleep(6.0)
            _, ctx.ip = await wait_for_sensor(ctx.ip, ctx.mac, ctx.pw,
                                              timeout=ctx.args.boot_timeout)
            # Answering get_sensor_info is not the same as being ready: the next
            # command often times out for several seconds after a reboot.
            print(f'  sensor is up at {ctx.ip}, settling', flush=True)
            await settle_after_reboot(ctx)

            await restart_stream(ctx)
            frames, transports = await ctx.cap.raw(2048, 30)
            await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
            got = sorted(set(transports))
            record('transport', f'raw stream over {want.upper()}',
                   bool(frames) and got == [want], f'frames={len(frames)} via {got}')

            await restart_stream(ctx, fft=True)
            ff, _, ftr = await ctx.cap.fft(6, 30)
            await sensor_api.stream_fft_stop(ctx.ip, ctx.mac, ctx.pw)
            gotf = sorted(set(ftr))
            record('transport', f'fft stream over {want.upper()}',
                   bool(ff) and gotf == [want], f'frames={len(ff)} via {gotf}')
    finally:
        await sensor_api.set_network_config(ctx.ip, ctx.mac, ctx.pw, **entry)
        try:
            await sensor_api.reset(ctx.ip, ctx.mac, ctx.pw)
        except Exception:
            pass
        await asyncio.sleep(6.0)
        _, ctx.ip = await wait_for_sensor(ctx.ip, ctx.mac, ctx.pw,
                                          timeout=ctx.args.boot_timeout)
        await settle_after_reboot(ctx)
        print(f'  transports restored to {entry}; sensor at {ctx.ip}', flush=True)


async def check_noise(ctx):
    """Per-axis noise density. Decimation must not change the density: the
    boxcar divides variance and bandwidth by the same R."""
    floors = {}
    for R in ctx.args.noise_odr:
        await sensor_api.set_sensor_config(
            ctx.ip, ctx.mac, ctx.pw, axes='AXIS_XYZ', odr_div=ODR_ENUM[R],
            filter_enabled='FILTER_LOW_PASS2', filter_cutoff=AUTO_CUTOFF[R])
        await asyncio.sleep(1.0)
        await restart_stream(ctx)
        want = max(int(26667.0 / R * ctx.args.noise_seconds), ctx.args.min_samples)
        frames, _ = await ctx.cap.raw(want, timeout=ctx.args.noise_seconds * 3 + 30)
        await sensor_api.stream_stop(ctx.ip, ctx.mac, ctx.pw)
        if len(frames) < 3:
            record('noise', f'odr_div {R}', False, 'no data')
            continue
        fs = frames[0].sample_rate_hz
        for col in sorted(frames[0].columns):
            ax = col.split('_')[-1]
            x = np.concatenate([f.columns[col] for f in frames]).astype(np.float64)
            f_hz, P, nseg = welch(x, fs)
            nyq = fs / 2
            sel = (f_hz >= 0.10 * nyq) & (f_hz <= 0.40 * nyq)
            if sel.sum() < 4:
                continue
            ug = math.sqrt(float(np.median(P[sel]))) / G * 1e6
            floors.setdefault(ax, {})[R] = ug
            record('noise', f'odr_div {R} {ax} floor in range',
                   ctx.args.floor_lo <= ug <= ctx.args.floor_hi,
                   f'{ug:.1f} ug/rtHz ({nseg} segments)')
    for ax, by_r in floors.items():
        if len(by_r) >= 2:
            v = np.array(list(by_r.values()))
            spread = (v.max() - v.min()) / v.mean()
            record('noise', f'{ax} density flat across odr_div', spread < ctx.args.tol_flat,
                   f'{v.min():.1f}..{v.max():.1f} ug/rtHz, spread {spread*100:.1f} %')
    await sensor_api.set_sensor_config(
        ctx.ip, ctx.mac, ctx.pw, odr_div=ctx.entry['odr_div'],
        filter_enabled=ctx.entry['filter']['filter_enabled'],
        filter_cutoff=ctx.entry['filter']['filter_cutoff'])


CHECKS = {'info': check_info, 'config': check_config, 'axes': check_axes,
          'odr': check_odr, 'fft': check_fft, 'format': check_format,
          'transport': check_transport, 'noise': check_noise}


class Ctx:
    pass


async def run(args):
    ctx = Ctx()
    ctx.args, ctx.ip, ctx.mac, ctx.pw = args, args.ip, args.mac, args.password
    ctx.cap = Capture(args.port)
    await ctx.cap.start()
    info = await sensor_api.get_sensor_info(args.ip, args.mac, args.password)
    dec.note_firmware_version(args.mac, info['firmware_version'])
    ctx.entry = await sensor_api.get_sensor_config(args.ip, args.mac, args.password)
    print(f'# entry config: {ctx.entry}\n')

    selected = [n for n in CHECKS if n in args.only] if args.only else list(CHECKS)
    selected = [n for n in selected if n not in args.skip]
    t0 = time.monotonic()
    try:
        for name in selected:
            print(f'\n== {name} ==', flush=True)
            try:
                await CHECKS[name](ctx)
            except Exception as e:                    # noqa: BLE001
                record(name, f'{name} raised', False, f'{type(e).__name__}: {e}')
    finally:
        try:
            await sensor_api.set_sensor_config(
                args.ip, args.mac, args.password,
                full_scale=ctx.entry['full_scale'], axes=ctx.entry['axes'],
                odr_div=ctx.entry['odr_div'], fft_size=ctx.entry['fft_size'],
                fft_precision=ctx.entry['fft_precision'],
                dc_removal=ctx.entry['dc_removal'],
                filter_enabled=ctx.entry['filter']['filter_enabled'],
                filter_cutoff=ctx.entry['filter']['filter_cutoff'])
            print(f'\n# config restored to entry state')
        except Exception as e:                        # noqa: BLE001
            print(f'\n# WARNING: could not restore config: {e}')
        ctx.cap.stop()

    npass = sum(1 for r in results if r['ok'] is True)
    nskip = sum(1 for r in results if r['ok'] is None)
    ngate = sum(1 for r in results if r['ok'] is not None)
    print(f'\n{"="*64}')
    for name in selected:
        sub = [r for r in results if r['check'] == name]
        bad = [r for r in sub if r['ok'] is False]
        print(f'{name:11s} {len(sub)-len(bad):3d}/{len(sub):<3d} '
              f'{"OK" if not bad else "FAILED: " + "; ".join(r["name"] for r in bad)}')
    print(f'{"="*64}')
    print(f'{npass}/{ngate} assertions passed'
          + (f', {nskip} skipped' if nskip else '')
          + f' in {time.monotonic()-t0:.0f}s  '
            f'(firmware 0x{info["firmware_version"]:04x})')

    if args.json:
        with open(args.json, 'w') as fh:
            json.dump({'firmware': info['firmware_version'],
                       'entry_config': ctx.entry, 'results': results}, fh, indent=2)
        print(f'summary written to {args.json}')
    return 0 if npass == ngate else 1


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ip', required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--password', default='')
    p.add_argument('--port', type=int, default=int(os.environ.get('TCP_PORT', '8066')))
    p.add_argument('--expect-fw', default=None, help='e.g. 0x1037')
    p.add_argument('--only', nargs='*', default=[], choices=list(CHECKS))
    p.add_argument('--skip', nargs='*', default=[], choices=list(CHECKS))
    p.add_argument('--sizes', type=int, nargs='+', default=[256, 512, 1024, 2048])
    p.add_argument('--odr', type=int, nargs='+',
                   default=[1, 2, 4, 8, 16, 32, 64, 128, 256])
    p.add_argument('--noise-odr', type=int, nargs='+', default=[1, 64])
    p.add_argument('--fft-frames', type=int, default=16)
    p.add_argument('--noise-seconds', type=float, default=10.0)
    p.add_argument('--min-samples', type=int, default=4096)
    p.add_argument('--boot-timeout', type=float, default=90.0)
    p.add_argument('--tol-axis', type=float, default=0.10, help='relative, vector modes')
    p.add_argument('--tol-axis-abs', type=float, default=0.35, help='m/s2, per-axis DC')
    p.add_argument('--tol-format', type=float, default=0.10)
    p.add_argument('--tol-flat', type=float, default=0.35)
    p.add_argument('--floor-lo', type=float, default=20.0, help='ug/rtHz')
    p.add_argument('--floor-hi', type=float, default=250.0)
    p.add_argument('--json', default=None)
    p.add_argument('--force', action='store_true')
    args = p.parse_args()
    if port_in_use(args.port) and not args.force:
        print(f'ERROR: port {args.port} is in use — stop the viewer backend first '
              f'(or pass --force, which will take the sensor stream away from it).')
        sys.exit(2)
    sys.exit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
