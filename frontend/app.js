/**
 * Browser Sensor Viewer — frontend logic
 *
 * WebSocket receives JSON envelopes from the backend:
 *   { type:'frame', t_end_ns, rate_hz, axes:{ accel_x:{min,max,last,n}, ... } }
 *   { type:'fft', fft_bins, fft_size, freq_hz, magnitudes:{x,y,z}, psd:{x,y,z}, unit, seq }
 *   { type:'status', connected, logging, device_id }
 *
 * Charts live in draggable, resizable floating windows.
 */

'use strict';

// ── config ────────────────────────────────────────────────────────────────────
const WS_URL      = `ws://${location.host}/ws`;
const MAX_PTS     = 6000;
const RECONNECT_MS = 2000;

// ── state ─────────────────────────────────────────────────────────────────────
let ws          = null;
let wsAlive     = false;
let _streamUid  = null;
let _rateHz     = 0;       // last known sample rate from frame data

// Ring buffers for raw waveform
const ring = {
  t:     [],
  x_last: [],
  y_last: [],
  z_last: [],
};

let windowSec = 5;

// Latest FFT/PSD data (shared across all windows of that type)
let _lastFFT = null;  // { freq_hz, magnitudes, psd, unit, fft_bins, fft_size }
let _burstFFT = null; // { snapshots: [...], index: 0 } — set after burst capture

// Time sync ring buffer
const _timeSync = { t: [], diff_ms: [] };
const MAX_TIMESYNC_PTS = 600; // ~10 minutes at 1 Hz status rate

// ── DOM refs ──────────────────────────────────────────────────────────────────
const dotSensor = document.getElementById('dot-sensor');
const dotLog    = document.getElementById('dot-log');
const dotWs     = document.getElementById('dot-ws');
const lblDevice = document.getElementById('lbl-device');
const lblRate   = document.getElementById('lbl-rate');
const lblLog    = document.getElementById('lbl-log');
const lblWs     = document.getElementById('lbl-ws');
const btnLog    = document.getElementById('btn-log');
const selFmt    = document.getElementById('sel-log-format');
const btnClear  = document.getElementById('btn-clear');
const selWindow = document.getElementById('sel-window');
const outInfo   = document.getElementById('out-info');
const chartsArea = document.getElementById('charts-area');

const inPw         = document.getElementById('in-pw');
const deviceListEl = document.getElementById('device-list');
const selectedInfo = document.getElementById('selected-info');

let selectedSensor = null;
let _refreshTimer  = null;

const hostInfo   = document.getElementById('host-info');

// ── Chart window types ────────────────────────────────────────────────────────
const WINDOW_TYPES = {
  raw:   { label: 'Raw Waveform (XYZ)',  group: 'time' },
  fftX:  { label: 'FFT — X Axis',        group: 'fft', axis: 'x', color: '#e05252' },
  fftY:  { label: 'FFT — Y Axis',        group: 'fft', axis: 'y', color: '#52c45a' },
  fftZ:  { label: 'FFT — Z Axis',        group: 'fft', axis: 'z', color: '#5296e0' },
  psdX:  { label: 'PSD — X Axis',        group: 'psd', axis: 'x', color: '#e05252' },
  psdY:  { label: 'PSD — Y Axis',        group: 'psd', axis: 'y', color: '#52c45a' },
  psdZ:  { label: 'PSD — Z Axis',        group: 'psd', axis: 'z', color: '#5296e0' },
  spectX: { label: 'Spectrogram — X',    group: 'spectrogram', axis: 'x' },
  spectY: { label: 'Spectrogram — Y',    group: 'spectrogram', axis: 'y' },
  spectZ: { label: 'Spectrogram — Z',    group: 'spectrogram', axis: 'z' },
  timeSync: { label: 'Time Sync',        group: 'timesync' },
};

// ── Floating window manager ──────────────────────────────────────────────────
let _chartWindows = {};  // id → { id, type, el, canvas, plot, x, y, w, h }
let _nextWinId = 1;
let _topZ = 10;

function _saveWindowState() {
  const state = {};
  for (const [id, win] of Object.entries(_chartWindows)) {
    state[id] = { id: win.id, type: win.type, x: win.x, y: win.y, w: win.w, h: win.h };
  }
  localStorage.setItem('chartWindows', JSON.stringify(state));
}

function _loadWindowState() {
  try {
    return JSON.parse(localStorage.getItem('chartWindows')) || {};
  } catch { return {}; }
}

const GRID = 60;
const snap = (v) => Math.round(v / GRID) * GRID;

function _cascadePosition() {
  const count = Object.keys(_chartWindows).length;
  const areaW = chartsArea.clientWidth || 800;
  const areaH = chartsArea.clientHeight || 400;
  const x = snap(20 + (count % 6) * 30);
  const y = snap(10 + (count % 6) * 30);
  const w = snap(Math.min(Math.max(areaW * 0.5, 300), areaW - x - 10));
  const h = snap(Math.min(Math.max(areaH * 0.45, 200), areaH - y - 10));
  return { x, y, w, h };
}

function createChartWindow(type, opts = {}) {
  const info = WINDOW_TYPES[type];
  if (!info) return null;

  const id = 'cw-' + (_nextWinId++);
  const pos = { ..._cascadePosition(), ...opts };

  // Create DOM
  const el = document.createElement('div');
  el.className = 'chart-window';
  el.style.left   = pos.x + 'px';
  el.style.top    = pos.y + 'px';
  el.style.width  = pos.w + 'px';
  el.style.height = pos.h + 'px';
  el.style.zIndex = ++_topZ;
  el.dataset.winId = id;

  const titlebar = document.createElement('div');
  titlebar.className = 'chart-window-titlebar';

  const title = document.createElement('span');
  title.className = 'chart-window-title';
  title.textContent = info.label;

  const closeBtn = document.createElement('button');
  closeBtn.className = 'chart-window-close';
  closeBtn.innerHTML = '&times;';
  closeBtn.title = 'Close';

  titlebar.appendChild(title);
  titlebar.appendChild(closeBtn);

  const canvas = document.createElement('div');
  canvas.className = 'chart-window-canvas';

  el.appendChild(titlebar);
  el.appendChild(canvas);

  // Legend for raw waveform
  if (type === 'raw') {
    const legend = document.createElement('div');
    legend.className = 'chart-window-legend';
    legend.innerHTML = '<span class="leg-x">— X</span><span class="leg-y">— Y</span><span class="leg-z">— Z</span>';
    el.appendChild(legend);
  }

  // Scrubber for FFT/PSD windows (hidden until burst data)
  if (info.group === 'fft' || info.group === 'psd') {
    const scrubber = document.createElement('div');
    scrubber.className = 'chart-window-scrubber';
    scrubber.hidden = true;
    scrubber.innerHTML = '<span class="scrubber-label">0/0</span><input type="range" min="0" max="0" value="0" />';
    scrubber.querySelector('input').addEventListener('input', (e) => {
      const idx = Number(e.target.value);
      if (_burstFFT) _showBurstFFTFrame(idx);
    });
    el.appendChild(scrubber);
  }

  // Resize handle
  const resizeHandle = document.createElement('div');
  resizeHandle.className = 'chart-window-resize';
  el.appendChild(resizeHandle);

  chartsArea.appendChild(el);

  const win = { id, type, el, canvas, plot: null, x: pos.x, y: pos.y, w: pos.w, h: pos.h };
  _chartWindows[id] = win;

  // Create plot after a frame so the canvas has dimensions
  requestAnimationFrame(() => {
    if (info.group === 'spectrogram') {
      win.plot = _createSpectrogramPlot(canvas, info);
    } else if (info.group === 'timesync') {
      win.plot = _createTimeSyncPlot(canvas);
    } else {
      win.plot = _createPlot(type, canvas, info);
    }
    _saveWindowState();
  });

  // ── Drag ──
  _setupDrag(titlebar, win);

  // ── Resize ──
  _setupResize(resizeHandle, win);

  // ── Focus on click ──
  el.addEventListener('mousedown', () => {
    el.style.zIndex = ++_topZ;
    for (const w of Object.values(_chartWindows)) {
      w.el.classList.toggle('focused', w.id === id);
    }
  });

  // ── Close ──
  closeBtn.addEventListener('click', () => {
    destroyChartWindow(id);
  });

  _saveWindowState();
  return win;
}

