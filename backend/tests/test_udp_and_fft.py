"""
Synthetic tests for the firmware features consumed on the host side:

  1. UDP chunk reassembly (udp_receiver.UDPReceiver)
  2. Metadata-driven FFT decode — float32, int16, and uint16 vector mode
     (protobuf_decoder.decode_fft_frame)
  3. FFT amplitude normalisation (fw 0x1033) — magnitudes checked against the
     firmware's reference implementation, PSD against scipy's periodogram
     definition, and the re-normalisation of pre-0x1033 sensors
  4. Software DC removal (fw 0x1032) — SensorConfig tag 7 and capture metadata

No sensor hardware is required — every frame is built from the generated
protobuf classes and fed through the real decode/reassembly code.

Run standalone:
    /home/jacob/venv-browser-viewer/bin/python backend/tests/test_udp_and_fft.py
(also discoverable by pytest as test_* functions).
"""

import asyncio
import os
import json
import struct
import sys
import threading

import numpy as np

# ── path bootstrap: backend/ and backend/protobuf/ ──────────────────────────
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_PROTO = os.path.join(_BACKEND, 'protobuf')
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

import message_pb2
from payloads.meta_data_pb2 import NumpyType, NumpyEndian, DataAxis
from shared.physical_units_pb2 import DataUnit
from protobuf_decoder import decode_fft_frame
from udp_receiver import UDPReceiver


# ── Burst capture timeout / cancel ───────────────────────────────────────────

def test_burst_timeout_and_cancel():
    """A burst must end even if no frames arrive (sensor not connected), so the
    UI never gets stuck on 'Capturing…'. Partial data still finishes normally;
    no data times out; explicit cancel always resets."""
    import asyncio
    from broadcaster import Broadcaster
    from protobuf_decoder import FrameData

    async def run():
        b = Broadcaster(fps=60)
        seen = []
        async def fake_broadcast(msg):
            seen.append(json.loads(msg))
        b._broadcast = fake_broadcast

        # No data + deadline passed → cancel(timeout)
        b.start_burst(1.0)
        b._burst_start_time -= 3.0
        await b._check_burst_timeout()
        assert not b._burst_active
        assert seen[-1] == {'type': 'burst', 'cancelled': True, 'reason': 'timeout'}

        # Partial data + deadline passed → normal burst (not cancelled)
        b.start_burst(1.0)
        b._last_frame = FrameData(device_id='d', stream_uid=7, seq=1, timestamp_ns=0,
                                  recv_time_ns=123, sample_rate_hz=6667.0,
                                  columns={'accel_x': np.array([1.0, 2.0, 3.0])},
                                  units={'accel_x': 'm/s²'})
        b._burst_samples = {'accel_x': [1.0, 2.0, 3.0]}
        b._burst_start_time -= 3.0
        await b._check_burst_timeout()
        assert not b._burst_active
        assert seen[-1]['type'] == 'burst' and not seen[-1].get('cancelled')
        assert seen[-1]['stream_uid'] == 7

        # Explicit cancel always resets + notifies
        b.start_burst(1.0)
        await b.cancel_burst('cancelled')
        assert not b._burst_active
        assert seen[-1] == {'type': 'burst', 'cancelled': True, 'reason': 'cancelled'}

        # Within the deadline → no premature end
        b.start_burst(5.0)
        n = len(seen)
        await b._check_burst_timeout()
        assert b._burst_active and len(seen) == n

    asyncio.run(run())
    print('  ok  burst timeout (no data), partial-data finish, cancel, and no premature end')


# ── per-stream frame accounting (traffic monitor) ────────────────────────────

