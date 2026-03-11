"""
Full-rate TSV logger.
One file per (device_id, stream_uid); header written on first packet.
Columns: delta_time + acceleration axes (auto-detected from MetaData labels).
Filename: {mac}_{uid}_{YYYYMMDD_HHMMSS}.log
"""

import os
from datetime import datetime
from typing import Dict, IO

from protobuf_decoder import FrameData


class LogWriter:
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

        # Each column is an array of length frame_count; write row per sample.
        n = max((len(frame.columns[c]) for c in cols if c in frame.columns), default=0)
        for i in range(n):
            row = []
            for c in cols:
                arr = frame.columns.get(c)
                if arr is not None and i < len(arr):
                    row.append(f'{arr[i]:.9g}')
                else:
                    row.append('')
            fh.write('\t'.join(row) + '\n')

    def _open(self, frame: FrameData, key: tuple):
        os.makedirs(self.output_dir, exist_ok=True)

        mac = frame.device_id.replace(':', '')
        ts  = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f'{mac}_{frame.stream_uid}_{ts}.log'
        path = os.path.join(self.output_dir, fname)

        fh = open(path, 'w', buffering=1)  # line-buffered

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