function destroyChartWindow(id) {
  const win = _chartWindows[id];
  if (!win) return;
  if (win.plot) win.plot.destroy();
  win.el.remove();
  delete _chartWindows[id];
  _saveWindowState();
}

function _setupDrag(handle, win) {
  let startX, startY, origX, origY;

  handle.addEventListener('mousedown', (e) => {
    if (e.target.closest('.chart-window-close')) return;
    e.preventDefault();
    startX = e.clientX;
    startY = e.clientY;
    origX = win.x;
    origY = win.y;

    const onMove = (e) => {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      const areaW = chartsArea.clientWidth;
      const areaH = chartsArea.clientHeight;
      win.x = Math.max(0, Math.min(origX + dx, areaW - win.w));
      win.y = Math.max(0, Math.min(origY + dy, areaH - win.h));
      win.el.style.left = win.x + 'px';
      win.el.style.top  = win.y + 'px';
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      win.x = snap(win.x);
      win.y = snap(win.y);
      win.el.style.left = win.x + 'px';
      win.el.style.top  = win.y + 'px';
      _saveWindowState();
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function _setupResize(handle, win) {
  let startX, startY, origW, origH;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    startX = e.clientX;
    startY = e.clientY;
    origW = win.w;
    origH = win.h;

    const onMove = (e) => {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      const areaW = chartsArea.clientWidth;
      const areaH = chartsArea.clientHeight;
      win.w = Math.max(200, Math.min(origW + dx, areaW - win.x));
      win.h = Math.max(140, Math.min(origH + dy, areaH - win.y));
      win.el.style.width  = win.w + 'px';
      win.el.style.height = win.h + 'px';
      if (win.plot && win.canvas.clientWidth > 0) {
        win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
      }
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      win.w = snap(Math.max(200, win.w));
      win.h = snap(Math.max(140, win.h));
      win.el.style.width  = win.w + 'px';
      win.el.style.height = win.h + 'px';
      if (win.plot && win.canvas.clientWidth > 0) {
        win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
      }
      _saveWindowState();
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// ── Tile windows ─────────────────────────────────────────────────────────────
function tileWindows() {
  const wins = Object.values(_chartWindows);
  if (wins.length === 0) return;
  const areaW = chartsArea.clientWidth;
  const areaH = chartsArea.clientHeight;
  const cols = Math.ceil(Math.sqrt(wins.length));
  const rows = Math.ceil(wins.length / cols);
  const cellW = snap(areaW / cols);
  const cellH = snap(areaH / rows);
  const pad = 4;

  wins.forEach((win, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    win.x = col * cellW + pad;
    win.y = row * cellH + pad;
    win.w = cellW - pad * 2;
    win.h = cellH - pad * 2;
    win.el.style.left   = win.x + 'px';
    win.el.style.top    = win.y + 'px';
    win.el.style.width  = win.w + 'px';
    win.el.style.height = win.h + 'px';
    if (win.plot) {
      win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
    }
  });
  _saveWindowState();
}

function tileWindowsVertical() {
  const wins = Object.values(_chartWindows);
  if (wins.length === 0) return;
  const areaW = chartsArea.clientWidth;
  const areaH = chartsArea.clientHeight;
  const cellH = snap(areaH / wins.length);
  const pad = 4;

  wins.forEach((win, i) => {
    win.x = pad;
    win.y = i * cellH + pad;
    win.w = areaW - pad * 2;
    win.h = cellH - pad * 2;
    win.el.style.left   = win.x + 'px';
    win.el.style.top    = win.y + 'px';
    win.el.style.width  = win.w + 'px';
    win.el.style.height = win.h + 'px';
    if (win.plot) {
      win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
    }
  });
  _saveWindowState();
}

// ── Spectrogram renderer ─────────────────────────────────────────────────────
function _createSpectrogramPlot(container, info) {
  const cvs = document.createElement('canvas');
  cvs.className = 'spectrogram-canvas';
  container.appendChild(cvs);

  const obj = {
    _cvs: cvs,
    _axis: info.axis,
    setSize() {
      cvs.width = container.clientWidth;
      cvs.height = container.clientHeight;
      if (_burstFFT) obj.update(_burstFFT.snapshots);
    },
    destroy() { cvs.remove(); },
    setData() {},  // no-op for compatibility
    update(snapshots) {
      const ctx = cvs.getContext('2d');
      const W = cvs.width;
      const H = cvs.height;
      ctx.clearRect(0, 0, W, H);
      if (!snapshots || snapshots.length === 0) return;

      const axis = obj._axis;
      const nFrames = snapshots.length;
      const nBins = snapshots[0].fft_bins || 0;
      if (nBins === 0) return;

      // Find global min/max for color normalization
      let gMin = Infinity, gMax = -Infinity;
      for (const snap of snapshots) {
        const mag = snap.magnitudes[axis];
        if (!mag) continue;
        for (let k = 0; k < mag.length; k++) {
          if (mag[k] < gMin) gMin = mag[k];
          if (mag[k] > gMax) gMax = mag[k];
        }
      }
      if (gMax <= gMin) gMax = gMin + 1;

      // Margins for labels
      const mLeft = 45, mBottom = 25, mTop = 5, mRight = 5;
      const plotW = W - mLeft - mRight;
      const plotH = H - mTop - mBottom;
      if (plotW <= 0 || plotH <= 0) return;

      // Draw heatmap
      const img = ctx.createImageData(nBins, nFrames);
      for (let row = 0; row < nFrames; row++) {
        const mag = snapshots[row].magnitudes[axis];
        if (!mag) continue;
        for (let col = 0; col < nBins; col++) {
          const t = (mag[col] - gMin) / (gMax - gMin);
          const [r, g, b] = _viridis(t);
          const idx = (row * nBins + col) * 4;
          img.data[idx] = r; img.data[idx+1] = g; img.data[idx+2] = b; img.data[idx+3] = 255;
        }
      }
      // Draw to offscreen canvas, then scale to plot area
      const off = new OffscreenCanvas(nBins, nFrames);
      off.getContext('2d').putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(off, mLeft, mTop, plotW, plotH);

      // Axis labels
      const isLight = document.body.classList.contains('light');
      ctx.fillStyle = isLight ? '#222' : '#fff';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';

      // X-axis: frequency
      const maxFreq = snapshots[0].freq_hz ? snapshots[0].freq_hz[nBins - 1] : nBins;
      for (let i = 0; i <= 4; i++) {
        const f = (maxFreq * i / 4);
        const x = mLeft + (plotW * i / 4);
        const label = f >= 1000 ? (f / 1000).toFixed(1) + 'k' : Math.round(f);
        ctx.fillText(label, x, H - 4);
      }
      ctx.fillText('Hz', mLeft + plotW / 2, H - 14);

      // Y-axis: time (frame index)
      ctx.textAlign = 'right';
      for (let i = 0; i <= 4; i++) {
        const fIdx = Math.round(nFrames * i / 4);
        const y = mTop + (plotH * i / 4);
        ctx.fillText(fIdx, mLeft - 4, y + 4);
      }
      ctx.save();
      ctx.translate(10, mTop + plotH / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'center';
      ctx.fillText('Frame', 0, 0);
      ctx.restore();
    },
  };

  requestAnimationFrame(() => obj.setSize());
  return obj;
}

// Viridis-like colormap: 0→dark blue, 0.5→teal/green, 1→yellow
function _viridis(t) {
  t = Math.max(0, Math.min(1, t));
  const r = Math.round(Math.min(255, Math.max(0, (t < 0.5 ? t * 2 * 30 : 30 + (t - 0.5) * 2 * 225))));
  const g = Math.round(Math.min(255, Math.max(0, (t < 0.5 ? 10 + t * 2 * 140 : 150 + (t - 0.5) * 2 * 105))));
  const b = Math.round(Math.min(255, Math.max(0, (t < 0.5 ? 80 + t * 2 * 100 : 180 - (t - 0.5) * 2 * 180))));
  return [r, g, b];
}

// ── Burst FFT scrubber + spectrogram helpers ─────────────────────────────────
function _showBurstFFTFrame(index) {
  if (!_burstFFT || !_burstFFT.snapshots.length) return;
  _burstFFT.index = Math.max(0, Math.min(index, _burstFFT.snapshots.length - 1));
  const snap = _burstFFT.snapshots[_burstFFT.index];

  // Recompute freq from rate if possible
  let freq = snap.freq_hz;
  const fftSize = snap.fft_size || (snap.fft_bins ? snap.fft_bins * 2 : 0);
  if (_rateHz > 0 && fftSize > 0) {
    freq = [];
    for (let k = 0; k < snap.fft_bins; k++) freq.push(k * _rateHz / fftSize);
  }

  _lastFFT = { freq_hz: freq, magnitudes: snap.magnitudes, psd: snap.psd || {}, unit: snap.unit || 'm/s²', fft_bins: snap.fft_bins, fft_size: fftSize };

  // Update FFT/PSD windows
  for (const win of Object.values(_chartWindows)) {
    if (!win.plot) continue;
    const info = WINDOW_TYPES[win.type];
    if (!info) continue;
    if (info.group === 'fft' && snap.magnitudes[info.axis]) {
      win.plot.setData([freq, snap.magnitudes[info.axis]]);
    } else if (info.group === 'psd' && snap.psd && snap.psd[info.axis]) {
      win.plot.setData([freq, snap.psd[info.axis]]);
    }
  }

  // Update all scrubber labels
  for (const win of Object.values(_chartWindows)) {
    const scrubber = win.el.querySelector('.chart-window-scrubber');
    if (scrubber) {
      scrubber.querySelector('.scrubber-label').textContent =
        `${_burstFFT.index + 1}/${_burstFFT.snapshots.length}`;
      scrubber.querySelector('input').value = _burstFFT.index;
    }
  }
}

function _updateSpectrograms() {
  for (const win of Object.values(_chartWindows)) {
    const info = WINDOW_TYPES[win.type];
    if (info && info.group === 'spectrogram' && win.plot && win.plot.update) {
      win.plot.update(_burstFFT ? _burstFFT.snapshots : []);
    }
  }
}

function _showBurstScrubbers() {
  const n = _burstFFT ? _burstFFT.snapshots.length : 0;
  for (const win of Object.values(_chartWindows)) {
    const scrubber = win.el.querySelector('.chart-window-scrubber');
    if (!scrubber) continue;
    if (n > 0) {
      scrubber.hidden = false;
      const inp = scrubber.querySelector('input');
      inp.max = n - 1;
      inp.value = 0;
      scrubber.querySelector('.scrubber-label').textContent = `1/${n}`;
    } else {
      scrubber.hidden = true;
    }
  }
}

// ── Time Sync window ─────────────────────────────────────────────────────────
function _createTimeSyncPlot(container) {
  // Time display panel above the graph
  const infoDiv = document.createElement('div');
  infoDiv.className = 'timesync-info';
  infoDiv.innerHTML = `
    <div><span class="ts-label">Device (NTP):</span> <span class="ts-value" id="ts-device">—</span></div>
    <div><span class="ts-label">Host:</span> <span class="ts-value" id="ts-host">—</span></div>
    <div><span class="ts-label">Difference:</span> <span class="ts-value" id="ts-diff">—</span></div>
  `;
  container.appendChild(infoDiv);

  // Graph canvas
  const graphDiv = document.createElement('div');
  graphDiv.className = 'timesync-graph';
  container.appendChild(graphDiv);

  const w = graphDiv.clientWidth || 400;
  const h = graphDiv.clientHeight || 150;

  const opts = {
    width: w, height: h,
    pxAlign: false,
    cursor: { show: true, drag: { x: true, y: true } },
    legend: { show: false },
    plugins: [wheelZoomPlugin()],
    axes: [
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: { stroke: '#444', width: 0.5 }, label: 'Time' },
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: { stroke: '#334', width: 0.5 }, label: 'ms' },
    ],
    series: [
      {},
      { stroke: '#ffb300', width: 2, paths: uPlot.paths.linear(), label: 'Host − Device' },
    ],
    scales: { x: { time: false }, y: { auto: true } },
  };

  const plot = new uPlot(opts, [[], []], graphDiv);

  return {
    _plot: plot,
    _graphDiv: graphDiv,
    _infoDiv: infoDiv,
    setSize() {
      if (graphDiv.clientWidth > 0) {
        const gh = container.clientHeight - infoDiv.offsetHeight;
        plot.setSize({ width: graphDiv.clientWidth, height: Math.max(60, gh) });
      }
    },
    destroy() { plot.destroy(); infoDiv.remove(); graphDiv.remove(); },
    setData(data) { plot.setData(data); },
    update(deviceNs, hostNs, diffMs) {
      // Update text display
      const devDate = new Date(deviceNs / 1e6);
      const hostDate = new Date(hostNs / 1e6);
      const fmt = (d) => d.toISOString().replace('T', ' ').replace('Z', '');
      container.querySelector('#ts-device').textContent = fmt(devDate);
      container.querySelector('#ts-host').textContent = fmt(hostDate);
      const sign = diffMs >= 0 ? '+' : '';
      container.querySelector('#ts-diff').textContent = `${sign}${diffMs.toFixed(2)} ms`;

      // Update graph with ring buffer data
      if (_timeSync.t.length > 1) {
        // Normalize time axis to seconds since first point
        const t0 = _timeSync.t[0];
        const t = _timeSync.t.map(v => v - t0);
        plot.setData([t, _timeSync.diff_ms]);
      }
    },
  };
}

function _updateTimeSyncWindows(deviceNs, hostNs, diffMs) {
  for (const win of Object.values(_chartWindows)) {
    if (WINDOW_TYPES[win.type]?.group === 'timesync' && win.plot && win.plot.update) {
      win.plot.update(deviceNs, hostNs, diffMs);
    }
  }
}

// ── uPlot creation per window type ────────────────────────────────────────────
const _GRID = { stroke: '#444', width: 0.5 };
const _GRID_Y = { stroke: '#334', width: 0.5 };
function _axisStroke() { return document.body.classList.contains('light') ? '#222' : '#fff'; }
function _tickStroke() { return document.body.classList.contains('light') ? '#bbb' : '#666'; }

// Wheel zoom plugin for uPlot
function wheelZoomPlugin() {
  return {
    hooks: {
      ready(u) {
        const plot = u.over;
        plot.addEventListener('wheel', (e) => {
          e.preventDefault();
          const { left, top } = u.cursor;
          if (left == null || top == null) return;
          const factor = e.deltaY < 0 ? 0.8 : 1.25;

          const xMin = u.scales.x.min;
          const xMax = u.scales.x.max;
          const xVal = u.posToVal(left, 'x');
          const nxMin = xVal - (xVal - xMin) * factor;
          const nxMax = xVal + (xMax - xVal) * factor;

          const yMin = u.scales.y.min;
          const yMax = u.scales.y.max;
          const yVal = u.posToVal(top, 'y');
          const nyMin = yVal - (yVal - yMin) * factor;
          const nyMax = yVal + (yMax - yVal) * factor;

          u.batch(() => {
            u.setScale('x', { min: nxMin, max: nxMax });
            u.setScale('y', { min: nyMin, max: nyMax });
          });
        });
      },
    },
  };
}

function _createPlot(type, canvas, info) {
  const w = canvas.clientWidth  || 400;
  const h = canvas.clientHeight || 200;

  if (type === 'raw') return _createRawPlot(w, h, canvas);

  // FFT or PSD — single axis line chart
  const yLabel = info.group === 'psd' ? '(m/s\u00B2)\u00B2/Hz' : (info.group === 'fft' ? 'm/s\u00B2' : '');
  const color = info.color;
  const opts = {
    width: w, height: h,
    pxAlign: false,
    cursor: { show: true, drag: { x: true, y: true } },
    legend: { show: false },
    plugins: [wheelZoomPlugin()],
    axes: [
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID, label: 'Hz' },
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID_Y, label: yLabel },
    ],
    series: [
      {},
      { stroke: color, width: 1.5, paths: uPlot.paths.linear(), fill: color + '22' },
    ],
    scales: { x: { time: false }, y: { auto: true } },
  };
  return new uPlot(opts, [[], []], canvas);
}

function _createRawPlot(w, h, canvas) {
  const opts = {
    width: w, height: h,
    pxAlign: false,
    cursor: { show: true, drag: { x: true, y: true } },
    legend: { show: false },
    plugins: [wheelZoomPlugin()],
    axes: [
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID },
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID_Y },
    ],
    series: [
      {},
      { stroke: '#e05252', width: 1.5, paths: uPlot.paths.linear() },
      { stroke: '#52c45a', width: 1.5, paths: uPlot.paths.linear() },
      { stroke: '#5296e0', width: 1.5, paths: uPlot.paths.linear() },
    ],
    scales: { x: { time: false }, y: { auto: true } },
  };
  return new uPlot(opts, _emptyRawData(), canvas);
}

function _emptyRawData() {
  return [[], [], [], []];
}

function _buildRawPlotData() {
  const t  = ring.t;
  const now = t.length ? t[t.length - 1] : 0;
  const cut = now - windowSec;
  const i0  = t.findIndex(v => v >= cut);
  const sl  = (arr) => arr.slice(i0 < 0 ? 0 : i0);
  return [
    sl(ring.t),
    sl(ring.x_last),
    sl(ring.y_last),
    sl(ring.z_last),
  ];
}

function _resizeAllPlots() {
  for (const win of Object.values(_chartWindows)) {
    if (win.plot && win.canvas.clientWidth > 0 && win.canvas.clientHeight > 0) {
      win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
    }
  }
}

// Resize observer
new ResizeObserver(() => _resizeAllPlots()).observe(chartsArea);

// ── "New Window" popup ────────────────────────────────────────────────────────
const popupOverlay = document.getElementById('popup-overlay');
const popupOptions = document.getElementById('popup-options');

document.getElementById('btn-add-window').addEventListener('click', () => {
  popupOptions.innerHTML = '';
  for (const [type, info] of Object.entries(WINDOW_TYPES)) {
    const btn = document.createElement('button');
    btn.className = 'popup-option';
    btn.textContent = info.label;
    btn.addEventListener('click', () => {
      popupOverlay.hidden = true;
      createChartWindow(type);
    });
    popupOptions.appendChild(btn);
  }
  popupOverlay.hidden = false;
});

document.getElementById('popup-cancel').addEventListener('click', () => {
  popupOverlay.hidden = true;
});

popupOverlay.addEventListener('click', (e) => {
  if (e.target === popupOverlay) popupOverlay.hidden = true;
});

document.getElementById('btn-tile-grid').addEventListener('click', tileWindows);
document.getElementById('btn-tile-vertical').addEventListener('click', tileWindowsVertical);

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    wsAlive = true;
    dotWs.className = 'dot green';
    lblWs.textContent = 'Connected';
  };

  ws.onclose = () => {
    wsAlive = false;
    dotWs.className = 'dot red';
    lblWs.textContent = 'Reconnecting\u2026';
    setTimeout(connect, RECONNECT_MS);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === 'frame')  handleFrame(msg);
    if (msg.type === 'fft')    handleFFT(msg);
    if (msg.type === 'status') handleStatus(msg);
    if (msg.type === 'burst')  handleBurst(msg);
  };
}