def test_stream_seq_accounting():
    """received/missing/resets from sequence numbers: in-order = 0 missing,
    gaps counted, duplicates ignored, uint32 wrap safe, restarts (backward jump
    or stream_uid change) counted as resets not loss."""
    import main
    main._reset_stream_stats()

    for seq in (10, 11, 12):
        main._account_seq('fft', 3, seq)
    assert main._stream_stats['fft']['missing'] == 0

    main._account_seq('fft', 3, 15)              # gap of 3 → missing += 2
    assert main._stream_stats['fft']['missing'] == 2
    main._account_seq('fft', 3, 15)              # duplicate → ignored
    assert main._stream_stats['fft']['missing'] == 2
    main._account_seq('fft', 3, 3)               # backward → reset
    assert main._stream_stats['fft']['resets'] == 1
    assert main._stream_stats['fft']['missing'] == 2

    main._reset_stream_stats()
    main._account_seq('raw', 2, 0xFFFFFFFF)
    main._account_seq('raw', 2, 0)               # uint32 wrap → delta 1 → 0 missing
    assert main._stream_stats['raw']['missing'] == 0
    main._account_seq('raw', 9, 100)             # stream_uid change → reset
    assert main._stream_stats['raw']['resets'] == 1
    assert main._stream_stats['raw']['missing'] == 0
    assert main._stream_stats['raw']['received'] == 3

    # _all_stats exposes the nested per-stream detail
    st = main._all_stats()
    assert 'streams' in st and set(st['streams']['fft']) >= {'received', 'missing', 'resets', 'transport'}
    print('  ok  per-stream seq accounting (gaps, dup, reset, wrap, uid change)')


# ── HMAC / auth error mapping ────────────────────────────────────────────────

def test_hmac_error_mapping():
    """A sensor response with RESPONSE_STATUS_INVALID_HMAC must raise the
    distinct SensorAuthError (so the UI can show a 'check password' pop-up),
    while other error statuses stay a generic SensorApiError."""
    import sensor_api
    import protobuf.sensor_cmd_pb2 as pb

    bad = pb.Response(); bad.status = pb.RESPONSE_STATUS_INVALID_HMAC
    try:
        sensor_api._check(bad.SerializeToString())
        assert False, 'expected SensorAuthError'
    except sensor_api.SensorAuthError:
        pass

    other = pb.Response(); other.status = pb.RESPONSE_STATUS_INVALID_PARAM
    try:
        sensor_api._check(other.SerializeToString())
        assert False, 'expected SensorApiError'
    except sensor_api.SensorAuthError:
        assert False, 'INVALID_PARAM must not be treated as an auth error'
    except sensor_api.SensorApiError:
        pass

    # SensorAuthError must subclass SensorApiError (so generic handlers still catch it)
    assert issubclass(sensor_api.SensorAuthError, sensor_api.SensorApiError)
    print('  ok  HMAC status → SensorAuthError; other status → SensorApiError')


# ── DC removal (software high-pass, firmware 0x1032) ─────────────────────────

def _stub_send_recv(pb, cfg, sent):
    """Replacement for sensor_api._send_recv: records every Request and answers
    with `cfg` as the sensor's SensorConfig."""
    async def _send_recv(target_ip, payload, mac_str, password):
        req = pb.Request()
        req.ParseFromString(payload)
        sent.append(req)
        resp = pb.Response(status=pb.RESPONSE_STATUS_OK)
        resp.sensor.CopyFrom(cfg)
        return resp.SerializeToString()
    return _send_recv


