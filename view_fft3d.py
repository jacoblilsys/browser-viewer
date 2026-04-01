#!/usr/bin/env python3
"""
3D FFT Waterfall Viewer — plots FFT magnitude over time from an HDF5 file.

Usage:
    python view_fft3d.py <file.h5> [--axis x|y|z] [--psd] [--max-frames 500]

Requires: pip install matplotlib h5py numpy
"""

import argparse
import sys

import h5py
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D


def main():
    parser = argparse.ArgumentParser(description='3D FFT waterfall viewer for HDF5 files')
    parser.add_argument('file', help='HDF5 file path')
    parser.add_argument('--axis', '-a', default='x', choices=['x', 'y', 'z'],
                        help='Which axis to plot (default: x)')
    parser.add_argument('--psd', action='store_true',
                        help='Plot PSD instead of magnitude')
    parser.add_argument('--max-frames', '-n', type=int, default=500,
                        help='Max number of FFT frames to plot (default: 500)')
    parser.add_argument('--log', action='store_true',
                        help='Use log scale for magnitude/PSD')
    parser.add_argument('--colormap', '-c', default='viridis',
                        help='Matplotlib colormap (default: viridis)')
    args = parser.parse_args()

    f = h5py.File(args.file, 'r')

    if 'fft' not in f:
        print('No /fft group in this file. Was FFT streaming enabled during logging?')
        sys.exit(1)

    fft_grp = f['fft']
    prefix = 'psd' if args.psd else 'mag'
    ds_name = f'{prefix}_{args.axis}'

    if ds_name not in fft_grp:
        available = [k for k in fft_grp.keys() if k.startswith(prefix)]
        print(f'Dataset "{ds_name}" not found. Available: {available}')
        sys.exit(1)

    freq = fft_grp['freq_hz'][:]
    data = fft_grp[ds_name][:]
    unit = fft_grp.attrs.get('unit', 'm/s²')
    sample_rate = f.attrs.get('sample_rate_hz', 0)
    fft_size = fft_grp.attrs.get('fft_size', 0)

    print(f'File: {args.file}')
    print(f'Dataset: {ds_name}  shape={data.shape}  ({data.shape[0]} frames × {data.shape[1]} bins)')
    print(f'Frequency range: {freq[0]:.0f} – {freq[-1]:.0f} Hz')
    if sample_rate:
        print(f'Sample rate: {sample_rate:.1f} Hz')

    # Limit frames for performance
    if data.shape[0] > args.max_frames:
        step = data.shape[0] // args.max_frames
        data = data[::step]
        print(f'Downsampled to {data.shape[0]} frames (step={step})')

    if args.log:
        data = np.log10(np.maximum(data, 1e-10))

    n_frames, n_bins = data.shape

    # Build meshgrid
    frame_idx = np.arange(n_frames)
    freq_grid, frame_grid = np.meshgrid(freq[:n_bins], frame_idx)

    # ── Plot 1: 3D surface ──
    fig1 = plt.figure(figsize=(14, 8))
    ax1 = fig1.add_subplot(111, projection='3d')
    ax1.plot_surface(freq_grid, frame_grid, data,
                     cmap=args.colormap, linewidth=0, antialiased=False,
                     rcount=min(n_frames, 200), ccount=min(n_bins, 200))
    ax1.set_xlabel('Frequency (Hz)')
    ax1.set_ylabel('Frame')
    z_label = f'{"PSD" if args.psd else "Magnitude"} ({"log " if args.log else ""}{unit})'
    ax1.set_zlabel(z_label)
    ax1.set_title(f'FFT {"PSD" if args.psd else "Magnitude"} — {args.axis.upper()} axis')
    ax1.view_init(elev=30, azim=-60)

    # ── Plot 2: 2D spectrogram (heatmap) ──
    fig2, ax2 = plt.subplots(figsize=(14, 6))
    extent = [freq[0], freq[-1], 0, n_frames]
    im = ax2.imshow(data, aspect='auto', origin='lower', extent=extent,
                    cmap=args.colormap, interpolation='nearest')
    ax2.set_xlabel('Frequency (Hz)')
    ax2.set_ylabel('Frame')
    ax2.set_title(f'Spectrogram — {args.axis.upper()} axis')
    plt.colorbar(im, ax=ax2, label=z_label)

    f.close()

    plt.tight_layout()
    plt.show()


if __name__ == '__main__':
    main()
