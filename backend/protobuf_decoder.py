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
    # Vector magnitude axes (proto3 passes unknown enum values as integers)
    10:                              'xy_vec',
    11:                              'xz_vec',
    12:                              'yz_vec',
    13:                              'xyz_vec',
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
    no_time_sync: bool = False      # Header.flags FLAG_NO_TIME_SYNC — clock not disciplined
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
    no_time_sync: bool = False             # Header.flags FLAG_NO_TIME_SYNC — clock not disciplined
    legacy_scaling: bool = False           # bins re-normalised from pre-0x1033 firmware


# ── decoder ──────────────────────────────────────────────────────────────────

def _device_id_str(raw: bytes) -> str:
    if isinstance(raw, (bytes, bytearray)) and len(raw) == 6:
        return ':'.join(f'{b:02x}' for b in raw)
    return raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)


# Track which streams we've already logged MetaData for.
_logged_streams: set = set()

_sample_rate = 26667.0  # Updated from FrameData when available


# ── FFT magnitude normalisation across the 0x1033 firmware boundary ──────────
#
# Firmware 0x1033 normalised both FFT transports onto one physically defined
# scale: a tone of amplitude A LSB reads A LSB, for every fft_size and both
# precisions (firmware note 2026-08-06). `float_factor × 10^exp` from the
# frame metadata is therefore the *whole* conversion to SI — no host-side fudge.
#
# Older firmware shipped un-normalised magnitudes, differently per transport:
#   Q15     — 8× low, size-independent (CMSIS `arm_rfft_q15`'s internal 1/N plus
#             `arm_cmplx_mag_q15`'s 2.14 output gave an implicit 1/(2N))
#   float32 — N/4× high (`arm_rfft_fast_f32` output was never normalised)
# and neither halved bin 0. Nothing on the wire distinguishes the two eras, so
# we key off the firmware version read through the config API (Get Info). A
# device we have not queried is taken at the documented word — already
# normalised — so the correct-by-spec path is the default and the compensation
# only ever engages on a version we positively know to be old.
FFT_NORM_FW = 0x1033

_fw_version: Dict[str, int] = {}

# Last FFT scaling mode logged per device, so the console reports it once and
# again on change rather than every frame.
_fft_mode_logged: Dict[str, str] = {}


def note_firmware_version(mac: str, version: int) -> None:
    """Record a device's firmware version (from get_sensor_info). Keyed by MAC
    in the same 'aa:bb:cc:dd:ee:ff' form as FrameData.device_id."""
    if isinstance(version, int) and version > 0:
        _fw_version[mac.lower()] = version


def fft_scaling_mode(mac: str) -> str:
    """'legacy_compensated' if this device's FFT bins need re-normalising onto
    the 0x1033 scale, else 'normalised' (also when the version is unknown)."""
    v = _fw_version.get(mac.lower())
    return 'legacy_compensated' if v is not None and v < FFT_NORM_FW else 'normalised'