def test_dc_removal_get_and_set():
    """dc_removal rides SensorConfig tag 7. A sensor that omits it (fw ≤ 0x1031)
    must read back as OFF rather than UNDEFINED or an error; a set must add the
    field without disturbing the other settings, which are orthogonal to it."""
    import sensor_api
    import protobuf.sensor_cmd_pb2 as pb

    cur = pb.SensorConfig(full_scale=pb.ACCEL_FS_2G, axes=pb.AXIS_XYZ,
                          odr_div=pb.ODR_DIV_4, fft_size=pb.FFT_SIZE_1024,
                          fft_precision=pb.FFT_PRECISION_Q15)
    cur.filter.filter_enabled = pb.FILTER_LOW_PASS2
    cur.filter.filter_cutoff  = pb.CUTOFF_2p66_KHZ

    sent = []
    orig = sensor_api._send_recv
    sensor_api._send_recv = _stub_send_recv(pb, cur, sent)
    try:
        # Old firmware: tag 7 absent → 0/UNDEFINED on the wire → reported as OFF.
        assert cur.dc_removal == 0
        cfg = asyncio.run(sensor_api.get_sensor_config('1.2.3.4', 'aa:bb:cc:dd:ee:ff', ''))
        assert cfg['dc_removal'] == 'DC_REMOVAL_OFF', cfg['dc_removal']

        # Setting only dc_removal must preserve everything read back first.
        sent.clear()
        asyncio.run(sensor_api.set_sensor_config(
            '1.2.3.4', 'aa:bb:cc:dd:ee:ff', '', dc_removal='DC_REMOVAL_1_HZ'))
        assert len(sent) == 2, 'expected a get followed by a set'
        sc = sent[1].set_sensor_config
        assert sc.dc_removal == pb.DcRemoval.Value('DC_REMOVAL_1_HZ')
        assert sc.odr_div == pb.ODR_DIV_4
        assert sc.fft_precision == pb.FFT_PRECISION_Q15
        # Orthogonal to the hardware filter — it must come through untouched.
        assert sc.filter.filter_enabled == pb.FILTER_LOW_PASS2
        assert sc.filter.filter_cutoff  == pb.CUTOFF_2p66_KHZ
        # Field 7, varint: key 0x38 then the value.
        assert b'\x38\x04' in sc.SerializeToString()

        # A normal readback echoes the enum name.
        cur.dc_removal = pb.DcRemoval.Value('DC_REMOVAL_0p5_HZ')
        cfg = asyncio.run(sensor_api.get_sensor_config('1.2.3.4', 'aa:bb:cc:dd:ee:ff', ''))
        assert cfg['dc_removal'] == 'DC_REMOVAL_0p5_HZ'

        # A value newer than these bindings degrades to its number, it must not
        # raise — same rule the NTP-mode and FFT-precision readbacks follow.
        cur.dc_removal = 9
        cfg = asyncio.run(sensor_api.get_sensor_config('1.2.3.4', 'aa:bb:cc:dd:ee:ff', ''))
        assert cfg['dc_removal'] == '9', cfg['dc_removal']

        # An unknown name from the UI is rejected locally, not sent as garbage.
        sent.clear()
        try:
            asyncio.run(sensor_api.set_sensor_config(
                '1.2.3.4', 'aa:bb:cc:dd:ee:ff', '', dc_removal='DC_REMOVAL_42_HZ'))
            assert False, 'expected ValueError for an unknown enum name'
        except ValueError:
            pass
        assert all(not s.HasField('set_sensor_config') for s in sent)
    finally:
        sensor_api._send_recv = orig
    print('  ok  dc_removal get (absent → OFF, unknown → numeric) and set (tag 7, others kept)')


def test_capture_metadata_records_sensor_config():
    """No sensor setting is signalled in the packet header, so the capture
    writers must stamp the last-known config — without dc_removal in the file a
    saved capture does not say whether the signal was high-passed, or at what
    corner."""
    import tempfile
    from log_writer import LogManager
    from protobuf_decoder import FrameData

    frame = FrameData(device_id='aa:bb:cc:dd:ee:ff', stream_uid=4, seq=1,
                      timestamp_ns=0, recv_time_ns=0, sample_rate_hz=6667.0,
                      columns={'accel_x': np.array([1.0, 2.0])},
                      units={'accel_x': 'm/s²'})

    with tempfile.TemporaryDirectory() as d:
        lm = LogManager(output_dir=d, fmt='tsv')
        lm.set_sensor_config({'aa:bb:cc:dd:ee:ff': {
            'dc_removal': 'DC_REMOVAL_1_HZ', 'odr_div': 'ODR_DIV_4',
            'fft_precision': None,
        }})
        lm.write(frame)
        lm.close_all()
        path = os.path.join(d, os.listdir(d)[0])
        head = open(path).read()
        assert '# dc_removal=DC_REMOVAL_1_HZ' in head, head[:400]
        assert '# odr_div=ODR_DIV_4' in head
        assert 'fft_precision' not in head, 'unknown settings must be omitted, not written as None'

        # A device with no config read yet must still log, just without settings.
        lm2 = LogManager(output_dir=d, fmt='tsv')
        lm2.write(frame)
        lm2.close_all()

    # main._note_sensor_cfg flattens the API shape and forwards it to the writer.
    import main
    main._note_sensor_cfg('AA:BB:CC:DD:EE:FF', {
        'full_scale': 'ACCEL_FS_2G', 'dc_removal': 'DC_REMOVAL_2_HZ',
        'filter': {'filter_enabled': 'FILTER_LOW_PASS2', 'filter_cutoff': 'CUTOFF_2p66_KHZ'},
    })
    got = main._sensor_cfg_seen['aa:bb:cc:dd:ee:ff']   # keyed as FrameData.device_id
    assert got['dc_removal'] == 'DC_REMOVAL_2_HZ'
    assert got['filter_cutoff'] == 'CUTOFF_2p66_KHZ'
    print('  ok  capture metadata records dc_removal + sensor settings (TSV header)')


