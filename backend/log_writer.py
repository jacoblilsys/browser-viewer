"""
Log manager — supports TSV and HDF5 formats.

TsvWriter:  Full-rate TSV logger (.log files).
LogManager: Wraps TsvWriter / Hdf5Writer, provides format switching.
"""

import os
from datetime import datetime
from typing import Dict, IO, Optional

from protobuf_decoder import FrameData


# ── TSV writer (original LogWriter) ──────────────────────────────────────────

class TsvWriter:
    def __init__(self, output_dir: str = '.'):
        self.output_dir = output_dir
        self._files: Dict[tuple, IO] = {}
        self._headers: Dict[tuple, list] = {}

    def write(self, frame: FrameData):
        key = (frame.device_id, frame.stream_uid)

        if key not in self._files:
            self._open(frame, key)

        fh = self._files[key]
        cols = self._headers[key]

        # Build all rows as a single string to minimise I/O calls.
        arrays = [frame.columns.get(c) for c in cols]
        n = max((len(a) for a in arrays if a is not None), default=0)
        if n == 0:
            return
        lines = []
        for i in range(n):
            row = []
            for a in arrays:
                if a is not None and i < len(a):
                    row.append(f'{a[i]:.9g}')
                else:
                    row.append('')
            lines.append('\t'.join(row))
        fh.write('\n'.join(lines) + '\n')

    def _open(self, frame: FrameData, key: tuple):
        os.makedirs(self.output_dir, exist_ok=True)

        mac = frame.device_id.replace(':', '')
        ts  = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f'{mac}_{frame.stream_uid}_{ts}.log'
        path = os.path.join(self.output_dir, fname)

        fh = open(path, 'w', buffering=128 * 1024)  # 128KB buffer

        # Determine column order: dt first, then accel axes, then rest
        all_cols = list(frame.columns.keys())
        ordered = []
        for prefix in ('dt', 'accel_x', 'accel_y', 'accel_z'):
            if prefix in all_cols:
                ordered.append(prefix)
        for c in all_cols:
            if c not in ordered:
                ordered.append(c)

        self._headers[key] = ordered
        self._files[key] = fh
        fh.write('\t'.join(ordered) + '\n')

    def close_all(self):
        for fh in self._files.values():
            fh.close()
        self._files.clear()
        self._headers.clear()

    @property
    def is_logging(self) -> bool:
        return bool(self._files)


# ── Log manager (format-switching wrapper) ───────────────────────────────────

class LogManager:
    """Wraps TsvWriter / Hdf5Writer. Format can be changed between sessions."""

    def __init__(self, output_dir: str = '.', fmt: str = 'tsv'):
        self.output_dir = output_dir
        self._fmt = fmt
        self._writer: Optional[object] = None

    @property
    def fmt(self) -> str:
        return self._fmt

    def set_format(self, fmt: str):
        """Switch format. Takes effect on the next logging session."""
        if fmt not in ('tsv', 'hdf5'):
            raise ValueError(f'Unknown log format: {fmt}')
        self._fmt = fmt

    def write(self, frame: FrameData):
        if self._writer is None:
            self._writer = self._create_writer()
        self._writer.write(frame)

    def close_all(self):
        if self._writer is not None:
            self._writer.close_all()
            self._writer = None

    @property
    def is_logging(self) -> bool:
        return self._writer is not None and self._writer.is_logging

    def _create_writer(self):
        if self._fmt == 'hdf5':
            from hdf5_writer import Hdf5Writer
            return Hdf5Writer(output_dir=self.output_dir)
        return TsvWriter(output_dir=self.output_dir)
