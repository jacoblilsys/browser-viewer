"""
Deserialises a raw TCP protobuf frame into a FrameData dataclass.
Also decodes FFT magnitude frames into FFTFrameData.

Import path bootstrap: adds the local protobuf/ directory to sys.path so that
the generated *_pb2.py files can use bare package-relative imports
(e.g. ``from config import config_pb2``).
"""

import os
import sys

# Make the bundled protobuf/ tree importable before anything else.
_PROTO_DIR = os.path.join(os.path.dirname(__file__), 'protobuf')
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Dict

import message_pb2
from payloads.meta_data_pb2 import (
    NumpyType,
    NumpyEndian,
    Transformation,
    DataType,
    DataAxis,
    VariableName,
)
from shared.physical_units_pb2 import DataUnit
from transform import adc_to_volt, volt_to_force

_log = logging.getLogger('decoder')

# ── numpy dtype helpers ──────────────────────────────────────────────────────

_NUMPY_KIND = {
    NumpyType.NUMPY_TYPE_UNSIGNED_INTEGER: 'u',
    NumpyType.NUMPY_TYPE_INTEGER:          'i',
    NumpyType.NUMPY_TYPE_FLOATING_POINT:   'f',
}

_NUMPY_ENDIAN = {
    NumpyEndian.NUMPY_ENDIAN_LITTLE:       '<',
    NumpyEndian.NUMPY_ENDIAN_BIG:          '>',
    NumpyEndian.NUMPY_ENDIAN_NOT_RELEVANT: '|',
}

_UNIT_STR: Dict[int, str] = {
    DataUnit.UNIT_METER_PER_SQUARE_SECOND: 'm/s²',
    DataUnit.UNIT_NEWTON:                  'N',
    DataUnit.UNIT_DEGREE_ANG_PER_SECOND:   '°/s',
    DataUnit.UNIT_VOLT:                    'V',
    DataUnit.UNIT_SECOND:                  's',
    DataUnit.UNIT_NONE:                    '',
}

_DATA_TYPE_STR = {
    DataType.DATA_TYPE_NONE:         '',
    DataType.DATA_TYPE_FORCE:        'force',
    DataType.DATA_TYPE_ACCELERATION: 'accel',
    DataType.DATA_TYPE_GYRO:         'gyro',
    DataType.DATA_TYPE_DELTA_TIME:   'dt',
    DataType.DATA_TYPE_TIME:         'time',
}

_DATA_AXIS_STR = {
    DataAxis.DATA_AXIS_NONE:         '',
    DataAxis.DATA_AXIS_X:            'x',
    DataAxis.DATA_AXIS_Y:            'y',
    DataAxis.DATA_AXIS_Z:            'z',
    DataAxis.DATA_AXIS_HORIZONTAL:   'horizontal',
    DataAxis.DATA_AXIS_VERTICAL:     'vertical',
    DataAxis.DATA_AXIS_LONGITUDINAL: 'longitudinal',
    DataAxis.DATA_AXIS_PITCH:        'pitch',
    DataAxis.DATA_AXIS_ROLL:         'roll',
    DataAxis.DATA_AXIS_YAW:          'yaw',
}


def _col_label(md) -> str:
    t = _DATA_TYPE_STR.get(md.data_type, '')
    a = _DATA_AXIS_STR.get(md.data_axis, '')
    parts = [p for p in (t, a) if p]
    return '_'.join(parts) if parts else 'col'


# ── transformation ───────────────────────────────────────────────────────────

def _build_env(trafo_params) -> dict:
    env = {}
    for var in trafo_params:
        name = VariableName.Name(var.name)
        which = var.WhichOneof('value')
        if which == 'double_value':
            env[name] = var.double_value
        elif which == 'float_value':
            env[name] = float(var.float_value)
        elif which == 'int_value':
            env[name] = float(var.int_value)
    return env


def _apply_transform(data: np.ndarray, md) -> np.ndarray:
    if md.transformation == Transformation.NO_TRANSFORMATION:
        return data
    if md.transformation == Transformation.ADC_STEP_TO_FORCE_N:
        env = _build_env(md.trafo_params)
        volts = adc_to_volt(data, env.get('ADC_V_PER_STEP', 1.0))
        return volt_to_force(
            volts,
            zero_mv_v=env.get('FT_ZERO_SIGNAL_MV_PER_V', 0.0),
            supply_v=env.get('FT_SUPPLY_V', 1.0),
            rated_mv_v=env.get('FT_RATED_LOAD_MV_PER_V', 1.0),
            full_scale_n=env.get('FT_FULL_SCALE_N', 1.0),
        )
    return data  # unknown transformation — pass through


def _scale_factor(md) -> float:
    which = md.WhichOneof('post_unit_scaling')
    factor = 1.0
    if which == 'float_factor':
        factor = float(md.float_factor)
    elif which == 'double_factor':
        factor = float(md.double_factor)
    elif which == 'int_factor':
        factor = float(md.int_factor)
    exp = getattr(md, 'si_unit_scaling_base10_exp', 0)
    if exp:
        factor *= 10 ** exp
    return factor