def _legacy_bin_gain(is_float: bool, fft_bins: int, fft_size: int) -> np.ndarray:
    """Per-bin factor taking pre-0x1033 magnitudes onto the 0x1033 scale:
    ×8 (Q15) or ×4/N (float32), and half that at bin 0 — 0x1033 halves DC,
    because the factor of two in its normalisation accounts for a real tone's
    energy splitting across ±f, which does not apply to a constant offset."""
    g = (4.0 / fft_size) if is_float else 8.0
    gain = np.full(fft_bins, g)
    gain[0] = g * 0.5
    return gain


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
        no_time_sync=bool(header.flags & message_pb2.Flags.FLAG_NO_TIME_SYNC),
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

    mode = fft_scaling_mode(device_id)
    legacy = mode == 'legacy_compensated'

    # Log once per FFT stream
    stream_key = (device_id, stream_uid, 'fft')
    if stream_key not in _logged_streams:
        _logged_streams.add(stream_key)
        _log.info('New FFT stream %s uid=%d  fft_size=%d  bins=%d  rate=%.1f Hz  %d meta cols',
                  device_id, stream_uid, fft_size, fft_bins,
                  fft.actual_frame_rate_hz, len(fft.meta_data))
    # Announce the magnitude scale in use, and again whenever it changes — a
    # Get Info mid-stream is what tells us a device is on pre-0x1033 firmware,
    # so compensation can engage after the stream is already running.
    if _fft_mode_logged.get(device_id) != mode:
        _fft_mode_logged[device_id] = mode
        if legacy:
            _log.warning('FFT %s: firmware 0x%04x predates 0x%04x — re-normalising '
                         'magnitudes onto the 0x%04x amplitude scale',
                         device_id, _fw_version.get(device_id, 0), FFT_NORM_FW, FFT_NORM_FW)
        else:
            _log.info('FFT %s: magnitudes taken as normalised (firmware >= 0x%04x '
                      'or version unknown)', device_id, FFT_NORM_FW)

    # Build per-axis column descriptors from MetaData.
    #
    # Columns are concatenated in metadata order. Each column carries its own
    # dtype via data_numpy_type / data_numpy_bytes / data_numpy_endian, so we
    # must NOT assume a fixed 2 bytes/bin: the firmware may ship Q15 signed
    # int16 (2 B), Q15 unsigned uint16 (vector modes, 2 B), or IEEE-754 float32
    # (4 B, ~144 dB dynamic range). Different axes could in principle differ,
    # so each column is decoded on its own dtype.
    #
    # descriptor tuple: (axis_name, np.dtype, bytes_per_bin, scale_factor, legacy_gain)
    columns: list = []
    unit = 'm/s²'
    for md in fft.meta_data:
        name = _DATA_AXIS_STR.get(md.data_axis, 'x')
        kind = _NUMPY_KIND.get(md.data_numpy_type, 'i')          # default int16 (legacy Q15 signed)
        endian = _NUMPY_ENDIAN.get(md.data_numpy_endian, '<')
        per_bin = md.data_numpy_bytes or 2
        dtype = np.dtype(f'{endian}{kind}{per_bin}')
        is_float = (md.data_numpy_type == NumpyType.NUMPY_TYPE_FLOATING_POINT)
        # From 0x1033 the metadata's own conversion is the whole story:
        #   value_SI = raw × float_factor × 10^si_unit_scaling_base10_exp
        # for both precisions and every fft_size. No ×fft_size, no per-precision
        # special case — see the FFT_NORM_FW block above for what the two older
        # transports did instead and how a known-old device is compensated.
        sf = _scale_factor(md)
        legacy_gain = _legacy_bin_gain(is_float, fft_bins, fft_size) if legacy else None
        u = _UNIT_STR.get(md.data_unit, '')
        if u:
            unit = u
        columns.append((name, dtype, per_bin, sf, legacy_gain))

    if not columns:
        # Legacy fallback: no metadata → assume int16 Q15 columns for x/y/z,
        # derived from payload length. Firmware old enough to omit metadata
        # predates 0x1033 unconditionally, so its bins always need the Q15
        # re-normalisation; with no float_factor to read there is no SI
        # conversion available, so these stay in raw LSB.
        default_axes = ['x', 'y', 'z']
        n = len(raw_payload) // (fft_bins * 2)
        gain = _legacy_bin_gain(False, fft_bins, fft_size)
        for i in range(n):
            name = default_axes[i] if i < len(default_axes) else f'ch{i}'
            columns.append((name, np.dtype('<i2'), 2, 1.0, gain))

    # Prefer _sample_rate from raw FrameData (actual measured rate) over the
    # FFT header's nominal rate, which firmware may not update correctly.
    fft_rate = _sample_rate if _sample_rate != 26667.0 else (
        fft.actual_frame_rate_hz if fft.actual_frame_rate_hz > 0 else _sample_rate
    )
    # One-sided power spectral density, still matching
    #   scipy.signal.periodogram(x, fs, window='hann', scaling='density')
    # but expressed on the 0x1033 amplitude spectrum: `scaled` (below) is the
    # physical amplitude a_k, not the raw windowed-DFT magnitude |X_k|, so the
    # Σw² normalisation folds into the window's ENBW (1.5 bins for Hann):
    #     S_k = a_k² / (2 · ENBW · Δf) = a_k² / (3Δf)
    # Identical to 2·|X_k|²/(fs·Σw²) after substituting a_k = |X_k|·4/N and
    # Σw² = 3N/8 for a periodic Hann — the firmware note's PSD recipe and the
    # periodogram definition agree exactly.
    delta_f = fft_rate / fft_size                 # bin width
    psd_scale = 1.0 / (3.0 * delta_f)             # = 1/(2·ENBW·Δf), ENBW = 1.5 bins
    psd_bin_scale = np.full(fft_bins, psd_scale)
    # DC gets ×2, not ÷2: the periodogram does not double bin 0, but the
    # firmware already halved its *amplitude*, and undoing that in power costs
    # a factor of four — net ×2 relative to the other bins.
    psd_bin_scale[0] = psd_scale * 2.0
    # (Nyquist is not present — fft_bins = N/2 covers k = 0..N/2-1.)

    magnitudes: Dict[str, list] = {}
    psd: Dict[str, list] = {}
    # Walk the payload column by column at a running offset — firmware may send
    # fewer axes than meta_data entries (e.g. single-axis mode with 3 metadata).
    offset = 0
    for name, dtype, per_bin, sf, legacy_gain in columns:
        col_bytes = fft_bins * per_bin
        if col_bytes <= 0 or offset + col_bytes > len(raw_payload):
            break
        raw_slice = np.frombuffer(raw_payload[offset:offset + col_bytes], dtype=dtype).astype(np.float64)
        offset += col_bytes
        scaled = raw_slice * sf
        if legacy_gain is not None:
            scaled = scaled * legacy_gain      # pre-0x1033 → 0x1033 amplitude scale
        magnitudes[name] = scaled.tolist()
        # PSD = a_k² / (2·ENBW·Δf)  → (unit)²/Hz  (one-sided, Hann ENBW = 1.5)
        psd[name] = (scaled * scaled * psd_bin_scale).tolist()

    if not magnitudes:
        _log.warning('FFT payload too short for even 1 axis: %d bytes, %d bins',
                     len(raw_payload), fft_bins)
        return None

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
        no_time_sync=bool(header.flags & message_pb2.Flags.FLAG_NO_TIME_SYNC),
        legacy_scaling=legacy,
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