# ── helpers ─────────────────────────────────────────────────────────────────

def _build_fft_frame(bins, columns, flags=0, device=None, rate_hz=1000.0):
    """columns: list of (data_axis, numpy_type, numpy_bytes, values_ndarray).

    float_factor/exp are set so the SI scale factor is exactly 1.0, making the
    expected magnitudes trivially checkable. `flags` sets Header.flags.
    `device` sets Header.device_id (6 raw bytes), which is what keys the
    decoder's per-device FFT scaling mode.
    """
    msg = message_pb2.Message()
    if flags:
        msg.flags = flags
    if device is not None:
        msg.device_id = device
    fft = msg.fft_stream
    fft.fft_bins = bins
    fft.actual_frame_rate_hz = rate_hz
    blob = b''
    for axis, ntype, nbytes, values in columns:
        md = fft.meta_data.add()
        md.data_axis = axis
        md.data_numpy_type = ntype
        md.data_numpy_bytes = nbytes
        md.data_numpy_endian = NumpyEndian.NUMPY_ENDIAN_LITTLE
        md.data_unit = DataUnit.UNIT_METER_PER_SQUARE_SECOND
        md.float_factor = 1000.0             # × 10^-3 = 1.0
        md.si_unit_scaling_base10_exp = -3
        blob += values.tobytes()
    msg.payload = blob
    return msg.SerializeToString()


def _dtype_for(ntype, nbytes):
    kind = {NumpyType.NUMPY_TYPE_INTEGER: 'i',
            NumpyType.NUMPY_TYPE_UNSIGNED_INTEGER: 'u',
            NumpyType.NUMPY_TYPE_FLOATING_POINT: 'f'}[ntype]
    return np.dtype(f'<{kind}{nbytes}')


# ── FFT decode tests ────────────────────────────────────────────────────────

def test_fft_float32_decode():
    bins = 8
    # Include a tiny value (1e-6) that Q15 would have rounded to zero.
    vals = np.array([0.0, 1e-6, 0.5, 1.25, 3.0, 10.0, 100.0, 1234.5], dtype='<f4')
    frame = _build_fft_frame(
        bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, vals)])
    fd = decode_fft_frame(frame, 0)
    assert fd is not None, 'float32 frame failed to decode'
    assert fd.fft_bins == bins
    got = np.array(fd.magnitudes['x'])
    # sf = 1.0 is the whole conversion — no ×fft_size, no per-precision fudge.
    assert np.allclose(got, vals.astype(np.float64), rtol=1e-5, atol=1e-9), \
        f'float32 magnitudes wrong: {got}'
    # The sub-µg value must survive (float32's whole point).
    assert got[1] > 0, 'small float32 value was lost'
    print('  ok  float32 decode (values preserved, metadata scale only)')


def test_fft_int16_no_size_scaling():
    """From firmware 0x1033 both transports ship the same physical amplitude
    scale, so int16 gets exactly the metadata conversion — the old ×fft_size
    Q15 block-float compensation is gone (firmware note 2026-08-06)."""
    bins = 8
    raw = np.array([1, 2, 10, 100, 1000, -5, 32000, -32000], dtype='<i2')
    frame = _build_fft_frame(
        bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_INTEGER, 2, raw)])
    fd = decode_fft_frame(frame, 0)
    assert fd is not None
    got = np.array(fd.magnitudes['x'])
    assert np.allclose(got, raw.astype(np.float64)), \
        f'int16 magnitudes must equal raw × metadata scale, got {got}'
    print('  ok  int16 decode (no ×fft_size — 0x1033 scale)')


def test_fft_q15_uint16_vector_mode():
    bins = 4
    raw = np.array([10, 2000, 40000, 65000], dtype='<u2')  # unsigned range
    # data_axis 13 → 'xyz_vec' in the decoder's vector-axis map.
    frame = _build_fft_frame(
        bins, [(13, NumpyType.NUMPY_TYPE_UNSIGNED_INTEGER, 2, raw)])
    fd = decode_fft_frame(frame, 0)
    assert fd is not None
    keys = list(fd.magnitudes.keys())
    assert len(keys) == 1 and keys[0].endswith('_vec'), \
        f'vector column mislabelled: {keys}'
    got = np.array(fd.magnitudes[keys[0]])
    assert np.allclose(got, raw.astype(np.float64))
    assert got[3] > got[0], 'unsigned uint16 not decoded correctly'
    print('  ok  uint16 vector-mode decode')