# ── public dataclasses ───────────────────────────────────────────────────────

@dataclass
class FrameData:
    device_id: str          # MAC string "aa:bb:cc:dd:ee:ff"
    stream_uid: int
    seq: int
    timestamp_ns: int       # device timestamp in nanoseconds
    recv_time_ns: int       # host recv time
    sample_rate_hz: float
    columns: Dict[str, np.ndarray] = field(default_factory=dict)
    units:   Dict[str, str]        = field(default_factory=dict)  # label → unit string


@dataclass
class FFTFrameData:
    device_id: str
    stream_uid: int
    seq: int
    timestamp_ns: int
    recv_time_ns: int
    fft_bins: int                          # bins per axis
    fft_size: int                          # = fft_bins * 2
    freq_hz: list                          # frequency axis [0..fft_bins-1]
    magnitudes: Dict[str, list]            # {'x': [...], 'y': [...], 'z': [...]}
    psd: Dict[str, list]                   # PSD: {'x': [...], ...} in (unit)²/Hz
    unit: str


# ── decoder ──────────────────────────────────────────────────────────────────

def _device_id_str(raw: bytes) -> str:
    if isinstance(raw, (bytes, bytearray)) and len(raw) == 6:
        return ':'.join(f'{b:02x}' for b in raw)
    return raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)


# Track which streams we've already logged MetaData for.
_logged_streams: set = set()

_sample_rate = 26667.0  # Updated from FrameData when available


def decode_frame(payload_bytes: bytes, recv_time_ns: int) -> 'FrameData | None':
    """
    Parse a raw TCP protobuf frame. Returns a FrameData or None if the
    message is not a FrameStream (e.g. FFT, Log).
    """
    header = message_pb2.Header()
    header.ParseFromString(payload_bytes)

    if not header.HasField('frame_stream'):
        return None

    payload_msg = message_pb2.Payload()
    payload_msg.ParseFromString(payload_bytes)
    raw_payload = payload_msg.payload

    fs = header.frame_stream
    frame_count = fs.frame_count

    # Device timestamp → ns
    ts = header.device_timestamp
    timestamp_ns = int(ts.seconds) * 1_000_000_000 + int(ts.nanos)

    # Sample rate
    global _sample_rate
    rate_hz = 0.0
    if fs.HasField('actual_frequency_hz'):
        rate_hz = fs.actual_frequency_hz
    elif fs.HasField('target_frequency_hz'):
        rate_hz = fs.target_frequency_hz
    if rate_hz > 0:
        _sample_rate = rate_hz
    device_id = _device_id_str(header.device_id)
    stream_uid = header.stream_uid

    # Log MetaData once per stream so we can inspect scaling fields.
    stream_key = (device_id, stream_uid)
    _is_new_stream = stream_key not in _logged_streams
    if _is_new_stream:
        _logged_streams.add(stream_key)
        _log.info('New stream %s uid=%d  rate=%.1f Hz  %d columns:',
                  device_id, stream_uid, rate_hz, len(fs.meta_data))
        for i, md in enumerate(fs.meta_data):
            which  = md.WhichOneof('post_unit_scaling')
            factor = getattr(md, which) if which else None
            exp    = md.si_unit_scaling_base10_exp
            unit   = _UNIT_STR.get(md.data_unit, f'unit#{md.data_unit}')
            trafo  = Transformation.Name(md.transformation)
            _log.debug('  col[%d] %-20s  dtype=%s%d  unit=%-12s  '
                       'factor=%s  exp=%d  trafo=%s',
                       i, _col_label(md),
                       _NUMPY_KIND.get(md.data_numpy_type, '?'), md.data_numpy_bytes,
                       unit, factor, exp, trafo)

    # Decode columns
    columns:      Dict[str, np.ndarray] = {}
    units:        Dict[str, str]        = {}
    label_counts: Dict[str, int]        = {}
    offset = 0

    for md in fs.meta_data:
        kind   = _NUMPY_KIND.get(md.data_numpy_type, 'u')
        endian = _NUMPY_ENDIAN.get(md.data_numpy_endian, '<')
        dtype  = np.dtype(f'{endian}{kind}{md.data_numpy_bytes}')
        nbytes = md.data_numpy_bytes * frame_count

        chunk = raw_payload[offset:offset + nbytes]
        offset += nbytes

        raw = np.frombuffer(chunk, dtype=dtype).copy().astype(np.float64)
        arr = _apply_transform(raw, md)
        sf = _scale_factor(md)
        arr = arr * sf

        label = _col_label(md)

        # Log raw vs scaled for first frame of each new stream
        if _is_new_stream and md.data_type == DataType.DATA_TYPE_ACCELERATION and len(raw) > 0:
            _log.debug('  %s  raw[0]=%.1f  scaled[0]=%.6f  factor=%.6e',
                       label, raw[0], arr[0], sf)
        count = label_counts.get(label, 0)
        label_counts[label] = count + 1
        if count > 0:
            label = f'{label}_{count}'

        columns[label] = arr
        units[label]   = _UNIT_STR.get(md.data_unit, '')

    return FrameData(
        device_id=device_id,
        stream_uid=stream_uid,
        seq=header.sequence_number,
        timestamp_ns=timestamp_ns,
        recv_time_ns=recv_time_ns,
        sample_rate_hz=rate_hz,
        columns=columns,
        units=units,
    )


