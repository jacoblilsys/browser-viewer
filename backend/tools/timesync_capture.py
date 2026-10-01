"""Log every frame's sensor timestamp against host receive time.

Explains the viewer's Time Sync trace, which plots
    host receive time - sensor timestamp   (ms)
for raw *and* FFT frames in one series. For each scenario (ODR divider x which
streams run) this captures every frame's header and writes a CSV, then prints
per-stream statistics:

  lag_ms      host recv - sensor timestamp
  dur_ms      the span of data the frame covers (raw: samples/rate,
              FFT: fft_size/rate) — timestamps mark the FIRST sample (0x1036+),
              so lag_ms >= dur_ms + transport latency
  dts_ms      step between consecutive sensor timestamps of the same stream
  drecv_ms    step between consecutive host receive times
  dframe      step in frame_number

Stop the viewer backend first: this binds the sensor ports itself.

Example:
    python tools/timesync_capture.py --ip <sensor-ip> --mac <sensor-mac> \\
        --password <app-password> --seconds 20
"""
import argparse
import asyncio
import csv
import os
import statistics as st
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hw_common import port_in_use                      # noqa: E402  (sets sys.path)
import message_pb2                                      # noqa: E402
import protobuf_decoder as dec                          # noqa: E402
import sensor_api                                       # noqa: E402
from network_receiver import NetworkReceiver            # noqa: E402
from udp_receiver import UDPReceiver                    # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
BASE_RATE = 26666.67


def header_row(payload, recv_ns, transport):
    h = message_pb2.Header()
    h.ParseFromString(payload)
    ts = h.device_timestamp.seconds * 1_000_000_000 + h.device_timestamp.nanos
    row = {'recv_ns': recv_ns, 'ts_ns': ts, 'seq': h.sequence_number,
           'flags': h.flags, 'transport': transport, 'uid': h.stream_uid}
    if h.HasField('fft_stream'):
        f = h.fft_stream
        row.update(kind='fft', frame_number=f.frame_number, frame_count=f.frame_count,
                   duration_ns=f.duration_ns, fft_bins=f.fft_bins, hop=f.hop_size,
                   rate_hz=f.actual_frame_rate_hz, fft_rate_hz=f.fft_frame_rate_hz)
    elif h.HasField('frame_stream'):
        f = h.frame_stream
        fr = dec.decode_frame(payload, recv_ns)
        n = len(next(iter(fr.columns.values()), [])) if fr else 0
        row.update(kind='raw', frame_number=f.frame_number, frame_count=f.frame_count,
                   n=n, rate_hz=f.actual_frequency_hz,
                   period_ps=f.actual_period_ps)
    else:
        row['kind'] = 'other'
    return row


async def capture(q, seconds):
    """Frames arriving in the next `seconds` (the receivers stay up across
    scenarios: dropping the TCP listener leaves the sensor's raw TCP client
    unable to reconnect until it is reset)."""
    loop = asyncio.get_running_loop()
    while not q.empty():
        q.get_nowait()
    rows = []
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            item = await asyncio.wait_for(q.get(), timeout=max(0.05, end - loop.time()))
        except asyncio.TimeoutError:
            break
        try:
            rows.append(header_row(item[0], item[1], item[2] if len(item) > 2 else '?'))
        except Exception:                                   # noqa: BLE001
            pass
    return rows


def describe(rows, odr_div):
    rate = BASE_RATE / odr_div
    out = []
    for kind in ('raw', 'fft'):
        r = [x for x in rows if x.get('kind') == kind]
        if len(r) < 3:
            out.append(f'  {kind}: {len(r)} frames')
            continue
        lag = [(x['recv_ns'] - x['ts_ns']) / 1e6 for x in r]
        dts = [(b['ts_ns'] - a['ts_ns']) / 1e6 for a, b in zip(r, r[1:])]
        drecv = [(b['recv_ns'] - a['recv_ns']) / 1e6 for a, b in zip(r, r[1:])]
        dfr = [b['frame_number'] - a['frame_number'] for a, b in zip(r, r[1:])]
        if kind == 'raw':
            dur = st.median(x['n'] for x in r) / rate * 1000
            extra = f'samples/frame {st.median(x["n"] for x in r):.0f}'
        else:
            n = r[-1]['fft_bins'] * 2
            dur = n / rate * 1000
            extra = (f'fft_size {n}, hop {r[-1]["hop"]}, duration_ns {r[-1]["duration_ns"]/1e6:.1f} ms, '
                     f'header rate {r[-1]["rate_hz"]:.1f}/{r[-1]["fft_rate_hz"]:.3f} Hz')
        span_ts = (r[-1]['ts_ns'] - r[0]['ts_ns']) / 1e9
        span_rx = (r[-1]['recv_ns'] - r[0]['recv_ns']) / 1e9
        out.append(
            f'  {kind}: {len(r)} frames  ({extra})\n'
            f'     lag_ms  min {min(lag):8.1f}  med {st.median(lag):8.1f}  max {max(lag):8.1f}'
            f'   (frame covers {dur:.1f} ms)\n'
            f'     dts_ms  min {min(dts):8.1f}  med {st.median(dts):8.1f}  max {max(dts):8.1f}'
            f'   drecv_ms min {min(drecv):7.1f} med {st.median(drecv):7.1f} max {max(drecv):7.1f}\n'
            f'     dframe  {sorted(set(dfr))[:8]}   sensor-time span {span_ts:.2f}s over '
            f'{span_rx:.2f}s host time  (ratio {span_ts / span_rx if span_rx else 0:.3f})'
            + (f'\n     no-time-sync flag set on {sum(1 for x in r if x["flags"] & 1)} frames'
               if any(x['flags'] for x in r) else ''))
    return '\n'.join(out)