function handleFrame(msg) {
  if (msg.stream_uid !== undefined && msg.stream_uid !== _streamUid) {
    _streamUid = msg.stream_uid;
    for (const k of Object.keys(ring)) ring[k] = [];
  }

  if (msg.rate_hz) {
    _rateHz = msg.rate_hz;
    lblRate.textContent = _rateHz >= 1000
      ? `${(_rateHz / 1000).toFixed(1)} kHz`
      : `${Math.round(_rateHz)} Hz`;
  }

  // Collect time sync data
  if (msg.device_time_ns && msg.t_end_ns) {
    const hostSec = msg.t_end_ns / 1e9;
    const devSec = msg.device_time_ns / 1e9;
    const diffMs = (hostSec - devSec) * 1000;
    _timeSync.t.push(hostSec);
    _timeSync.diff_ms.push(diffMs);
    while (_timeSync.t.length > MAX_TIMESYNC_PTS) {
      _timeSync.t.shift();
      _timeSync.diff_ms.shift();
    }
    _updateTimeSyncWindows(msg.device_time_ns, msg.t_end_ns, diffMs);
  }

  // Raw samples mode (low ODR — all samples forwarded)
  if (msg.raw_samples) {
    const rs = msg.raw_samples;
    const x = rs['accel_x'] || [];
    const y = rs['accel_y'] || [];
    const z = rs['accel_z'] || [];
    const n = Math.max(x.length, y.length, z.length);
    if (n === 0) return;

    const tEnd = msg.t_end_ns / 1e9;
    const rate = msg.rate_hz || _rateHz || 1;
    const tStart = tEnd - (n - 1) / rate;
    for (let i = 0; i < n; i++) {
      ring.t.push(tStart + i / rate);
      ring.x_last.push(x[i] ?? null);
      ring.y_last.push(y[i] ?? null);
      ring.z_last.push(z[i] ?? null);
    }
  } else {
    // Decimated mode (high ODR — min/max/last envelope)
    const axes = msg.axes || {};
    if (Object.keys(axes).length === 0) return;

    const tSec = msg.t_end_ns / 1e9;
    const get = (label, key) => {
      const a = axes[label];
      return (a && key in a) ? a[key] : null;
    };

    const xLast = get('accel_x', 'last');
    const yLast = get('accel_y', 'last');
    const zLast = get('accel_z', 'last');

    if (xLast === null && yLast === null && zLast === null) return;

    ring.t.push(tSec);
    ring.x_last.push(xLast);
    ring.y_last.push(yLast);
    ring.z_last.push(zLast);
  }

  while (ring.t.length > MAX_PTS) {
    for (const k of Object.keys(ring)) ring[k].shift();
  }

  // Update all raw windows
  const plotData = _buildRawPlotData();
  for (const win of Object.values(_chartWindows)) {
    if (win.type === 'raw' && win.plot) {
      if (msg.units) {
        const unit = msg.units['accel_x'] || msg.units['accel_y'] || msg.units['accel_z']
                  || msg.units['force_x'] || msg.units[Object.keys(msg.units)[0]] || '';
        if (unit) {
          win.plot.axes[1].label = unit;
        }
      }
      win.plot.setData(plotData);
    }
  }
}

