"""
HDF5 logger for full-rate sensor data.
One file per (device_id, stream_uid); datasets created on first packet.
Filename: {mac}_{uid}_{YYYYMMDD_HHMMSS}.h5

Structure:
    /data/<col>     — float32 datasets, chunked + gzip compressed
    root attrs:     device_id, stream_uid, sample_rate_hz, created_at
    dataset attrs:  unit
"""

import os
from datetime import datetime
from typing import Dict, Optional

import h5py
import numpy as np

from protobuf_decoder import FrameData, FFTFrameData


class Hdf5Writer:
    def __init__(self, output_dir: str = '.'):
        self.output_dir = output_dir
        self._files: Dict[tuple, h5py.File] = {}
        self._headers: Dict[tuple, list] = {}
        self._fft_init: Dict[tuple, bool] = {}  # whether /fft group exists

    def write(self, frame: FrameData):
        key = (frame.device_id, frame.stream_uid)

        if key not in self._files:
            self._open(frame, key)

        hf = self._files[key]
        cols = self._headers[key]
        grp = hf['data']

        for col in cols:
            arr = frame.columns.get(col)
            if arr is None or len(arr) == 0:
                continue
            ds = grp[col]
            old_len = ds.shape[0]
            ds.resize((old_len + len(arr),))
            ds[old_len:] = arr.astype(np.float32)

    def _open(self, frame: FrameData, key: tuple):
        os.makedirs(self.output_dir, exist_ok=True)

        mac = frame.device_id.replace(':', '')
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f'{mac}_{frame.stream_uid}_{ts}.h5'
        path = os.path.join(self.output_dir, fname)

        hf = h5py.File(path, 'w')

        # Root attributes
        hf.attrs['device_id'] = frame.device_id
        hf.attrs['stream_uid'] = frame.stream_uid
        hf.attrs['sample_rate_hz'] = frame.sample_rate_hz
        hf.attrs['created_at'] = ts

        # Determine column order: dt first, then accel axes, then rest
        all_cols = list(frame.columns.keys())
        ordered = []
        for prefix in ('dt', 'accel_x', 'accel_y', 'accel_z'):
            if prefix in all_cols:
                ordered.append(prefix)
        for c in all_cols:
            if c not in ordered:
                ordered.append(c)

        # Create datasets
        grp = hf.create_group('data')
        for col in ordered:
            ds = grp.create_dataset(
                col,
                shape=(0,),
                maxshape=(None,),
                dtype=np.float32,
                chunks=(16384,),
                compression='gzip',
                compression_opts=4,
            )
            unit = frame.units.get(col, '')
            if unit:
                ds.attrs['unit'] = unit

        self._headers[key] = ordered
        self._files[key] = hf

    def write_fft(self, fft: FFTFrameData):
        """Append one FFT snapshot. Stored as /fft/mag_<axis> and /fft/psd_<axis>."""
        key = (fft.device_id, fft.stream_uid)

        if key not in self._files:
            # No raw data file open yet — skip FFT (we need a file first)
            return

        hf = self._files[key]

        # Create /fft group and datasets on first FFT frame
        if key not in self._fft_init:
            grp = hf.require_group('fft')
            grp.attrs['fft_bins'] = fft.fft_bins
            grp.attrs['fft_size'] = fft.fft_size
            grp.attrs['unit'] = fft.unit
            # Store frequency axis once
            grp.create_dataset('freq_hz', data=np.array(fft.freq_hz, dtype=np.float32))

            for axis in fft.magnitudes:
                nbins = len(fft.magnitudes[axis])
                grp.create_dataset(
                    f'mag_{axis}', shape=(0, nbins), maxshape=(None, nbins),
                    dtype=np.float32, chunks=(64, nbins),
                    compression='gzip', compression_opts=4,
                )
            for axis in fft.psd:
                nbins = len(fft.psd[axis])
                grp.create_dataset(
                    f'psd_{axis}', shape=(0, nbins), maxshape=(None, nbins),
                    dtype=np.float32, chunks=(64, nbins),
                    compression='gzip', compression_opts=4,
                )
            self._fft_init[key] = True

        grp = hf['fft']
        for axis, data in fft.magnitudes.items():
            ds_name = f'mag_{axis}'
            if ds_name in grp:
                ds = grp[ds_name]
                n = ds.shape[0]
                ds.resize((n + 1, ds.shape[1]))
                ds[n, :] = np.array(data, dtype=np.float32)
        for axis, data in fft.psd.items():
            ds_name = f'psd_{axis}'
            if ds_name in grp:
                ds = grp[ds_name]
                n = ds.shape[0]
                ds.resize((n + 1, ds.shape[1]))
                ds[n, :] = np.array(data, dtype=np.float32)

    def close_all(self):
        for hf in self._files.values():
            hf.close()
        self._files.clear()
        self._headers.clear()
        self._fft_init.clear()

    @property
    def is_logging(self) -> bool:
        return bool(self._files)