def test_fft_multi_axis_float32():
    bins = 4
    x = np.array([1.0, 2.0, 3.0, 4.0], dtype='<f4')
    y = np.array([5.0, 6.0, 7.0, 8.0], dtype='<f4')
    z = np.array([9.0, 10.0, 11.0, 12.0], dtype='<f4')
    frame = _build_fft_frame(bins, [
        (DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, x),
        (DataAxis.DATA_AXIS_Y, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, y),
        (DataAxis.DATA_AXIS_Z, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, z),
    ])
    fd = decode_fft_frame(frame, 0)
    assert set(fd.magnitudes) == {'x', 'y', 'z'}
    assert np.allclose(fd.magnitudes['y'], y.astype(np.float64))
    assert np.allclose(fd.magnitudes['z'], z.astype(np.float64))
    print('  ok  multi-axis float32 columns split correctly')


def test_no_time_sync_flag_decode():
    """Header.flags FLAG_NO_TIME_SYNC (=2) must surface as FrameData.no_time_sync.
    Old firmware sends flags=0 → False. Drives the time-sync UI indicator."""
    bins = 4
    vals = np.array([1.0, 2.0, 3.0, 4.0], dtype='<f4')
    col = [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, vals)]

    # flags = 0 → synced (old firmware default)
    fd0 = decode_fft_frame(_build_fft_frame(bins, col, flags=0), 0)
    assert fd0 is not None and fd0.no_time_sync is False, 'flags=0 must be no_time_sync=False'

    # flag set → not synced
    nts = message_pb2.Flags.FLAG_NO_TIME_SYNC
    fd1 = decode_fft_frame(_build_fft_frame(bins, col, flags=nts), 0)
    assert fd1 is not None and fd1.no_time_sync is True, 'FLAG_NO_TIME_SYNC must set no_time_sync'

    # Unrelated flags must not trip it (e.g. FLAG_TEST_RUN without NO_TIME_SYNC).
    fd2 = decode_fft_frame(
        _build_fft_frame(bins, col, flags=message_pb2.Flags.FLAG_TEST_RUN), 0)
    assert fd2 is not None and fd2.no_time_sync is False, 'other flags must not set no_time_sync'

    # Both bits set → still detected (bitmask, not equality).
    fd3 = decode_fft_frame(
        _build_fft_frame(bins, col, flags=nts | message_pb2.Flags.FLAG_TEST_RUN), 0)
    assert fd3.no_time_sync is True, 'masked bit must be detected among other flags'
    print('  ok  FLAG_NO_TIME_SYNC decodes to no_time_sync (bitmask)')


def _firmware_spectrum(x, N):
    """Reference implementation of the 0x1033 FFT, verbatim from the firmware
    note 2026-08-06 — periodic Hann scaled by 4/N (= 2/(N·CG)), DC halved,
    Nyquist dropped. A tone of amplitude A at bin centre returns exactly A."""
    k = np.arange(N)
    w = 0.5 * (1.0 - np.cos(2.0 * np.pi * k / N)) * (4.0 / N)
    X = np.fft.rfft(x * w)
    X[0] *= 0.5
    return np.abs(X[:N // 2])


def test_fft_amplitude_matches_reference_implementation():
    """A tone of amplitude A must read A, and a constant offset A must read A in
    bin 0, for every fft_size — the host must add nothing to what the firmware
    sends. Checks the reference implementation itself, then that the decoder
    passes it through unchanged."""
    fs = 1000.0
    for N in (256, 512, 1024, 2048):
        bins = N // 2
        A = 3.25
        k0 = 40                                      # exact bin centre
        n = np.arange(N)
        tone = A * np.sin(2 * np.pi * k0 * n / N)
        spec = _firmware_spectrum(tone, N)
        assert abs(spec[k0] - A) < 1e-6 * A, \
            f'N={N}: tone of amplitude {A} read {spec[k0]} at its bin'

        off = _firmware_spectrum(np.full(N, A), N)
        assert abs(off[0] - A) < 1e-6 * A, \
            f'N={N}: constant offset {A} read {off[0]} in bin 0'

        # Through the real decoder (sf = 1.0), values must be untouched.
        frame = _build_fft_frame(
            bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4,
                    spec.astype('<f4'))], rate_hz=fs)
        fd = decode_fft_frame(frame, 0)
        got = np.array(fd.magnitudes['x'])
        assert np.allclose(got, spec, rtol=1e-5, atol=1e-9), \
            f'N={N}: decoder altered the firmware amplitudes'
        assert abs(got[k0] - A) < 1e-4 * A, \
            f'N={N}: decoded tone amplitude {got[k0]} != {A}'
    print('  ok  amplitude spectrum matches the firmware reference (N=256..2048)')