function handleFFT(msg) {
  // Live FFT clears burst scrubber state
  if (_burstFFT) {
    _burstFFT = null;
    _showBurstScrubbers();
  }

  let freq = msg.freq_hz;
  const mags = msg.magnitudes;
  const psd  = msg.psd;

  if (!freq || !mags) return;

  // Recompute frequency axis from actual sample rate if known
  const fftSize = msg.fft_size || (msg.fft_bins ? msg.fft_bins * 2 : 0);
  if (_rateHz > 0 && fftSize > 0) {
    freq = [];
    for (let k = 0; k < msg.fft_bins; k++) {
      freq.push(k * _rateHz / fftSize);
    }
  }

  _lastFFT = { freq_hz: freq, magnitudes: mags, psd: psd || {}, unit: msg.unit || 'm/s\u00B2', fft_bins: msg.fft_bins, fft_size: fftSize };

  // Update all FFT and PSD windows
  for (const win of Object.values(_chartWindows)) {
    if (!win.plot) continue;
    const info = WINDOW_TYPES[win.type];
    if (!info) continue;

    if (info.group === 'fft' && mags[info.axis]) {
      win.plot.setData([freq, mags[info.axis]]);
    } else if (info.group === 'psd' && psd && psd[info.axis]) {
      win.plot.setData([freq, psd[info.axis]]);
    }
  }
}