# ── FFT decoder ──────────────────────────────────────────────────────────────

def decode_fft_frame(payload_bytes: bytes, recv_time_ns: int) -> 'FFTFrameData | None':
    """
    Parse a raw TCP protobuf FFT frame. Returns FFTFrameData or None.
    Caller should verify header.HasField('fft_stream') before calling,
    or use decode_any() which dispatches automatically.
    """
    header = message_pb2.Header()
    header.ParseFromString(payload_bytes)

    if not header.HasField('fft_stream'):
        return None

    payload_msg = message_pb2.Payload()
    payload_msg.ParseFromString(payload_bytes)
    raw_payload = payload_msg.payload

    fft = header.fft_stream
    fft_bins = fft.fft_bins
    fft_size = fft_bins * 2

    if fft_bins == 0:
        return None

    # Device timestamp → ns
    ts = header.device_timestamp
    timestamp_ns = int(ts.seconds) * 1_000_000_000 + int(ts.nanos)

    device_id = _device_id_str(header.device_id)
    stream_uid = header.stream_uid

    # Log once per FFT stream
    stream_key = (device_id, stream_uid, 'fft')
    if stream_key not in _logged_streams:
        _logged_streams.add(stream_key)
        _log.info('New FFT stream %s uid=%d  fft_size=%d  bins=%d  rate=%.1f Hz  %d meta cols',
                  device_id, stream_uid, fft_size, fft_bins,
                  fft.actual_frame_rate_hz, len(fft.meta_data))

    # Parse 3 x fft_bins int16 magnitudes from raw payload
    expected = 3 * fft_bins * 2
    if len(raw_payload) < expected:
        _log.warning('FFT payload too short: %d < %d', len(raw_payload), expected)
        return None

    all_mags = np.frombuffer(raw_payload[:expected], dtype='<i2').astype(np.float64)

    # Build per-axis scaling from MetaData, apply fft_size * float_factor * 10^exp
    # Map axis from MetaData.data_axis → 'x'/'y'/'z'
    axis_names = []
    scale_factors = []
    unit = 'm/s²'
    for md in fft.meta_data:
        axis_label = _DATA_AXIS_STR.get(md.data_axis, 'x')
        axis_names.append(axis_label)
        sf = _scale_factor(md) * fft_size  # Q15 compensation
        scale_factors.append(sf)
        u = _UNIT_STR.get(md.data_unit, '')
        if u:
            unit = u

    # Fallback if meta_data has fewer than 3 entries
    while len(axis_names) < 3:
        axis_names.append(['x', 'y', 'z'][len(axis_names)])
        scale_factors.append(scale_factors[-1] if scale_factors else 1.0)

    magnitudes: Dict[str, list] = {}
    psd: Dict[str, list] = {}
    # Prefer _sample_rate from raw FrameData (actual measured rate) over the
    # FFT header's nominal rate, which firmware may not update correctly.
    fft_rate = _sample_rate if _sample_rate != 26667.0 else (
        fft.actual_frame_rate_hz if fft.actual_frame_rate_hz > 0 else _sample_rate
    )
    # PSD normalisation: freq_resolution = fs / fft_size
    freq_res = fft_rate / fft_size  # Hz per bin
    for i, (name, sf) in enumerate(zip(axis_names, scale_factors)):
        raw_slice = all_mags[i * fft_bins:(i + 1) * fft_bins]
        scaled = raw_slice * sf
        magnitudes[name] = scaled.tolist()
        # PSD = magnitude² / freq_resolution  → (unit)²/Hz
        psd[name] = (scaled * scaled / freq_res).tolist()

    # Frequency axis: f[k] = k * sample_rate / fft_size
    freq_hz = [k * fft_rate / fft_size for k in range(fft_bins)]

    return FFTFrameData(
        device_id=device_id,
        stream_uid=stream_uid,
        seq=header.sequence_number,
        timestamp_ns=timestamp_ns,
        recv_time_ns=recv_time_ns,
        fft_bins=fft_bins,
        fft_size=fft_size,
        freq_hz=freq_hz,
        magnitudes=magnitudes,
        psd=psd,
        unit=unit,
    )


# ── unified dispatcher ───────────────────────────────────────────────────────

def decode_any(payload_bytes: bytes, recv_time_ns: int) -> 'FrameData | FFTFrameData | None':
    """Parse header once, dispatch to decode_frame or decode_fft_frame."""
    header = message_pb2.Header()
    header.ParseFromString(payload_bytes)

    which = header.WhichOneof('specific_header')
    if which == 'frame_stream':
        return decode_frame(payload_bytes, recv_time_ns)
    elif which == 'fft_stream':
        return decode_fft_frame(payload_bytes, recv_time_ns)
    return None