def test_fft_legacy_firmware_compensation():
    """Pre-0x1033 firmware shipped magnitudes 8× low (int16) or N/4× high
    (float32) and did not halve DC. Once Get Info has told the host the version,
    those bins must be re-normalised onto the 0x1033 scale; a device on 0x1033
    (or an unknown one) must be left alone."""
    import protobuf_decoder as _pd
    mac_bytes = b'\x02\xa0\x6d\x63\x38\x01'
    mac = '02:a0:6d:63:38:01'
    bins = 8
    N = bins * 2
    raw_i = np.array([100, 200, 300, 400, 500, 600, 700, 800], dtype='<i2')
    raw_f = raw_i.astype('<f4')

    def _mags(vals, ntype, nbytes):
        frame = _build_fft_frame(
            bins, [(DataAxis.DATA_AXIS_X, ntype, nbytes, vals)], device=mac_bytes)
        return np.array(decode_fft_frame(frame, 0).magnitudes['x'])

    try:
        # Unknown version → taken at the documented word (already normalised).
        _pd._fw_version.pop(mac, None)
        assert np.allclose(_mags(raw_i, NumpyType.NUMPY_TYPE_INTEGER, 2),
                           raw_i.astype(np.float64)), \
            'unknown firmware version must not be compensated'

        # Known-old → int16 ×8, bin 0 ×4 (half, because 0x1033 halves DC).
        _pd.note_firmware_version(mac, 0x1032)
        assert _pd.fft_scaling_mode(mac) == 'legacy_compensated'
        got = _mags(raw_i, NumpyType.NUMPY_TYPE_INTEGER, 2)
        want = raw_i.astype(np.float64) * 8.0
        want[0] = raw_i[0] * 4.0
        assert np.allclose(got, want), f'legacy int16 compensation wrong: {got}'

        # Known-old → float32 ×4/N, bin 0 ×2/N.
        got = _mags(raw_f, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4)
        want = raw_f.astype(np.float64) * (4.0 / N)
        want[0] = raw_f[0] * (2.0 / N)
        assert np.allclose(got, want), f'legacy float32 compensation wrong: {got}'

        # And the frame says so, for capture metadata.
        frame = _build_fft_frame(
            bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, raw_f)],
            device=mac_bytes)
        assert decode_fft_frame(frame, 0).legacy_scaling is True

        # Current firmware → untouched.
        _pd.note_firmware_version(mac, 0x1033)
        assert _pd.fft_scaling_mode(mac) == 'normalised'
        assert np.allclose(_mags(raw_i, NumpyType.NUMPY_TYPE_INTEGER, 2),
                           raw_i.astype(np.float64)), \
            '0x1033 magnitudes must not be compensated'
        frame = _build_fft_frame(
            bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_INTEGER, 2, raw_i)],
            device=mac_bytes)
        assert decode_fft_frame(frame, 0).legacy_scaling is False
    finally:
        _pd._fw_version.pop(mac, None)
        _pd._fft_mode_logged.pop(mac, None)
    print('  ok  pre-0x1033 magnitudes re-normalised (int16 ×8, float32 ×4/N, DC halved)')