function handleBurst(msg) {
  const samples = msg.samples || {};
  const rate = msg.sample_rate_hz || _rateHz || 26667;
  const x = samples['accel_x'] || [];
  const y = samples['accel_y'] || [];
  const z = samples['accel_z'] || [];
  const n = Math.max(x.length, y.length, z.length);
  if (n === 0) return;

  // Build time axis from sample rate
  const t = new Array(n);
  for (let i = 0; i < n; i++) t[i] = i / rate;

  // Replace ring buffers with burst data
  ring.t = t;
  ring.x_last = x.length === n ? x : new Array(n).fill(null);
  ring.y_last = y.length === n ? y : new Array(n).fill(null);
  ring.z_last = z.length === n ? z : new Array(n).fill(null);

  if (msg.sample_rate_hz) {
    _rateHz = msg.sample_rate_hz;
    lblRate.textContent = `${(msg.sample_rate_hz / 1000).toFixed(1)} kHz`;
  }

  // Update all raw windows with full burst data (no windowing)
  const plotData = [ring.t, ring.x_last, ring.y_last, ring.z_last];
  for (const win of Object.values(_chartWindows)) {
    if (win.type === 'raw' && win.plot) {
      if (msg.units) {
        const unit = msg.units['accel_x'] || msg.units['accel_y'] || msg.units['accel_z']
                  || msg.units[Object.keys(msg.units)[0]] || '';
        if (unit) win.plot.axes[1].label = unit;
      }
      win.plot.setData(plotData);
    }
  }

  const btn = document.getElementById('btn-burst');
  btn.disabled = false;
  btn.textContent = 'Burst Capture';

  // Handle burst FFT snapshots
  const fftCount = (msg.fft_snapshots && msg.fft_snapshots.length) || 0;
  if (fftCount > 0) {
    _burstFFT = { snapshots: msg.fft_snapshots, index: 0 };
    _showBurstScrubbers();
    _showBurstFFTFrame(0);
    _updateSpectrograms();
  } else {
    _burstFFT = null;
  }

  outInfo.textContent = `Burst: ${n} samples, ${(n / rate).toFixed(3)}s at ${(rate / 1000).toFixed(1)} kHz` +
    (fftCount > 0 ? ` | ${fftCount} FFT snapshots` : '');
}

function handleStatus(msg) {
  dotSensor.className = 'dot ' + (msg.connected ? 'green' : 'red');
  if (msg.device_id) {
    const ip = msg.sensor_ip ? ` (${msg.sensor_ip})` : '';
    lblDevice.textContent = msg.device_id + ip;
  }
  dotLog.className    = 'dot ' + (msg.logging ? 'orange' : '');
  lblLog.textContent  = msg.logging ? 'Logging' : 'Not logging';
  btnLog.textContent  = msg.logging ? 'Stop Logging' : 'Start Logging';
  btnLog.classList.toggle('active', msg.logging);
  loggingActive = msg.logging;
  selFmt.disabled = msg.logging;
  if (msg.log_format) selFmt.value = msg.log_format;

  // Streaming state
  if (msg.streaming !== undefined) {
    const btnStart = document.getElementById('btn-stream-start');
    const btnStop  = document.getElementById('btn-stream-stop');
    btnStart.classList.toggle('active', msg.streaming);
    btnStop.classList.toggle('active', !msg.streaming);
  }

  // Burst state
  if (msg.burst !== undefined) {
    const btn = document.getElementById('btn-burst');
    if (msg.burst) {
      btn.disabled = true;
      btn.textContent = 'Capturing…';
    } else if (btn.disabled) {
      btn.disabled = false;
      btn.textContent = 'Burst Capture';
    }
  }

  // Stats counters
  if (msg.stats) {
    const fmt = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
    document.getElementById('stat-packets').textContent = fmt(msg.stats.packets || 0);
    document.getElementById('stat-frames').textContent  = fmt(msg.stats.frames || 0);
    document.getElementById('stat-samples').textContent = fmt(msg.stats.samples || 0);
    document.getElementById('stat-fft').textContent     = fmt(msg.stats.fft_frames || 0);
  }
}

// ── logging button ────────────────────────────────────────────────────────────
let loggingActive = false;
btnLog.addEventListener('click', async () => {
  const url = loggingActive ? '/api/logging/stop' : '/api/logging/start';
  const r = await fetch(url, { method: 'POST' });
  if (r.ok) loggingActive = !loggingActive;
  selFmt.disabled = loggingActive;
});