async def run(args):
    a = (args.ip, args.mac, args.password)
    entry = await sensor_api.get_sensor_config(*a)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(LOG_DIR, exist_ok=True)
    scenarios = [(o, s) for o in args.odr for s in args.streams]
    print(f'entry config: {entry["odr_div"]} {entry["fft_size"]}  '
          f'(restored at the end)')
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    tcp = NetworkReceiver('0.0.0.0', args.port, q, loop)
    udp = UDPReceiver('0.0.0.0', args.port, q, loop)
    tcp.start()
    udp.start()
    if args.reset:
        # With the listener already up, a reset lets the raw TCP client connect
        # cleanly (it does not recover by itself once a host dropped it).
        try:
            await sensor_api.reset(*a)
        except Exception:                                   # noqa: BLE001
            pass
        await asyncio.sleep(4)
        # boot_now is never answered by the bootloader; fire it and move on.
        bn = asyncio.ensure_future(sensor_api.boot_now(*a))
        for _ in range(90):
            try:
                if (await sensor_api.get_sensor_info(*a))['firmware_version']:
                    break
            except Exception:                               # noqa: BLE001
                pass
            await asyncio.sleep(1)
        else:
            sys.exit('sensor did not come back in application mode')
        bn.cancel()
        await asyncio.sleep(3)              # let the stream sockets connect
        print('sensor reset; app running')
    try:
        for odr, streams in scenarios:
            await sensor_api.set_sensor_config(*a, odr_div=f'ODR_DIV_{odr}')
            raw_on, fft_on = 'raw' in streams, 'fft' in streams
            await (sensor_api.stream_start if raw_on else sensor_api.stream_stop)(*a)
            await (sensor_api.stream_fft_start if fft_on else sensor_api.stream_fft_stop)(*a)
            await asyncio.sleep(args.settle)
            rows = await capture(q, args.seconds)
            name = f'timesync_{stamp}_odr{odr}_{streams}.csv'
            keys = sorted({k for r in rows for k in r})
            with open(os.path.join(LOG_DIR, name), 'w', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=keys)
                w.writeheader()
                w.writerows(rows)
            print(f'\n== ODR_DIV_{odr}, streams: {streams}  ({len(rows)} frames -> logs/{name})')
            print(describe(rows, odr))
    finally:
        await sensor_api.set_sensor_config(*a, odr_div=entry['odr_div'])
        await sensor_api.stream_start(*a)
        await sensor_api.stream_fft_stop(*a)
        print('\nrestored ODR and streams (raw on, FFT off)')
        tcp.stop()
        udp.stop()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ip', required=True)
    p.add_argument('--mac', required=True)
    p.add_argument('--password', default='')
    p.add_argument('--port', type=int, default=int(os.environ.get('TCP_PORT', '8066')))
    p.add_argument('--odr', type=int, nargs='+', default=[8, 4])
    p.add_argument('--streams', nargs='+', default=['raw+fft', 'fft', 'raw'],
                   choices=['raw+fft', 'fft', 'raw'])
    p.add_argument('--seconds', type=float, default=20)
    p.add_argument('--settle', type=float, default=3)
    p.add_argument('--reset', action='store_true',
                   help='reset the sensor once the receivers listen (recovers a raw '
                        'TCP link that a previous host dropped)')
    p.add_argument('--force', action='store_true',
                   help='skip the port check (e.g. only a dead connection lingers)')
    args = p.parse_args()
    if port_in_use(args.port) and not args.force:
        sys.exit(f'port {args.port} is in use — stop the viewer backend first')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
