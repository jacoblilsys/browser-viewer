# Browser Viewer

Real-time browser-based vibration sensor viewer. Receives protobuf-framed acceleration data over TCP, displays live waveforms with min/max envelope, and provides sensor configuration via a web UI.

## Requirements

- Python 3.10+
- Packages listed in `requirements.txt`

## Setup

```bash
cd Utils/browser_viewer/backend

# Create virtual environment
python3 -m venv ~/venv-browser-viewer
source ~/venv-browser-viewer/bin/activate

# Install dependencies
pip install -r ../requirements.txt
```

## Running

```bash
source ~/venv-browser-viewer/bin/activate
cd Utils/browser_viewer/backend
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

# Lower broadcast rate to reduce CPU/bandwidth
WS_FPS=30 uvicorn main:app --host 0.0.0.0 --port 8000

# Custom TCP port and log directory
TCP_PORT=9000 LOG_DIR=/data/logs uvicorn main:app --host 0.0.0.0 --port 8000

# Auto-reload during development
uvicorn main:app --reload --port 8000
```

### Broadcast rate (`WS_FPS`)

The backend accumulates full-rate samples (e.g. 26.6 kHz) and sends a downsampled envelope (min/max/last per axis) to the browser at `WS_FPS` frames per second.

| WS_FPS | Decimation at 26.6 kHz | WS bandwidth | Notes                        |
|--------|------------------------|--------------|------------------------------|
| 30     | ~887:1                 | ~6 KB/s      | Low CPU, coarser display     |
| 60     | ~443:1                 | ~12 KB/s     | Default, smooth for most use |
| 120    | ~222:1                 | ~24 KB/s     | Smoother, higher CPU         |
| 240    | ~111:1                 | ~48 KB/s     | Near real-time envelope      |

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
- **protobuf_decoder** — Decodes protobuf into `FrameData` (numpy arrays per axis).
- **Broadcaster** — Accumulates samples, emits min/max/last envelope at `WS_FPS` Hz via WebSocket.
- **LogWriter / LogManager** — Full-rate logging to TSV or HDF5 (selectable in the UI).
- **MDNSScanner** — Discovers sensors on the network via `_nw-config._udp.local.` mDNS service.
- **sensor_api** — UDP API for sensor configuration (HMAC-MD5 authenticated).

## Logging formats

Toggle between formats in the chart toolbar dropdown (while not actively logging).

- **TSV** (`.log`) — Tab-separated values. Human-readable, open in any text editor or spreadsheet.
- **HDF5** (`.h5`) — Chunked + gzip compressed. Read with Python (`h5py`, `scipy`), MATLAB, R, Julia. Includes metadata attributes (sample rate, units, device ID).

## Browser UI

- **Topbar** — App title, server IP:port, light/dark theme toggle
- **Chart** — Real-time uPlot waveform with min/max envelope bands (X/Y/Z axes)
- **Chart toolbar** — Window size, envelope toggle, log format selector, start/stop logging, clear
- **Sidebar** — Device list (auto-discovered via mDNS), sensor config, network config
- **Console footer** — JSON output from sensor API commands