def test_sensor_info_endpoint_feeds_fft_scaling():
    """Get Info is the only thing that tells the host which FFT scale a sensor
    is on, so /api/sensor/info must hand the firmware version to the decoder and
    report the resulting mode back for the UI."""
    import sensor_api
    import protobuf_decoder as _pd
    import protobuf.sensor_cmd_pb2 as pb
    import main

    mac = 'aa:bb:cc:00:11:22'

    def _stub_info(version):
        async def _send_recv(target_ip, payload, mac_str, password):
            resp = pb.Response(status=pb.RESPONSE_STATUS_OK)
            resp.info.firmware_version = version
            return resp.SerializeToString()
        return _send_recv

    orig = sensor_api._send_recv
    try:
        for version, want in ((0x1032, 'legacy_compensated'), (0x1033, 'normalised')):
            sensor_api._send_recv = _stub_info(version)
            _pd._fw_version.pop(mac, None)
            info = asyncio.run(main.sensor_info(
                main.SensorTarget(target_ip='1.2.3.4', mac=mac, password='')))
            assert info['firmware_version'] == version
            assert info['fft_scaling'] == want, f'0x{version:04x} → {info["fft_scaling"]}'
            assert _pd.fft_scaling_mode(mac) == want, 'decoder was not told the version'
    finally:
        sensor_api._send_recv = orig
        _pd._fw_version.pop(mac, None)
    print('  ok  /api/sensor/info records the firmware version and reports fft_scaling')


def test_fft_psd_one_sided_periodogram():
    """PSD must equal a one-sided, Hann-windowed, density-scaled periodogram of
    the *raw* samples (scipy.signal.periodogram(x, fs, window='hann',
    scaling='density')). The sensor now sends the amplitude spectrum a_k, so the
    host applies S_k = a_k²/(2·ENBW·Δf) with ENBW = 1.5 bins — and ×2 at DC,
    which the offset in `x` below actually exercises."""
    fs = 1000.0            # _build_fft_frame's default actual_frame_rate_hz
    N = 512
    bins = N // 2
    rng = np.random.default_rng(3)
    t = np.arange(N) / fs
    x = (2.0                                            # DC offset — tests bin 0
         + 1.5 * np.sin(2 * np.pi * 125 * t)
         + 0.05 * rng.standard_normal(N))
    x_dev = _firmware_spectrum(x, N).astype('<f4')      # what the sensor streams

    import protobuf_decoder as _pd
    _pd._sample_rate = 26667.0                          # force use of actual_frame_rate_hz
    frame = _build_fft_frame(
        bins, [(DataAxis.DATA_AXIS_X, NumpyType.NUMPY_TYPE_FLOATING_POINT, 4, x_dev)])
    fd = decode_fft_frame(frame, 0)
    ours = np.array(fd.psd['x'])

    # Reference: one-sided Hann density periodogram of the raw samples.
    n = np.arange(N)
    wp = 0.5 - 0.5 * np.cos(2 * np.pi * n / N)          # periodic Hann (device window)
    X = np.fft.rfft(wp * x)
    ref = (np.abs(X) ** 2) / (fs * np.sum(wp ** 2))
    ref[1:] *= 2.0                                      # one-sided; DC not doubled
    ref = ref[:bins]

    assert np.allclose(ours, ref, rtol=1e-4, atol=1e-12), \
        'PSD does not match the one-sided Hann density periodogram'
    print('  ok  PSD matches one-sided Hann-window density periodogram')


# ── UDP reassembly tests ────────────────────────────────────────────────────

def _make_datagrams(pb, packet_id, chunk_size):
    """Wrap a protobuf payload as a length-prefixed frame, split into chunks."""
    frame = struct.pack('>I', len(pb)) + pb
    total = len(frame)
    pieces = [frame[i:i + chunk_size] for i in range(0, total, chunk_size)]
    count = len(pieces)
    out = []
    for idx, c in enumerate(pieces):
        hdr = struct.pack('<IHHHH', packet_id, idx, count, len(c), total)
        out.append(hdr + c)
    return out


