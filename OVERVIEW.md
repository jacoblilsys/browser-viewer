# Lillie System — Browser-Based Sensor Viewer

A real-time browser application for monitoring and configuring vibration sensors over a local network. Built on open-source technologies — no proprietary software or licensing required.

## What It Does

- **Live waveform display** of 3-axis acceleration data at up to 26.7 kHz sample rate
- **FFT spectrum analysis** computed on-sensor and streamed alongside raw data, in Q15 or high-dynamic-range **float32** precision
- **Power Spectral Density (PSD)** calculated in real time from FFT magnitudes
- **TCP or UDP transport** — the raw-sample and FFT streams can each be delivered over TCP (reliable) or UDP (chunked, low-latency), reassembled and decoded transparently
- **Log-scale spectra** — per-window log magnitude toggle to surface small signals many orders of magnitude below the peaks (needed to see float32's range)
- **Flexible chart layout** with draggable, resizable floating windows — create any combination of raw, FFT, and PSD views
- **Sensor configuration** — full scale, sample rate, filter, FFT size — all adjustable from the browser
- **Network configuration** — IP, gateway, DHCP, NTP, data stream toggle
- **Automatic sensor discovery** via mDNS on the local network
- **Data logging** to TSV or HDF5 files for offline analysis

## Architecture

| Component | Technology |
|-----------|------------|
| Backend   | Python, FastAPI, WebSocket |
| Frontend  | Vanilla JS, uPlot charts |
| Protocol  | Protobuf over TCP or UDP (data), UDP multicast (config) |
| Discovery | mDNS / Zeroconf |

The sensor streams length-prefixed protobuf frames over TCP, or over UDP as 12-byte-header chunks that the backend reassembles into the same frames. The backend decodes, downsamples for display (60 Hz waveform envelope, 30 Hz FFT snapshots), and pushes JSON over WebSocket to any number of connected browsers. FFT magnitudes are decoded per-column from their metadata (Q15 int16 or float32). Sensor configuration uses a UDP multicast protocol with HMAC-MD5 authentication.

## Open Source

The entire viewer — backend, frontend, and protobuf definitions — is open source. No dependencies on closed-source tools. Runs on any machine with Python 3.10+ and a modern browser.