// ── log format selector ──────────────────────────────────────────────────────
selFmt.addEventListener('change', async () => {
  const r = await fetch('/api/logging/format', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: selFmt.value }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert(err.detail || 'Failed to change format');
    const cur = await (await fetch('/api/logging/format')).json();
    selFmt.value = cur.format;
  }
});

// ── clear button ──────────────────────────────────────────────────────────────
btnClear.addEventListener('click', () => {
  for (const k of Object.keys(ring)) ring[k] = [];
  _lastFFT = null;
  _burstFFT = null;
  _timeSync.t = [];
  _timeSync.diff_ms = [];
  _showBurstScrubbers();
  _updateSpectrograms();
  for (const win of Object.values(_chartWindows)) {
    if (!win.plot) continue;
    const info = WINDOW_TYPES[win.type];
    if (win.type === 'raw') {
      win.plot.setData(_emptyRawData());
    } else if (info && info.group === 'spectrogram') {
      // already handled by _updateSpectrograms
    } else {
      win.plot.setData([[], []]);
    }
  }
});

// ── export button ────────────────────────────────────────────────────────────
document.getElementById('btn-export').addEventListener('click', () => {
  // Find focused window
  const focused = Object.values(_chartWindows).find(w => w.el.classList.contains('focused'));
  if (!focused) {
    alert('Click a chart window first to select it, then click Export.');
    return;
  }

  const info = WINDOW_TYPES[focused.type];
  let csv = '';
  let filename = '';

  if (focused.type === 'raw') {
    csv = 'time_s,x,y,z\n';
    const n = ring.t.length;
    for (let i = 0; i < n; i++) {
      csv += `${ring.t[i]},${ring.x_last[i] ?? ''},${ring.y_last[i] ?? ''},${ring.z_last[i] ?? ''}\n`;
    }
    filename = 'raw_waveform.csv';
  } else if (info && info.group === 'fft' && _lastFFT) {
    const axis = info.axis;
    const freq = _lastFFT.freq_hz || [];
    const mag = (_lastFFT.magnitudes && _lastFFT.magnitudes[axis]) || [];
    csv = 'freq_hz,magnitude\n';
    for (let i = 0; i < freq.length; i++) {
      csv += `${freq[i]},${mag[i] ?? ''}\n`;
    }
    filename = `fft_${axis}.csv`;
  } else if (info && info.group === 'psd' && _lastFFT) {
    const axis = info.axis;
    const freq = _lastFFT.freq_hz || [];
    const psd = (_lastFFT.psd && _lastFFT.psd[axis]) || [];
    csv = 'freq_hz,psd\n';
    for (let i = 0; i < freq.length; i++) {
      csv += `${freq[i]},${psd[i] ?? ''}\n`;
    }
    filename = `psd_${axis}.csv`;
  } else if (info && info.group === 'spectrogram' && _burstFFT && _burstFFT.snapshots.length > 0) {
    const axis = info.axis;
    const snaps = _burstFFT.snapshots;
    const freq = snaps[0].freq_hz || [];
    csv = 'frame,freq_hz,magnitude\n';
    for (let f = 0; f < snaps.length; f++) {
      const mag = (snaps[f].magnitudes && snaps[f].magnitudes[axis]) || [];
      for (let k = 0; k < freq.length; k++) {
        csv += `${f},${freq[k]},${mag[k] ?? ''}\n`;
      }
    }
    filename = `spectrogram_${axis}.csv`;
  } else if (info && info.group === 'timesync' && _timeSync.t.length > 0) {
    csv = 'host_time_s,diff_ms\n';
    for (let i = 0; i < _timeSync.t.length; i++) {
      csv += `${_timeSync.t[i]},${_timeSync.diff_ms[i]}\n`;
    }
    filename = 'time_sync.csv';
  } else {
    alert('No data to export for this window.');
    return;
  }

  // Trigger download
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
});

// ── window selector ───────────────────────────────────────────────────────────
selWindow.addEventListener('change', () => {
  windowSec = Number(selWindow.value);
});

// ── console toggle ───────────────────────────────────────────────────────────
const consoleFloat = document.getElementById('console-float');

document.getElementById('btn-toggle-console').addEventListener('click', () => {
  consoleFloat.hidden = !consoleFloat.hidden;
});

document.getElementById('btn-close-console').addEventListener('click', () => {
  consoleFloat.hidden = true;
});

// Make console window draggable by its titlebar
(function() {
  const titlebar = consoleFloat.querySelector('.console-float-titlebar');
  let startX, startY, origLeft, origTop;
  titlebar.addEventListener('mousedown', (e) => {
    if (e.target.closest('.chart-window-close')) return;
    e.preventDefault();
    // Switch from bottom/right positioning to left/top
    const rect = consoleFloat.getBoundingClientRect();
    consoleFloat.style.left = rect.left + 'px';
    consoleFloat.style.top = rect.top + 'px';
    consoleFloat.style.bottom = 'auto';
    consoleFloat.style.right = 'auto';
    startX = e.clientX;
    startY = e.clientY;
    origLeft = rect.left;
    origTop = rect.top;
    const onMove = (e) => {
      consoleFloat.style.left = (origLeft + e.clientX - startX) + 'px';
      consoleFloat.style.top = (origTop + e.clientY - startY) + 'px';
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
  titlebar.style.cursor = 'grab';
})();

// ── stats reset ──────────────────────────────────────────────────────────────
document.getElementById('btn-stats-reset').addEventListener('click', async () => {
  await fetch('/api/stats/reset', { method: 'POST' });
});

// ── Phase 2: sensor control ───────────────────────────────────────────────────
function sensorBody(extra = {}) {
  if (!selectedSensor) {
    alert('Select a sensor from the device list first');
    throw new Error('no sensor selected');
  }
  return {
    target_ip: selectedSensor.ip,
    mac:       selectedSensor.mac,
    password:  inPw.value,
    ...extra,
  };
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

document.getElementById('btn-get-info').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/info', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-get-cfg').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/config', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
  if (!d.detail) {
    document.getElementById('cfg-fs').value     = d.full_scale  || '';
    document.getElementById('cfg-axes').value   = d.axes        || '';
    document.getElementById('cfg-odr').value    = d.odr_div     || '';
    const f = d.filter || {};
    document.getElementById('cfg-filt').value   = f.filter_enabled || '';
    document.getElementById('cfg-cutoff').value = f.filter_cutoff  || '';
    if (d.fft_size) document.getElementById('cfg-fft-size').value = d.fft_size;
  }
});

document.getElementById('btn-set-cfg').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/config/set', sensorBody({
    full_scale:     document.getElementById('cfg-fs').value    || null,
    axes:           document.getElementById('cfg-axes').value  || null,
    odr_div:        document.getElementById('cfg-odr').value   || null,
    filter_enabled: document.getElementById('cfg-filt').value  || null,
    filter_cutoff:  document.getElementById('cfg-cutoff').value || null,
    fft_size:       document.getElementById('cfg-fft-size').value || null,
  }));
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-get-net').addEventListener('click', async () => {
  const d = await apiPost('/api/network/config', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
  if (!d.detail) {
    document.getElementById('net-ip').value          = d.ip           || '';
    document.getElementById('net-mask').value         = d.netmask      || '';
    document.getElementById('net-gw').value           = d.gateway      || '';
    document.getElementById('net-server-ip').value    = d.server_ip    || '';
    document.getElementById('net-server-port').value  = d.server_port  ?? '';
    document.getElementById('net-ntp-ip').value       = d.ntp_server_ip || '';
    document.getElementById('net-dhcp').value         = d.dhcp         || 'FEATURE_DISABLED';
    document.getElementById('net-stream').value       = d.data_stream  || 'FEATURE_DISABLED';
    if (d.fft_stream) document.getElementById('net-fft-stream').value = d.fft_stream;
  }
});

