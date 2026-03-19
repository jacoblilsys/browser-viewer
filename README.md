# Browser Viewer

Real-time browser-based vibration sensor viewer. Receives protobuf-framed acceleration data over TCP, displays live waveforms and FFT/PSD spectra, and provides sensor configuration via a web UI.

## Requirements

- Python 3.10+
- Packages listed in `requirements.txt`

## Quick Start

```bash
cd Utils/browser_viewer/backend

# Create virtual environment (first time only)
python3 -m venv ~/venv-browser-viewer
source ~/venv-browser-viewer/bin/activate
pip install -r ../requirements.txt

# Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` in a browser.

## Configuration

All settings are via environment variables. Defaults are sensible for typical use.

| Variable   | Default  | Description                                         |
|------------|----------|-----------------------------------------------------|
| `TCP_PORT` | `8066`   | TCP port the backend listens on for sensor data     |
| `LOG_DIR`  | `./logs` | Directory where log files (TSV/HDF5) are written    |
| `WS_FPS`   | `60`     | WebSocket broadcast rate in frames per second        |

### Examples

```bash
# Default settings (60 Hz broadcast, TCP port 8066)
uvicorn main:app --host 0.0.0.0 --port 8000

# Higher broadcast rate for smoother graphs
WS_FPS=120 uvicorn main:app --host 0.0.0.0 --port 8000

# Custom TCP port and log directory
TCP_PORT=9000 LOG_DIR=/data/logs uvicorn main:app --host 0.0.0.0 --port 8000

# Auto-reload during development
uvicorn main:app --reload --port 8000
```

### Broadcast rate (`WS_FPS`)

The backend accumulates full-rate samples (e.g. 26.7 kHz) and sends a downsampled summary (min/max/last per axis) to the browser at `WS_FPS` frames per second.

| WS_FPS | Decimation at 26.7 kHz | WS bandwidth | Notes                        |
|--------|------------------------|--------------|------------------------------|
| 30     | ~887:1                 | ~6 KB/s      | Low CPU, coarser display     |
| 60     | ~443:1                 | ~12 KB/s     | Default, smooth for most use |
| 120    | ~222:1                 | ~24 KB/s     | Smoother, higher CPU         |

## Architecture

```
┌──────────┐  TCP/protobuf  ┌──────────────┐  asyncio.Queue  ┌───────────┐
│  Sensor  │ ──────────────►│NetworkReceiver│ ──────────────► │drain_queue│
└──────────┘   port 8066    └──────────────┘                  └─────┬─────┘
                                                                    │
                                                          ┌─────────┼─────────┐
                                                          ▼         ▼         ▼
                                                    Broadcaster  LogWriter  Status
                                                     (WS_FPS)   (TSV/HDF5) broadcast
                                                          │
                                                    WebSocket
                                                          │
                                                    ┌─────▼─────┐
                                                    │  Browser   │
                                                    │  (uPlot)   │
                                                    └───────────┘
```

- **NetworkReceiver** — TCP listener in a background thread. Frames are length-prefixed (4-byte big-endian uint32 + protobuf payload).
- **protobuf_decoder** — Decodes protobuf into `FrameData` (numpy arrays per axis) and `FFTFrameData`.
- **Broadcaster** — Accumulates samples, emits min/max/last per axis at `WS_FPS` Hz via WebSocket. FFT spectra are throttled to 30 Hz. Supports pause/resume to stop sending data to browsers without affecting logging.
- **LogWriter / LogManager** — Full-rate logging to TSV or HDF5 (selectable in the UI).
- **MDNSScanner** — Discovers sensors on the network via `_nw-config._udp.local.` mDNS service.
- **sensor_api** — UDP API for sensor configuration (HMAC-MD5 authenticated).

## Browser UI

- **Topbar** — Company logo, app title, server IP:port, light/dark theme toggle
- **Chart toolbar** — New window, tile grid/vertical, time window selector, log format, start/stop logging, clear, live stats (packets/frames/samples/FFT), console toggle
- **Charts area** — Floating draggable/resizable windows with snap-to-grid (60px). Create any combination of:
  - Raw Waveform (XYZ) — live acceleration traces
  - FFT (X/Y/Z) — frequency spectrum per axis, X-axis scaled by actual sample rate
  - PSD (X/Y/Z) — power spectral density per axis
- **Sidebar** — Device list (auto-discovered via mDNS), selected sensor controls, sensor config, FFT config, network config
- **Floating console** — Toggleable JSON output window for sensor API responses

### Window management

- **Tile Grid** — Auto-arranges all windows into an even grid
- **Tile Vertical** — Stacks all windows vertically at full width
- **Snap-to-grid** — Windows snap to a 60px grid on drag/resize release
- **Bounds clamping** — Windows cannot be dragged or resized beyond the charts area

### Stream control

- **Stream Start/Stop** — Pauses/resumes data broadcast to the browser. The backend continues receiving and logging data regardless. No sensor selection required.
- **FFT Start/Stop** — Runtime toggle for FFT streaming from the sensor (requires sensor selection).

## Logging formats

Toggle between formats in the chart toolbar dropdown (while not actively logging).

- **TSV** (`.log`) — Tab-separated values. Human-readable, open in any text editor or spreadsheet.
- **HDF5** (`.h5`) — Chunked + gzip compressed. Read with Python (`h5py`, `scipy`), MATLAB, R, Julia. Includes metadata attributes (sample rate, units, device ID).

## REST API

| Method | Endpoint                | Description                          |
|--------|------------------------|--------------------------------------|
| GET    | `/api/status`          | Connection, logging, streaming state |
| GET    | `/api/devices`         | Discovered sensors (mDNS)           |
| GET    | `/api/host`            | Server LAN IP and TCP port          |
| POST   | `/api/stream/start`    | Resume data broadcast to browsers   |
| POST   | `/api/stream/stop`     | Pause data broadcast to browsers    |
| POST   | `/api/logging/start`   | Start logging to file               |
| POST   | `/api/logging/stop`    | Stop logging                        |
| GET/POST | `/api/logging/format` | Get/set log format (tsv/hdf5)      |
| POST   | `/api/stats/reset`     | Reset packet/frame/FFT counters     |
| POST   | `/api/sensor/info`     | Query sensor info                   |
| POST   | `/api/sensor/config`   | Get sensor config                   |
| POST   | `/api/sensor/config/set` | Set sensor config                 |
| POST   | `/api/network/config`  | Get network config                  |
| POST   | `/api/network/config/set` | Set network config               |
| POST   | `/api/stream/fft/start` | Start FFT streaming on sensor      |
| POST   | `/api/stream/fft/stop`  | Stop FFT streaming on sensor       |
| POST   | `/api/sensor/reset`    | Reset the sensor                    |