class _LoopHarness:
    """A real asyncio loop in a background thread + a queue bound to it."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._t.start()
        self.queue = asyncio.run_coroutine_threadsafe(self._mk(), self.loop).result()

    async def _mk(self):
        return asyncio.Queue()

    def drain(self):
        out = []
        while True:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    asyncio.wait_for(self.queue.get(), 0.2), self.loop)
                out.append(fut.result(0.5))
            except Exception:
                break
        return out

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def _new_receiver(h):
    return UDPReceiver('127.0.0.1', 0, h.queue, h.loop)


def test_udp_single_chunk():
    h = _LoopHarness()
    rx = _new_receiver(h)
    pb = b'\x08\x2a hello single-chunk payload'
    for dg in _make_datagrams(pb, packet_id=1, chunk_size=4096):
        rx._handle(dg, '10.0.0.5')
    frames = [f[0] for f in h.drain()]
    h.stop()
    assert frames == [pb], f'single-chunk mismatch: {frames}'
    assert rx.lost_chunks == 0
    print('  ok  single-chunk datagram reassembles to exact payload')


def test_udp_multi_chunk_in_order():
    h = _LoopHarness()
    rx = _new_receiver(h)
    pb = bytes(range(256)) * 4  # 1024 bytes → several chunks
    for dg in _make_datagrams(pb, packet_id=2, chunk_size=300):
        rx._handle(dg, '10.0.0.5')
    frames = [f[0] for f in h.drain()]
    h.stop()
    assert frames == [pb], 'in-order multi-chunk mismatch'
    assert rx.lost_chunks == 0
    print('  ok  multi-chunk in-order reassembly')


def test_udp_multi_chunk_out_of_order():
    h = _LoopHarness()
    rx = _new_receiver(h)
    pb = bytes(range(200)) * 3
    dgrams = _make_datagrams(pb, packet_id=3, chunk_size=250)
    assert len(dgrams) >= 3
    # Reverse arrival order.
    for dg in reversed(dgrams):
        rx._handle(dg, '10.0.0.5')
    frames = [f[0] for f in h.drain()]
    h.stop()
    assert frames == [pb], 'out-of-order reassembly failed'
    assert rx.lost_chunks == 0
    print('  ok  out-of-order chunks reassemble correctly')


def test_udp_incomplete_packet_times_out():
    h = _LoopHarness()
    rx = _new_receiver(h)
    pb_lost = bytes(range(150)) * 3
    dgrams = _make_datagrams(pb_lost, packet_id=5, chunk_size=200)
    assert len(dgrams) >= 3
    del dgrams[1]                       # drop the middle chunk of packet 5
    for dg in dgrams:
        rx._handle(dg, '10.0.0.5')
    # A later packet must NOT discard the still-incomplete packet 5 — raw & FFT
    # multiplex on one packet_id space, so a newer id is not proof of loss.
    pb_ok = b'the-next-packet-arrives-complete'
    for dg in _make_datagrams(pb_ok, packet_id=6, chunk_size=4096):
        rx._handle(dg, '10.0.0.5')
    assert rx.lost_packets == 0, 'incomplete packet wrongly dropped by a newer id'
    # It is reclaimed only after the timeout.
    for k in rx._partials:
        rx._partials[k].first_ns -= 10**9   # make it look old
    rx._evict_stale()
    frames = [f[0] for f in h.drain()]
    h.stop()
    assert frames == [pb_ok], f'expected only packet 6, got {frames}'
    assert rx.lost_packets == 1 and rx.lost_chunks == 1, (rx.lost_packets, rx.lost_chunks)
    print('  ok  incomplete packet reclaimed by timeout (not by a newer packet_id)')


def test_udp_interleaved_raw_fft():
    """A 1-chunk 'raw' packet arriving mid-assembly of a multi-chunk 'FFT'
    packet (higher packet_id, shared id space) must NOT discard the FFT packet.
    This is the bug where FFT ≥3 chunks never assembled over UDP."""
    h = _LoopHarness()
    rx = _new_receiver(h)
    fft = bytes(range(200)) * 5                                   # large → many chunks
    fft_dgrams = _make_datagrams(fft, packet_id=10, chunk_size=250)
    assert len(fft_dgrams) >= 3
    raw = b'one-chunk-raw-frame'
    raw_dgrams = _make_datagrams(raw, packet_id=11, chunk_size=4096)  # 1 chunk, higher id
    assert len(raw_dgrams) == 1
    # Interleave the raw packet between the FFT chunks (as the sensor does).
    order = [fft_dgrams[0], raw_dgrams[0]] + fft_dgrams[1:]
    for dg in order:
        rx._handle(dg, '10.0.0.9')
    frames = [f[0] for f in h.drain()]
    h.stop()
    assert raw in frames and fft in frames, f'both must be delivered; got {len(frames)}'
    assert rx.lost_packets == 0, f'no loss expected, got {rx.lost_packets}'
    print('  ok  interleaved raw packet does not drop an in-progress FFT packet')


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