document.getElementById('btn-set-net').addEventListener('click', async () => {
  const port = document.getElementById('net-server-port').value;
  const d = await apiPost('/api/network/config/set', sensorBody({
    ip:          document.getElementById('net-ip').value          || null,
    netmask:     document.getElementById('net-mask').value        || null,
    gateway:     document.getElementById('net-gw').value          || null,
    server_ip:   document.getElementById('net-server-ip').value   || null,
    server_port: port ? Number(port) : null,
    ntp_server_ip: document.getElementById('net-ntp-ip').value    || null,
    dhcp:        document.getElementById('net-dhcp').value        || null,
    data_stream: document.getElementById('net-stream').value      || null,
    fft_stream:  document.getElementById('net-fft-stream').value  || null,
  }));
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-stream-start').addEventListener('click', async () => {
  const r = await fetch('/api/stream/start', { method: 'POST' });
  const d = await r.json();
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-stream-stop').addEventListener('click', async () => {
  const r = await fetch('/api/stream/stop', { method: 'POST' });
  const d = await r.json();
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-burst').addEventListener('click', async () => {
  const dur = Number(document.getElementById('sel-burst-duration').value);
  const btn = document.getElementById('btn-burst');
  btn.disabled = true;
  btn.textContent = `Capturing ${dur}s…`;
  try {
    const r = await fetch('/api/burst', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration: dur }),
    });
    const d = await r.json();
    outInfo.textContent = JSON.stringify(d, null, 2);
  } finally {
    // Re-enable after capture duration + a small margin
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = 'Burst Capture';
    }, dur * 1000 + 500);
  }
});

// ── FFT stream buttons ──────────────────────────────────────────────────────
document.getElementById('btn-fft-start').addEventListener('click', async () => {
  const d = await apiPost('/api/stream/fft/start', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-fft-stop').addEventListener('click', async () => {
  const d = await apiPost('/api/stream/fft/stop', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-reset').addEventListener('click', async () => {
  if (!confirm('Reset the sensor?')) return;
  const d = await apiPost('/api/sensor/reset', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
});

// ── device discovery ─────────────────────────────────────────────────────────
async function refreshDevices() {
  try {
    const r = await fetch('/api/devices');
    const devices = await r.json();
    renderDeviceList(devices);
  } catch (e) {
    deviceListEl.innerHTML = '<div class="device-empty">Fetch failed</div>';
  }
}

function renderDeviceList(devices) {
  if (!devices.length) {
    deviceListEl.innerHTML = '<div class="device-empty">No sensors found</div>';
    return;
  }
  deviceListEl.innerHTML = '';
  for (const d of devices) {
    const el = document.createElement('div');
    el.className = 'device-item';
    if (selectedSensor && selectedSensor.mac === d.mac) el.classList.add('selected');

    const modeClass = (d.mode || '').toLowerCase();
    const modeBadge = modeClass ? `<span class="device-mode ${modeClass}">${modeClass}</span>` : '';
    el.innerHTML = `
      <span class="dot ${modeClass === 'app' ? 'green' : modeClass === 'boot' ? 'orange' : ''}" title="${modeClass === 'app' ? 'Application mode — sensor is running' : modeClass === 'boot' ? 'Bootloader mode — sensor is waiting to start' : 'Unknown mode'}"></span>
      <span class="device-mac">${d.mac}</span>
      ${modeBadge}
      <span class="device-ip">${d.ip}</span>
      <button class="btn btn-setup" data-ip="${d.ip}" data-mac="${d.mac}" title="Set server address to this machine">Setup</button>
    `;

    // Auto fast-boot if sensor is in bootloader and setting is enabled
    if (modeClass === 'boot' && _settings.autoBoot) {
      _autoBootSensor(d);
    }

    el.addEventListener('click', (e) => {
      if (e.target.classList.contains('btn-setup')) return;
      selectSensor(d);
    });

    el.querySelector('.btn-setup').addEventListener('click', async (e) => {
      e.stopPropagation();
      selectSensor(d);
      _showSetupModal(d);
    });

    deviceListEl.appendChild(el);
  }
}

function selectSensor(d) {
  selectedSensor = { ip: d.ip, mac: d.mac };
  selectedInfo.textContent = `${d.mac}  (${d.ip})`;
  deviceListEl.querySelectorAll('.device-item').forEach(el => {
    el.classList.toggle('selected', el.querySelector('.device-mac')?.textContent === d.mac);
  });
}

// ── setup modal ──────────────────────────────────────────────────────────────
const setupOverlay = document.getElementById('setup-overlay');
let _setupPending = null; // { device, host }

async function _showSetupModal(device) {
  let host;
  try {
    host = await (await fetch('/api/host')).json();
  } catch {
    outInfo.textContent = 'Failed to get host info';
    return;
  }

  _setupPending = { device, host };

  document.getElementById('setup-server-ip').textContent = host.ip;
  document.getElementById('setup-server-port').textContent = host.tcp_port;
  document.getElementById('setup-ntp-ip').textContent = host.ip;
  document.getElementById('setup-current-ip').textContent = device.ip;
  document.getElementById('setup-info').textContent =
    `Sensor: ${device.mac} (${device.ip})`;

  // Check for link-local or unreachable IP
  const warning = document.getElementById('setup-warning');
  const ip = device.ip;
  const isLinkLocal = ip.startsWith('169.254.');
  const sameSubnet = _isSameSubnet(ip, host.ip, '255.255.255.0');

  const rescue = document.getElementById('setup-rescue');

  if (isLinkLocal) {
    warning.hidden = false;
    warning.textContent =
      'This sensor has a link-local IP (169.254.x.x) which means it has no network config. ' +
      'You should either enable DHCP or set a static IP on the same subnet as this server. ' +
      'Use the rescue command below — Apply Setup will not work from this subnet.';
    rescue.hidden = false;
    _updateRescueCmd();
  } else if (!sameSubnet) {
    warning.hidden = false;
    warning.textContent =
      `This sensor (${ip}) may not be on the same subnet as this server (${host.ip}). ` +
      'After applying, the sensor may become unreachable. Consider setting a static IP on the same subnet or enabling DHCP. ' +
      'Or use the rescue command below.';
    rescue.hidden = false;
    _updateRescueCmd();
  } else {
    warning.hidden = true;
    rescue.hidden = true;
  }

  // Reset radio to "keep"
  document.querySelector('input[name="setup-ip-mode"][value="keep"]').checked = true;
  document.getElementById('setup-static-ip').value = '';

  setupOverlay.hidden = false;
}

function _updateRescueCmd() {
  if (!_setupPending) return;
  const { device, host } = _setupPending;
  const mode = document.querySelector('input[name="setup-ip-mode"]:checked');
  const modeVal = mode ? mode.value : 'dhcp';
  const pw = inPw.value;

  let args = `${device.ip} ${device.mac}`;
  if (pw) args += ` -p "${pw}"`;
  args += ` --server-ip ${host.ip} --server-port ${host.tcp_port}`;

  if (modeVal === 'dhcp') {
    args += ' --dhcp';
  } else if (modeVal === 'static') {
    const staticIp = document.getElementById('setup-static-ip').value.trim() || '192.168.0.50';
    args += ` --ip ${staticIp}`;
  } else {
    // "keep" — default to dhcp for rescue
    args += ' --dhcp';
  }

  const prefix = navigator.platform.startsWith('Win') ? 'python' : 'sudo ~/venv-browser-viewer/bin/python';
  document.getElementById('setup-cmd').textContent = `${prefix} setup_sensor.py ${args}`;
}

function _isSameSubnet(ip1, ip2, mask) {
  const toNum = (ip) => ip.split('.').reduce((a, b) => (a << 8) | Number(b), 0) >>> 0;
  const m = toNum(mask);
  return (toNum(ip1) & m) === (toNum(ip2) & m);
}

// Update rescue command when IP mode or static IP changes
document.querySelectorAll('input[name="setup-ip-mode"]').forEach(r => {
  r.addEventListener('change', _updateRescueCmd);
});
document.getElementById('setup-static-ip').addEventListener('input', _updateRescueCmd);

// Copy rescue command to clipboard
document.getElementById('btn-copy-cmd').addEventListener('click', () => {
  const cmd = document.getElementById('setup-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => {
    const btn = document.getElementById('btn-copy-cmd');
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
});

document.getElementById('btn-setup-confirm').addEventListener('click', async () => {
  if (!_setupPending) return;
  const { device, host } = _setupPending;
  setupOverlay.hidden = true;

  const mode = document.querySelector('input[name="setup-ip-mode"]:checked').value;
  const extra = {
    server_ip:     host.ip,
    server_port:   host.tcp_port,
    ntp_server_ip: host.ip,
    data_stream:   'FEATURE_ENABLED',
  };

  if (mode === 'dhcp') {
    extra.dhcp = 'FEATURE_ENABLED';
  } else if (mode === 'static') {
    const staticIp = document.getElementById('setup-static-ip').value.trim();
    if (!staticIp || !/^\d{1,3}(\.\d{1,3}){3}$/.test(staticIp)) {
      alert('Please enter a valid IP address.');
      return;
    }
    extra.ip = staticIp;
    extra.dhcp = 'FEATURE_DISABLED';
  }

  try {
    const body = sensorBody(extra);
    const res = await apiPost('/api/network/config/set', body);
    outInfo.textContent = 'Setup sent: ' + JSON.stringify(res, null, 2);
  } catch (err) {
    if (err.message !== 'no sensor selected')
      outInfo.textContent = 'Setup error: ' + err.message;
  }
  _setupPending = null;
});

document.getElementById('btn-setup-cancel').addEventListener('click', () => {
  setupOverlay.hidden = true;
  _setupPending = null;
});

setupOverlay.addEventListener('click', (e) => {
  if (e.target === setupOverlay) {
    setupOverlay.hidden = true;
    _setupPending = null;
  }
});

document.getElementById('btn-refresh-devices').addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  refreshDevices();
});

_refreshTimer = setInterval(refreshDevices, 10000);

// ── settings ─────────────────────────────────────────────────────────────────
const _settings = {
  autoBoot: localStorage.getItem('autoBoot') === 'true',
};
const _bootedMacs = new Set(); // avoid sending boot_now repeatedly

function applyTheme(isLight) {
  document.body.classList.toggle('light', isLight);
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  // Recreate all plots (theme affects axis colors)
  for (const win of Object.values(_chartWindows)) {
    if (win.plot) {
      win.plot.destroy();
      const info = WINDOW_TYPES[win.type];
      if (info.group === 'spectrogram') {
        win.canvas.innerHTML = '';
        win.plot = _createSpectrogramPlot(win.canvas, info);
      } else if (info.group === 'timesync') {
        win.canvas.innerHTML = '';
        win.plot = _createTimeSyncPlot(win.canvas);
      } else {
        win.plot = _createPlot(win.type, win.canvas, info);
      }
    }
  }
}

// Settings modal
const settingsOverlay = document.getElementById('settings-overlay');
const chkLightTheme = document.getElementById('chk-light-theme');
const chkAutoBoot = document.getElementById('chk-auto-boot');

document.getElementById('btn-settings').addEventListener('click', () => {
  chkLightTheme.checked = document.body.classList.contains('light');
  chkAutoBoot.checked = _settings.autoBoot;
  settingsOverlay.hidden = false;
});

document.getElementById('btn-settings-close').addEventListener('click', () => {
  settingsOverlay.hidden = true;
});

settingsOverlay.addEventListener('click', (e) => {
  if (e.target === settingsOverlay) settingsOverlay.hidden = true;
});

chkLightTheme.addEventListener('change', () => {
  applyTheme(chkLightTheme.checked);
});

chkAutoBoot.addEventListener('change', () => {
  _settings.autoBoot = chkAutoBoot.checked;
  localStorage.setItem('autoBoot', chkAutoBoot.checked);
});

// Apply saved theme on load
if (localStorage.getItem('theme') === 'light') {
  applyTheme(true);
}

// Auto fast-boot
async function _autoBootSensor(device) {
  if (_bootedMacs.has(device.mac)) return;
  _bootedMacs.add(device.mac);
  try {
    const pw = document.getElementById('in-pw').value;
    await apiPost('/api/sensor/boot', {
      target_ip: device.ip,
      mac: device.mac,
      password: pw,
    });
    outInfo.textContent = `Auto fast-boot sent to ${device.mac} (${device.ip})`;
  } catch (err) {
    outInfo.textContent = `Auto fast-boot failed for ${device.mac}: ${err.message}`;
  }
}

// ── host info in topbar ──────────────────────────────────────────────────────
async function updateHostInfo() {
  try {
    const host = await (await fetch('/api/host')).json();
    const ifLabel = host.if_name ? ` [${host.if_name}]` : '';
    hostInfo.textContent = `This host (Server): ${host.ip}:${host.tcp_port}${ifLabel}`;
  } catch {
    hostInfo.textContent = 'This host (Server): unavailable';
  }
}

// ── password persistence & toggle ─────────────────────────────────────────────
inPw.value = localStorage.getItem('sensorPw') || '';
inPw.addEventListener('input', () => localStorage.setItem('sensorPw', inPw.value));

document.getElementById('btn-toggle-pw').addEventListener('click', () => {
  const show = inPw.type === 'password';
  inPw.type = show ? 'text' : 'password';
});

// ── init ──────────────────────────────────────────────────────────────────────

// Restore saved windows or create a default raw window
const savedState = _loadWindowState();
const savedKeys = Object.keys(savedState);
if (savedKeys.length > 0) {
  for (const s of Object.values(savedState)) {
    if (WINDOW_TYPES[s.type]) {
      createChartWindow(s.type, { x: s.x, y: s.y, w: s.w, h: s.h });
    }
  }
} else {
  // First visit: create one raw waveform window filling most of the area
  requestAnimationFrame(() => {
    const areaW = chartsArea.clientWidth || 800;
    const areaH = chartsArea.clientHeight || 400;
    createChartWindow('raw', { x: 10, y: 10, w: areaW - 20, h: areaH - 20 });
  });
}

connect();
refreshDevices();
updateHostInfo();

fetch('/api/logging/format').then(r => r.json()).then(d => {
  selFmt.value = d.format;
}).catch(() => {});
