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
// No fixed MAX_PTS — ring is trimmed by time window in handleFrame
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

// Default X-axis time span (seconds) for new raw windows. Each raw window can
// override this via its per-window settings (win.windowSec).
let _defaultWindowSec = 5;

function _maxRawWindowSec() {
  let m = 0;
  for (const w of Object.values(_chartWindows)) {
    if (w.type === 'raw') m = Math.max(m, w.windowSec || _defaultWindowSec);
  }
  return m || _defaultWindowSec;
}

// Latest FFT/PSD data (shared across all windows of that type)
let _lastFFT = null;  // { freq_hz, magnitudes, psd, unit, fft_bins, fft_size }
let _burstFFT = null; // { snapshots: [...], index: 0 } — set after burst capture
let _capturing = false; // true while a burst capture is in progress (from backend status)

// Time sync ring buffer
const _timeSync = { t: [], diff_ms: [] };
const MAX_TIMESYNC_PTS = 600; // ~10 minutes at 1 Hz status rate

// ── DOM refs ──────────────────────────────────────────────────────────────────
const dotSensor = document.getElementById('dot-sensor');
const dotLog    = document.getElementById('dot-log');
const dotWs     = document.getElementById('dot-ws');
const dotTsync  = document.getElementById('dot-tsync');
const lblDevice = document.getElementById('lbl-device');
const lblRate   = document.getElementById('lbl-rate');
const lblLog    = document.getElementById('lbl-log');
const lblTsync  = document.getElementById('lbl-tsync');
const lblWs     = document.getElementById('lbl-ws');
const btnLog    = document.getElementById('btn-log');
const selFmt    = document.getElementById('sel-log-format');
const btnClear  = document.getElementById('btn-clear');
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

// ── Log-scale helpers (FFT/PSD/spectrogram) ───────────────────────────────────
// A log magnitude scale is needed to actually see the float32 FFT's dynamic
// range — small signals (nearby ACs, HVAC, faint mechanical vibration) that
// Q15 rounded to zero populate many orders of magnitude below the peaks.
const _LOG_FLOOR = 1e-9;  // floor so log10(0) → finite, avoids -Infinity

// Convert a magnitude/PSD array to a dB-like log scale.
// FFT magnitude → 20·log10, PSD (already power) → 10·log10.
function _toLog(arr, group) {
  const k = group === 'psd' ? 10 : 20;
  const out = new Array(arr.length);
  for (let i = 0; i < arr.length; i++) {
    out[i] = k * Math.log10(Math.max(arr[i], _LOG_FLOOR));
  }
  return out;
}

// Resolve an axis from an {x,y,z} dict, falling back to the sole column in
// vector modes (single unsigned column labelled by its vector axis).
function _resolveAxisData(dict, axis) {
  if (!dict) return null;
  if (dict[axis]) return dict[axis];
  const keys = Object.keys(dict);
  if (keys.length === 1) return dict[keys[0]];
  return null;
}

// Re-render one FFT/PSD/spectrogram window (e.g. after its log toggle changes).
function _rerenderSpectralWindow(win) {
  const info = WINDOW_TYPES[win.type];
  if (!info || !win.plot) return;
  if (info.group === 'spectrogram') {
    win.plot._log = !!win.logScale;
    win.plot.update(_burstFFT ? _burstFFT.snapshots : []);
  } else if (info.group === 'fft' || info.group === 'psd') {
    // Keep the Y-axis label in sync with log mode (dB vs linear unit).
    if (win.plot.axes && win.plot.axes[1]) {
      win.plot.axes[1].label = _yAxisLabel(info.group, win.logScale);
    }
    if (_lastFFT) {
      const dict = info.group === 'fft' ? _lastFFT.magnitudes : _lastFFT.psd;
      let data = _resolveAxisData(dict, info.axis);
      if (data) {
        if (win.logScale) data = _toLog(data, info.group);
        win.plot.setData([_lastFFT.freq_hz, data]);
      }
    }
  }
}

// ── Per-window Y-axis scaling ─────────────────────────────────────────────────
// uPlot calls a scale's range function once per setData (per frame), NOT per
// point — so reading win.yScale here is cheap. We close over `win`, so no DOM
// lookup happens in the hot path; toggling a window's setting is picked up on
// the next frame automatically.
function _autoPadY(dMin, dMax) {
  // Fit-to-data with ~10% headroom (mirrors uPlot's default auto feel), but
  // version-independent so auto mode can't silently break.
  if (dMin == null || dMax == null || !isFinite(dMin) || !isFinite(dMax)) return [dMin, dMax];
  if (dMin === dMax) { const p = Math.abs(dMax) * 0.1 || 1; return [dMin - p, dMax + p]; }
  const pad = (dMax - dMin) * 0.1;
  return [dMin - pad, dMax + pad];
}

function _yRange(win, dMin, dMax) {
  const ys = win && win.yScale;
  if (ys && ys.mode === 'fixed' && ys.max != null && isFinite(ys.max)) {
    const lo = (ys.min != null && isFinite(ys.min)) ? ys.min : 0;
    return [lo, ys.max];
  }
  return _autoPadY(dMin, dMax);  // auto: fit data with headroom
}

// Current [min,max] of the data shown in a window — used by the settings modal's
// "Fit to data" button to seed a sensible fixed range.
function _currentRangeForWindow(win) {
  const info = WINDOW_TYPES[win.type];
  if (!info) return null;
  let lo = Infinity, hi = -Infinity;
  const scan = (arr) => { for (const v of arr) { if (v == null) continue; if (v < lo) lo = v; if (v > hi) hi = v; } };
  if (info.group === 'fft' || info.group === 'psd') {
    if (!_lastFFT) return null;
    const dict = info.group === 'fft' ? _lastFFT.magnitudes : _lastFFT.psd;
    let arr = _resolveAxisData(dict, info.axis);
    if (!arr) return null;
    if (win.logScale) arr = _toLog(arr, info.group);
    scan(arr);
  } else if (win.type === 'raw') {
    scan(ring.x_last); scan(ring.y_last); scan(ring.z_last);
  } else {
    return null;
  }
  return isFinite(lo) && isFinite(hi) ? [lo, hi] : null;
}

// Re-apply a window's plot data so a settings change (Y-scale / window length)
// takes effect immediately, without waiting for the next frame.
function _refreshWindow(win) {
  if (!win.plot) return;
  try {
    if (win.type === 'raw') win.plot.setData(_buildRawPlotData(win.windowSec));
    else if (win.plot.data) win.plot.setData(win.plot.data);
  } catch (e) { console.error('refresh window failed', win.type, e); }
}

// ── Floating window manager ──────────────────────────────────────────────────
let _chartWindows = {};  // id → { id, type, el, canvas, plot, x, y, w, h, logScale, yScale, windowSec }
let _nextWinId = 1;
let _topZ = 10;

function _saveWindowState() {
  const state = {};
  for (const [id, win] of Object.entries(_chartWindows)) {
    state[id] = { id: win.id, type: win.type, x: win.x, y: win.y, w: win.w, h: win.h,
                  logScale: !!win.logScale, yScale: win.yScale, windowSec: win.windowSec };
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

  // Settings gear — Y-scale (auto/fixed), log scale, window length. Everything
  // configurable per window now lives in the settings modal (no separate
  // titlebar buttons). Time-sync windows have nothing to configure.
  if (info.group !== 'timesync') {
    const gearBtn = document.createElement('button');
    gearBtn.className = 'chart-window-gear';
    gearBtn.innerHTML = '&#9881;';  // ⚙
    gearBtn.title = 'Window settings — Y-axis scale, log scale, window length';
    gearBtn.addEventListener('mousedown', (e) => e.stopPropagation());
    gearBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openWindowSettings(id);
    });
    titlebar.appendChild(gearBtn);
  }

  titlebar.appendChild(closeBtn);

  const canvas = document.createElement('div');
  canvas.className = 'chart-window-canvas';

  el.appendChild(titlebar);

  // Live values + stats panel for raw waveform
  if (type === 'raw') {
    const valuesDiv = document.createElement('div');
    valuesDiv.className = 'raw-values-info';
    valuesDiv.innerHTML =
      '<table class="rv-table"><thead>' +
      '<tr><th></th><th class="leg-x">X</th><th class="leg-y">Y</th><th class="leg-z">Z</th></tr>' +
      '</thead><tbody>' +
      '<tr><td class="rv-label">Min</td><td class="rv-val" data-s="min-x">—</td><td class="rv-val" data-s="min-y">—</td><td class="rv-val" data-s="min-z">—</td></tr>' +
      '<tr><td class="rv-label">Max</td><td class="rv-val" data-s="max-x">—</td><td class="rv-val" data-s="max-y">—</td><td class="rv-val" data-s="max-z">—</td></tr>' +
      '<tr><td class="rv-label">Avg</td><td class="rv-val" data-s="avg-x">—</td><td class="rv-val" data-s="avg-y">—</td><td class="rv-val" data-s="avg-z">—</td></tr>' +
      '<tr><td class="rv-label">RMS</td><td class="rv-val" data-s="rms-x">—</td><td class="rv-val" data-s="rms-y">—</td><td class="rv-val" data-s="rms-z">—</td></tr>' +
      '</tbody></table>';
    el.appendChild(valuesDiv);
  }

  el.appendChild(canvas);

  // Legend + zoom hint for raw waveform
  if (type === 'raw') {
    const legend = document.createElement('div');
    legend.className = 'chart-window-legend';
    legend.innerHTML = '<span class="leg-x">— X</span><span class="leg-y">— Y</span><span class="leg-z">— Z</span>' +
      '<span class="zoom-hint" hidden>Zoomed — double-click to reset</span>';
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

  // Edge/corner resize handles
  for (const edge of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
    const h = document.createElement('div');
    h.className = `cw-edge cw-edge-${edge}`;
    h.dataset.edge = edge;
    el.appendChild(h);
  }

  chartsArea.appendChild(el);

  const win = {
    id, type, el, canvas, plot: null,
    x: pos.x, y: pos.y, w: pos.w, h: pos.h,
    logScale: !!pos.logScale,
    // Per-window settings (read efficiently via closures in the plot scales).
    yScale: (pos.yScale && pos.yScale.mode) ? { mode: pos.yScale.mode, min: pos.yScale.min ?? null, max: pos.yScale.max ?? null } : { mode: 'auto', min: null, max: null },
    windowSec: pos.windowSec || _defaultWindowSec,
  };
  _chartWindows[id] = win;

  // Create plot after a frame so the canvas has dimensions
  requestAnimationFrame(() => {
    if (info.group === 'spectrogram') {
      win.plot = _createSpectrogramPlot(canvas, info);
      win.plot._log = !!win.logScale;
    } else if (info.group === 'timesync') {
      win.plot = _createTimeSyncPlot(canvas);
    } else {
      win.plot = _createPlot(type, canvas, info, win);
    }
    _saveWindowState();
  });

  // ── Drag ──
  _setupDrag(titlebar, win);

  // ── Resize from all edges/corners ──
  el.querySelectorAll('.cw-edge').forEach(h => _setupEdgeResize(h, win));

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

function _setupEdgeResize(handle, win) {
  const edge = handle.dataset.edge;
  const resizeN = edge.includes('n');
  const resizeS = edge.includes('s');
  const resizeW = edge.includes('w');
  const resizeE = edge.includes('e');

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX, startY = e.clientY;
    const origX = win.x, origY = win.y, origW = win.w, origH = win.h;
    const areaW = chartsArea.clientWidth;
    const areaH = chartsArea.clientHeight;

    const onMove = (e) => {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      if (resizeE) win.w = Math.max(200, Math.min(origW + dx, areaW - win.x));
      if (resizeS) win.h = Math.max(140, Math.min(origH + dy, areaH - win.y));
      if (resizeW) {
        const newW = Math.max(200, origW - dx);
        win.x = origX + origW - newW;
        win.w = newW;
        if (win.x < 0) { win.w += win.x; win.x = 0; }
      }
      if (resizeN) {
        const newH = Math.max(140, origH - dy);
        win.y = origY + origH - newH;
        win.h = newH;
        if (win.y < 0) { win.h += win.y; win.y = 0; }
      }

      win.el.style.left   = win.x + 'px';
      win.el.style.top    = win.y + 'px';
      win.el.style.width  = win.w + 'px';
      win.el.style.height = win.h + 'px';
      if (win.plot && win.canvas.clientWidth > 0) {
        win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
      }
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      win.x = snap(win.x); win.y = snap(win.y);
      win.w = snap(Math.max(200, win.w));
      win.h = snap(Math.max(140, win.h));
      win.el.style.left   = win.x + 'px';
      win.el.style.top    = win.y + 'px';
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
// Sort windows by WINDOW_TYPES definition order
function _sortedWins() {
  const order = Object.keys(WINDOW_TYPES);
  return Object.values(_chartWindows).sort((a, b) =>
    order.indexOf(a.type) - order.indexOf(b.type)
  );
}

function _applyLayout(win, x, y, w, h) {
  const pad = 4;
  win.x = x + pad; win.y = y + pad;
  win.w = w - pad * 2; win.h = h - pad * 2;
  win.el.style.left   = win.x + 'px';
  win.el.style.top    = win.y + 'px';
  win.el.style.width  = win.w + 'px';
  win.el.style.height = win.h + 'px';
  if (win.plot) {
    win.plot.setSize({ width: win.canvas.clientWidth, height: win.canvas.clientHeight });
  }
}

function tileWindows() {
  const wins = _sortedWins();
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
  const wins = _sortedWins();
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

// ── Layout presets ───────────────────────────────────────────────────────────
function _clearAllWindows() {
  for (const id of Object.keys(_chartWindows)) destroyChartWindow(id);
}

const LAYOUT_PRESETS = {
  'raw-fft': {
    label: 'Raw + FFT',
    windows: ['raw', 'fftX', 'fftY', 'fftZ', 'timeSync'],
    arrange(wins, W, H) {
      // 40% raw, 40% FFT×3, 20% time sync (half width)
      const r1H = Math.round(H * 0.4);
      const r2H = Math.round(H * 0.4);
      const r3H = H - r1H - r2H;
      const colW = Math.round(W / 3);
      _applyLayout(wins[0], 0, 0, W, r1H);              // Raw full width
      _applyLayout(wins[1], 0, r1H, colW, r2H);          // FFT X
      _applyLayout(wins[2], colW, r1H, colW, r2H);       // FFT Y
      _applyLayout(wins[3], colW * 2, r1H, W - colW * 2, r2H); // FFT Z
      _applyLayout(wins[4], 0, r1H + r2H, Math.round(W / 2), r3H); // Time Sync
    },
  },
  'raw-fft-psd': {
    label: 'Raw + FFT + PSD',
    windows: ['raw', 'fftX', 'fftY', 'fftZ', 'psdX', 'psdY', 'psdZ'],
    arrange(wins, W, H) {
      const r1H = Math.round(H * 0.34);
      const r2H = Math.round(H * 0.33);
      const r3H = H - r1H - r2H;
      const colW = Math.round(W / 3);
      _applyLayout(wins[0], 0, 0, W, r1H);
      _applyLayout(wins[1], 0, r1H, colW, r2H);
      _applyLayout(wins[2], colW, r1H, colW, r2H);
      _applyLayout(wins[3], colW * 2, r1H, W - colW * 2, r2H);
      _applyLayout(wins[4], 0, r1H + r2H, colW, r3H);
      _applyLayout(wins[5], colW, r1H + r2H, colW, r3H);
      _applyLayout(wins[6], colW * 2, r1H + r2H, W - colW * 2, r3H);
    },
  },
  'raw-only': {
    label: 'Raw Only',
    windows: ['raw'],
    arrange(wins, W, H) {
      _applyLayout(wins[0], 0, 0, W, H);
    },
  },
  'fft-only': {
    label: 'FFT X/Y/Z',
    windows: ['fftX', 'fftY', 'fftZ'],
    arrange(wins, W, H) {
      const cellH = Math.round(H / 3);
      _applyLayout(wins[0], 0, 0, W, cellH);
      _applyLayout(wins[1], 0, cellH, W, cellH);
      _applyLayout(wins[2], 0, cellH * 2, W, H - cellH * 2);
    },
  },
};

function applyLayoutPreset(presetId) {
  const preset = LAYOUT_PRESETS[presetId];
  if (!preset) return;

  _clearAllWindows();

  const wins = [];
  for (const type of preset.windows) {
    const win = createChartWindow(type);
    if (win) wins.push(win);
  }

  // Apply layout after plots are created
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      const W = chartsArea.clientWidth;
      const H = chartsArea.clientHeight;
      preset.arrange(wins, W, H);
      _saveWindowState();
    });
  });
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

      // Optional log-magnitude scale — reveals the float32 FFT's low-level
      // content that a linear scale flattens against the peaks.
      const useLog = !!obj._log;
      const tx = useLog ? (v) => Math.log10(Math.max(v, _LOG_FLOOR)) : (v) => v;

      // Find global min/max for color normalization
      let gMin = Infinity, gMax = -Infinity;
      for (const snap of snapshots) {
        const mag = snap.magnitudes[axis];
        if (!mag) continue;
        for (let k = 0; k < mag.length; k++) {
          const v = tx(mag[k]);
          if (v < gMin) gMin = v;
          if (v > gMax) gMax = v;
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
          const t = (tx(mag[col]) - gMin) / (gMax - gMin);
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

  // Update FFT/PSD windows (with vector fallback)
  const _resolveAxis = (dict, axis) => {
    if (!dict) return null;
    if (dict[axis]) return dict[axis];
    const keys = Object.keys(dict);
    if (keys.length === 1) return dict[keys[0]];
    return null;
  };
  for (const win of Object.values(_chartWindows)) {
    if (!win.plot) continue;
    const info = WINDOW_TYPES[win.type];
    if (!info) continue;
    if (info.group === 'fft') {
      let data = _resolveAxis(snap.magnitudes, info.axis);
      if (data) { if (win.logScale) data = _toLog(data, 'fft'); win.plot.setData([freq, data]); }
    } else if (info.group === 'psd') {
      let data = _resolveAxis(snap.psd, info.axis);
      if (data) { if (win.logScale) data = _toLog(data, 'psd'); win.plot.setData([freq, data]); }
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

// Compact Y-axis tick label: scientific notation for very small / very large
// magnitudes (e.g. PSD values ~1e-7 that otherwise round to "0" on a linear
// axis), plain digits in between.
function _fmtSci(v) {
  if (v == null || !isFinite(v)) return '';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a < 1e-3 || a >= 1e5) return v.toExponential(1);   // e.g. "3.0e-7"
  return String(+v.toPrecision(4));
}
const _sciYValues = (u, splits) => splits.map(_fmtSci);

// Y-axis label for FFT/PSD windows. In log mode the plotted values are dB
// (PSD: 10·log10 re 1 (m/s²)²/Hz; FFT: 20·log10 re 1 m/s²), so the label
// must say dB — not the linear unit.
function _yAxisLabel(group, logScale) {
  const unit = group === 'psd' ? '(m/s²)²/Hz' : (group === 'fft' ? 'm/s²' : '');
  if (!unit) return '';
  return logScale ? `dB re 1 ${unit}` : unit;
}

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

function _createPlot(type, canvas, info, win) {
  const w = canvas.clientWidth  || 400;
  const h = canvas.clientHeight || 200;

  if (type === 'raw') return _createRawPlot(w, h, canvas, win);

  // FFT or PSD — single axis line chart
  const yLabel = _yAxisLabel(info.group, win && win.logScale);
  const color = info.color;
  const opts = {
    width: w, height: h,
    pxAlign: false,
    cursor: { show: true, drag: { x: true, y: true } },
    legend: { show: false },
    plugins: [wheelZoomPlugin()],
    axes: [
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID, label: 'Hz' },
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID_Y, label: yLabel, values: _sciYValues },
    ],
    series: [
      {},
      { stroke: color, width: 1.5, paths: uPlot.paths.linear(), fill: color + '22' },
    ],
    scales: { x: { time: false }, y: { auto: true, range: (u, dMin, dMax) => _yRange(win, dMin, dMax) } },
  };
  return new uPlot(opts, [[], []], canvas);
}

function _createRawPlot(w, h, canvas, win) {
  const opts = {
    width: w, height: h,
    pxAlign: false,
    cursor: { show: true, drag: { x: true, y: true } },
    legend: { show: false },
    plugins: [wheelZoomPlugin()],
    hooks: {
      setScale: [(_self, key) => {
        if (key === 'x') {
          _rawStatsTimer = 0;  // force immediate stats update
          _updateRawStats();
        }
      }],
    },
    axes: [
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID },
      { stroke: _axisStroke(), ticks: { stroke: _tickStroke() }, grid: _GRID_Y, values: _sciYValues },
    ],
    series: [
      {},
      { stroke: '#e05252', width: 1.5, paths: uPlot.paths.linear() },
      { stroke: '#52c45a', width: 1.5, paths: uPlot.paths.linear() },
      { stroke: '#5296e0', width: 1.5, paths: uPlot.paths.linear() },
    ],
    scales: { x: { time: false }, y: { auto: true, range: (u, dMin, dMax) => _yRange(win, dMin, dMax) } },
  };
  return new uPlot(opts, _emptyRawData(), canvas);
}

function _emptyRawData() {
  return [[], [], [], []];
}

function _buildRawPlotData(span) {
  const t  = ring.t;
  const now = t.length ? t[t.length - 1] : 0;
  const cut = now - (span || _defaultWindowSec);
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

document.getElementById('sel-layout').addEventListener('change', (e) => {
  const val = e.target.value;
  if (val) applyLayoutPreset(val);
  e.target.selectedIndex = 0;  // reset to "Layout" placeholder
});

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

    // Guard each handler so one bad message can't wedge the socket loop.
    try {
      if (msg.type === 'frame')  handleFrame(msg);
      if (msg.type === 'fft')    handleFFT(msg);
      if (msg.type === 'status') handleStatus(msg);
      if (msg.type === 'burst')  handleBurst(msg);
    } catch (e) {
      console.error('WS handler error for', msg.type, e);
    }
  };
}

// Find acceleration columns dynamically (handles accel_x, accel_xy_vec, etc.)
function _findAccelCols(obj) {
  const keys = Object.keys(obj).filter(k => k.startsWith('accel_'));
  // Map to x/y/z slots: accel_x→x, accel_y→y, accel_z→z, vector→x (single channel)
  let x = null, y = null, z = null;
  for (const k of keys) {
    const suffix = k.slice(6); // after 'accel_'
    if (suffix === 'x') x = k;
    else if (suffix === 'y') y = k;
    else if (suffix === 'z') z = k;
    else if (!x) x = k; // vector or other single-channel → put in x slot
  }
  return { x, y, z };
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
    const cols = _findAccelCols(rs);
    const x = cols.x ? rs[cols.x] : [];
    const y = cols.y ? rs[cols.y] : [];
    const z = cols.z ? rs[cols.z] : [];
    const n = Math.max(x.length, y.length, z.length);
    if (n === 0) return;

    const tEnd = msg.t_end_ns / 1e9;
    const rate = msg.rate_hz || _rateHz || 1;
    const tStart = tEnd - (n - 1) / rate;
    for (let i = 0; i < n; i++) {
      ring.t.push(tStart + i / rate);
      ring.x_last.push(i < x.length ? x[i] : null);
      ring.y_last.push(i < y.length ? y[i] : null);
      ring.z_last.push(i < z.length ? z[i] : null);
    }
  } else {
    // Decimated mode (high ODR — min/max/last envelope)
    const axes = msg.axes || {};
    if (Object.keys(axes).length === 0) return;

    const cols = _findAccelCols(axes);
    const tSec = msg.t_end_ns / 1e9;
    const get = (label, key) => {
      if (!label) return null;
      const a = axes[label];
      return (a && key in a) ? a[key] : null;
    };

    const xLast = get(cols.x, 'last');
    const yLast = get(cols.y, 'last');
    const zLast = get(cols.z, 'last');

    if (xLast === null && yLast === null && zLast === null) return;

    ring.t.push(tSec);
    ring.x_last.push(xLast);
    ring.y_last.push(yLast);
    ring.z_last.push(zLast);
  }

  // Trim ring to the largest window span in use + small margin (not during burst)
  if (ring.t.length > 0 && !_burstFFT) {
    const tKeep = ring.t[ring.t.length - 1] - _maxRawWindowSec() - 1;
    while (ring.t.length > 1 && ring.t[0] < tKeep) {
      for (const k of Object.keys(ring)) ring[k].shift();
    }
  }

  // Update all raw windows — each uses its own window length for the X span.
  // Slicing per window is cheap (a few windows, O(n) each) and keeps the
  // per-window span independent without touching the shared ring.
  for (const win of Object.values(_chartWindows)) {
    if (win.type === 'raw' && win.plot) {
      if (msg.units) {
        const unit = msg.units['accel_x'] || msg.units['accel_y'] || msg.units['accel_z']
                  || msg.units['force_x'] || msg.units[Object.keys(msg.units)[0]] || '';
        if (unit) {
          win.plot.axes[1].label = unit;
        }
      }
      win.plot.setData(_buildRawPlotData(win.windowSec));
    }
  }

  // Throttled stats update (~2 Hz)
  _updateRawStats();
}

let _rawStatsTimer = 0;
function _updateRawStats() {
  const now = performance.now();
  if (now - _rawStatsTimer < 500) return;
  _rawStatsTimer = now;

  const n = ring.t.length;
  if (n === 0) return;

  // Determine visible X range — check if any raw window is zoomed
  let xMin = -Infinity, xMax = Infinity;
  let isZoomed = false;
  for (const win of Object.values(_chartWindows)) {
    if (win.type === 'raw' && win.plot && win.plot.scales) {
      const sx = win.plot.scales.x;
      if (sx.min != null && sx.max != null) {
        const dataMin = ring.t[0];
        const dataMax = ring.t[n - 1];
        // Consider zoomed if scale is noticeably narrower than data range
        if ((sx.max - sx.min) < (dataMax - dataMin) * 0.95) {
          xMin = sx.min;
          xMax = sx.max;
          isZoomed = true;
        }
      }
      // Show/hide zoom hint
      const hint = win.el.querySelector('.zoom-hint');
      if (hint) hint.hidden = !isZoomed;
      break; // use first raw window's zoom state
    }
  }

  // Compute stats over visible range
  let i0 = 0, iEnd = n;
  if (isZoomed) {
    for (let i = 0; i < n; i++) { if (ring.t[i] >= xMin) { i0 = i; break; } }
    for (let i = n - 1; i >= i0; i--) { if (ring.t[i] <= xMax) { iEnd = i + 1; break; } }
  } else if (!_burstFFT) {
    const tNow = ring.t[n - 1];
    const tCut = tNow - _maxRawWindowSec();
    for (let i = 0; i < n; i++) { if (ring.t[i] >= tCut) { i0 = i; break; } }
  }

  const stats = {};
  for (const [key, arr] of [['x', ring.x_last], ['y', ring.y_last], ['z', ring.z_last]]) {
    let min = Infinity, max = -Infinity, sum = 0, sqSum = 0, count = 0;
    for (let i = i0; i < iEnd; i++) {
      const v = arr[i];
      if (v == null) continue;
      if (v < min) min = v;
      if (v > max) max = v;
      sum += v;
      sqSum += v * v;
      count++;
    }
    if (count > 0) {
      stats[key] = {
        last: arr[n - 1],
        min, max,
        avg: sum / count,
        rms: Math.sqrt(sqSum / count),
      };
    }
  }

  const f = (v) => v != null ? v.toFixed(3) : '—';
  for (const win of Object.values(_chartWindows)) {
    if (win.type !== 'raw') continue;
    const vp = win.el.querySelector('.raw-values-info');
    if (!vp) continue;
    for (const axis of ['x', 'y', 'z']) {
      const s = stats[axis];
      vp.querySelector(`[data-s="min-${axis}"]`).textContent  = s ? f(s.min) : '—';
      vp.querySelector(`[data-s="max-${axis}"]`).textContent  = s ? f(s.max) : '—';
      vp.querySelector(`[data-s="avg-${axis}"]`).textContent  = s ? f(s.avg) : '—';
      vp.querySelector(`[data-s="rms-${axis}"]`).textContent  = s ? f(s.rms) : '—';
    }
  }
}

function handleFFT(msg) {

  let freq = msg.freq_hz;
  const mags = msg.magnitudes;
  const psd  = msg.psd;

  if (!freq || !mags) return;

  // Collect time sync data from FFT frames (works even without raw data stream)
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
  // Resolve axis: use exact match, or fall back to the only available axis (vector mode)
  const _resolveAxis = (dict, axis) => {
    if (!dict) return null;
    if (dict[axis]) return dict[axis];
    const keys = Object.keys(dict);
    if (keys.length === 1) return dict[keys[0]];
    return null;
  };

  for (const win of Object.values(_chartWindows)) {
    if (!win.plot) continue;
    const info = WINDOW_TYPES[win.type];
    if (!info) continue;

    // Per-window guard: an error updating one window must not skip the others
    // (e.g. it would otherwise prevent later axes like Z from ever rendering).
    try {
      if (info.group === 'fft') {
        let data = _resolveAxis(mags, info.axis);
        if (data) { if (win.logScale) data = _toLog(data, 'fft'); win.plot.setData([freq, data]); }
      } else if (info.group === 'psd') {
        let data = _resolveAxis(psd, info.axis);
        if (data) { if (win.logScale) data = _toLog(data, 'psd'); win.plot.setData([freq, data]); }
      }
    } catch (e) {
      console.error('FFT/PSD window update failed', win.type, e);
    }
  }
}

function handleBurst(msg) {
  // Cancelled or timed-out capture (e.g. sensor not connected → no data).
  if (msg.cancelled) {
    _setBurstButton(false);
    if (msg.reason === 'timeout') {
      showDialog({
        title: 'Burst capture timed out',
        message: 'No data was received during the capture window. Is the sensor connected and streaming? Check the sensor status, then try again.',
      });
    } else {
      outInfo.textContent = 'Burst capture cancelled.';
    }
    return;
  }
  const samples = msg.samples || {};
  const rate = msg.sample_rate_hz || _rateHz || 26667;
  const cols = _findAccelCols(samples);
  const x = cols.x ? samples[cols.x] : [];
  const y = cols.y ? samples[cols.y] : [];
  const z = cols.z ? samples[cols.z] : [];
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

  // Force stats update with burst data (no throttle)
  _rawStatsTimer = 0;
  _updateRawStats();

  _setBurstButton(false);

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

  // Sensor time-sync state (absent on older backends → leave as unknown).
  if (msg.time_sync !== undefined) { _lastTimeSync = msg.time_sync; _renderTimeSync(); }

  // Streaming state
  if (msg.streaming !== undefined) {
    const btnStart = document.getElementById('btn-stream-start');
    const btnStop  = document.getElementById('btn-stream-stop');
    btnStart.classList.toggle('active', msg.streaming);
    btnStop.classList.toggle('active', !msg.streaming);
  }

  // Burst state — the backend is authoritative (survives a frontend reload and
  // the frame-independent timeout), so always mirror msg.burst onto the button.
  if (msg.burst !== undefined && msg.burst !== _capturing) {
    _setBurstButton(!!msg.burst);
  }

  // Stats counters
  if (msg.stats) {
    const fmt = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
    document.getElementById('stat-packets').textContent = fmt(msg.stats.packets || 0);
    document.getElementById('stat-frames').textContent  = fmt(msg.stats.frames || 0);
    document.getElementById('stat-samples').textContent = fmt(msg.stats.samples || 0);
    document.getElementById('stat-fft').textContent     = fmt(msg.stats.fft_frames || 0);
    const lossEl = document.getElementById('stat-udp-loss');
    if (lossEl) {
      const lost = msg.stats.udp_lost_chunks || 0;
      lossEl.textContent = fmt(lost);
      lossEl.classList.toggle('stat-warn', lost > 0);
    }
    // Per-stream transport indicator (auto-detected from live data).
    const setTr = (id, v) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = v ? v.toUpperCase() : '—';
      el.classList.toggle('tr-udp', v === 'udp');
      el.classList.toggle('tr-tcp', v === 'tcp');
      el.classList.toggle('tr-idle', !v);
    };
    setTr('tr-raw', msg.stats.raw_transport);
    setTr('tr-fft', msg.stats.fft_transport);
  }

  // Traffic Monitor panel (no-op unless the panel is open).
  _updateMonitor(msg);
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

// ── Traffic Monitor panel ─────────────────────────────────────────────────────
const monitorFloat = document.getElementById('monitor-float');
let _monitorMode = false;
// Rolling snapshots for SMOOTHED rates (not an instantaneous delta, which would
// dip to 0 for low-rate streams like FFT between status updates).
let _monHist = [];              // [{ t, rawRecv, fftRecv, samples, dgrams }]
const _MON_WINDOW_MS = 2000;    // average rates over this window

function _mfmt(n) {
  n = n || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
}
function _lossPct(missing, received) {
  const tot = (missing || 0) + (received || 0);
  if (tot <= 0) return '0%';
  const p = (missing || 0) / tot * 100;
  return (p > 0 && p < 0.1 ? p.toFixed(3) : p.toFixed(1)) + '%';
}
function _monBadge(id, v) {
  const el = document.getElementById(id); if (!el) return;
  el.textContent = v ? String(v).toUpperCase() : '—';
  el.classList.toggle('tr-udp', v === 'udp');
  el.classList.toggle('tr-tcp', v === 'tcp');
  el.classList.toggle('tr-idle', !v);
}

function _setMonitor(on) {
  _monitorMode = on;
  monitorFloat.hidden = !on;
  document.getElementById('btn-monitor').classList.toggle('active', on);
  // Enabling pauses chart streaming (backend keeps receiving + logging);
  // disabling resumes it. Stream ▶/■ buttons reflect this via msg.streaming.
  fetch(on ? '/api/stream/stop' : '/api/stream/start', { method: 'POST' }).catch(() => {});
  if (on) _monHist = [];  // reset rate baseline
}

document.getElementById('btn-monitor').addEventListener('click', () => _setMonitor(!_monitorMode));
document.getElementById('btn-close-monitor').addEventListener('click', () => _setMonitor(false));
document.getElementById('btn-monitor-reset').addEventListener('click', async () => {
  await fetch('/api/stats/reset', { method: 'POST' }).catch(() => {});
  _monHist = [];
});

function _updateMonitor(msg) {
  if (monitorFloat.hidden || !msg.stats) return;
  const s = msg.stats;
  const raw = (s.streams && s.streams.raw) || {};
  const fft = (s.streams && s.streams.fft) || {};

  // Smoothed rates: average the counter deltas over a ~2 s window of snapshots,
  // so a low-rate stream (e.g. FFT) doesn't flicker to 0 between status updates.
  const now = performance.now();
  _monHist.push({ t: now, rawRecv: raw.received || 0, fftRecv: fft.received || 0,
                  samples: raw.samples || 0, dgrams: s.udp_datagrams || 0 });
  while (_monHist.length > 2 && now - _monHist[0].t > _MON_WINDOW_MS) _monHist.shift();

  let rawRate = '—', fftRate = '—', sps = '—', dps = '—';
  if (_monHist.length >= 2) {
    const a = _monHist[0], b = _monHist[_monHist.length - 1];
    const dt = (b.t - a.t) / 1000;
    if (dt > 0.3) {
      const rate = (k) => Math.max(0, b[k] - a[k]) / dt;
      rawRate = rate('rawRecv').toFixed(1) + '/s';
      fftRate = rate('fftRecv').toFixed(1) + '/s';
      sps     = _mfmt(Math.round(rate('samples'))) + '/s';
      dps     = rate('dgrams').toFixed(0) + '/s';
    }
  }

  const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  const warn = (id, on) => { const e = document.getElementById(id); if (e) e.classList.toggle('stat-warn', !!on); };

  set('mon-raw-recv', _mfmt(raw.received)); set('mon-raw-miss', _mfmt(raw.missing));
  set('mon-raw-loss', _lossPct(raw.missing, raw.received)); warn('mon-raw-miss', (raw.missing || 0) > 0);
  set('mon-raw-rate', rawRate); _monBadge('mon-raw-tr', raw.transport);
  set('mon-fft-recv', _mfmt(fft.received)); set('mon-fft-miss', _mfmt(fft.missing));
  set('mon-fft-loss', _lossPct(fft.missing, fft.received)); warn('mon-fft-miss', (fft.missing || 0) > 0);
  set('mon-fft-rate', fftRate); _monBadge('mon-fft-tr', fft.transport);

  set('mon-raw-samples', _mfmt(raw.samples)); set('mon-raw-sps', sps);

  set('mon-udp-dgrams', _mfmt(s.udp_datagrams)); set('mon-udp-dps', dps);
  set('mon-udp-pkts', _mfmt(s.udp_packets));
  set('mon-udp-lostp', _mfmt(s.udp_lost_packets)); warn('mon-udp-lostp', (s.udp_lost_packets || 0) > 0);
  set('mon-udp-lostc', _mfmt(s.udp_lost_chunks)); warn('mon-udp-lostc', (s.udp_lost_chunks || 0) > 0);
  set('mon-udp-loss', _lossPct(s.udp_lost_packets, s.udp_packets));

  set('mon-packets', _mfmt(s.packets)); set('mon-errors', _mfmt(s.errors)); warn('mon-errors', (s.errors || 0) > 0);
  const conn = document.getElementById('mon-conn');
  if (conn) { conn.textContent = msg.connected ? 'YES' : '—'; conn.classList.toggle('tr-udp', !!msg.connected); conn.classList.toggle('tr-idle', !msg.connected); }
  const lg = document.getElementById('mon-logging');
  if (lg) { lg.textContent = msg.logging ? 'ON' : 'off'; lg.classList.toggle('tr-udp', !!msg.logging); lg.classList.toggle('tr-idle', !msg.logging); }
}

// Make the monitor panel draggable by its titlebar (mirror the console panel).
(function () {
  const tb = monitorFloat.querySelector('.console-float-titlebar');
  let sx, sy, ol, ot;
  tb.addEventListener('mousedown', (e) => {
    if (e.target.closest('button')) return;
    e.preventDefault();
    const r = monitorFloat.getBoundingClientRect();
    monitorFloat.style.left = r.left + 'px'; monitorFloat.style.top = r.top + 'px';
    monitorFloat.style.bottom = 'auto'; monitorFloat.style.right = 'auto';
    sx = e.clientX; sy = e.clientY; ol = r.left; ot = r.top;
    const mv = (e) => { monitorFloat.style.left = (ol + e.clientX - sx) + 'px'; monitorFloat.style.top = (ot + e.clientY - sy) + 'px'; };
    const up = () => { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
    document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
  });
  tb.style.cursor = 'grab';
})();

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

// ── Message / warning dialog ──────────────────────────────────────────────────
// showDialog({ title, message, warning, actions:[{label,primary,onClick}], onDismiss })
// Each action closes the dialog then runs its onClick. Clicking the backdrop
// closes and runs onDismiss (used by ensurePassword to treat it as cancel).
function showDialog({ title = 'Notice', message = '', warning = true, actions, onDismiss } = {}) {
  const overlay = document.getElementById('msg-overlay');
  const bodyEl  = document.getElementById('msg-body');
  const actEl   = document.getElementById('msg-actions');
  document.getElementById('msg-title').textContent = title;
  bodyEl.textContent = message;
  bodyEl.classList.toggle('setup-warning', !!warning);
  const acts = (actions && actions.length) ? actions : [{ label: 'OK', primary: true }];
  const close = () => { overlay.hidden = true; overlay.onclick = null; };
  actEl.innerHTML = '';
  for (const a of acts) {
    const b = document.createElement('button');
    b.className = 'btn' + (a.primary ? ' btn-primary' : '');
    b.textContent = a.label;
    b.addEventListener('click', () => { close(); if (a.onClick) a.onClick(); });
    actEl.appendChild(b);
  }
  overlay.onclick = (e) => { if (e.target === overlay) { close(); if (onDismiss) onDismiss(); } };
  overlay.hidden = false;
}

// Pre-flight for sensor writes: if the password field is empty, warn (the sensor
// will reject the change if it's password-protected) but let password-less
// sensors continue. Resolves true to proceed, false to abort.
let _emptyPwAck = false;
function ensurePassword() {
  if (inPw.value || _emptyPwAck) return Promise.resolve(true);
  return new Promise((resolve) => {
    showDialog({
      title: 'Password field is empty',
      message: 'The Password field is empty. If this sensor is password-protected, the change will be rejected. Enter the password, or continue anyway (for sensors with no password).',
      actions: [
        { label: 'Enter password', primary: true, onClick: () => { inPw.focus(); resolve(false); } },
        { label: 'Continue anyway', onClick: () => { _emptyPwAck = true; resolve(true); } },
      ],
      onDismiss: () => resolve(false),
    });
  });
}

// Detect the sensor's HMAC rejection (wrong or missing password) in an API
// result and show a clear pop-up. Returns true if it was an auth error.
function checkAuthError(d) {
  if (!d || typeof d.detail !== 'string' || !/invalid_hmac/i.test(d.detail)) return false;
  showDialog({
    title: 'Wrong or missing password',
    message: 'The sensor rejected the command — authentication (HMAC) failed. Check the Password field for the selected sensor and try again.',
    actions: [{ label: 'OK', primary: true, onClick: () => inPw.focus() }],
  });
  return true;
}

document.getElementById('btn-get-info').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/info', sensorBody());
  if (checkAuthError(d)) return;
  // Format with units for readability
  const fmt = { ...d };
  if (fmt.temp1 != null)     fmt.temp1 = `${fmt.temp1.toFixed(1)} °C`;
  if (fmt.temp2 != null)     fmt.temp2 = `${fmt.temp2.toFixed(1)} °C`;
  if (fmt.temp_core != null) fmt.temp_core = `${fmt.temp_core.toFixed(1)} °C`;
  if (fmt.cpu_usage != null) fmt.cpu_usage = `${fmt.cpu_usage.toFixed(1)} %`;
  outInfo.textContent = JSON.stringify(fmt, null, 2);
  // Persistent status readout in the Selected Sensor panel.
  const cpuEl = document.getElementById('info-cpu');
  const dbgEl = document.getElementById('info-debug');
  if (cpuEl) cpuEl.textContent = (d.cpu_usage != null) ? `${d.cpu_usage.toFixed(1)} %` : '—';
  if (dbgEl) dbgEl.textContent = (d.debug_str != null && d.debug_str !== '') ? d.debug_str : '—';
  // Firmware version, and the FFT magnitude scale it implies. 0x1033 changed
  // the numeric value of every FFT bin with no wire-format change, so nothing
  // in the stream itself reveals which scale a sensor is on — this Get Info is
  // what tells the backend, and it re-normalises older sensors from here on.
  const fwEl = document.getElementById('info-fw');
  if (fwEl) fwEl.textContent = (d.firmware_version != null)
    ? `0x${d.firmware_version.toString(16)}` : '—';
  const scEl = document.getElementById('info-fft-scale');
  const scWrap = document.getElementById('info-fft-scale-wrap');
  if (scEl && scWrap) {
    if (d.fft_scaling === 'legacy_compensated') {
      scEl.textContent = 'legacy — compensated';
      scEl.className = 'ntp-status warn';
      scWrap.title = 'This sensor predates firmware 0x1033, whose FFT magnitudes were '
        + 'not normalised (Q15 8× low, float32 fft_size/4 too high, DC not halved). '
        + 'The viewer re-normalises its bins onto the 0x1033 amplitude scale, so a tone '
        + 'of amplitude A reads A. Spectra recorded before this Get Info are on the raw scale.';
      scWrap.style.display = '';
    } else if (d.fft_scaling === 'normalised') {
      scEl.textContent = 'normalised';
      scEl.className = 'ntp-status ok';
      scWrap.title = 'FFT magnitudes are an amplitude spectrum straight from the sensor '
        + '(firmware 0x1033+): a tone of amplitude A reads A, at any fft_size and in '
        + 'either precision.';
      scWrap.style.display = '';
    } else {
      scWrap.style.display = 'none';
    }
  }
});

// ── DC removal (software high-pass, firmware 0x1032+) ────────────────────────
// Enable and cutoff share a single wire enum, so choosing Off forgets the
// cutoff. Remember the last non-Off choice per sensor and offer it back.
const dcSel  = document.getElementById('cfg-dc-removal');
const dcNote = document.getElementById('cfg-dc-note');
const dcHint = document.getElementById('cfg-dc-hint');

function _dcCacheKey() {
  return 'dcRemoval:' + ((selectedSensor && selectedSensor.mac) || '').toLowerCase();
}
function _dcRemember(v) {
  if (v && v !== 'DC_REMOVAL_OFF') {
    try { localStorage.setItem(_dcCacheKey(), v); } catch {}
  }
}
function _dcRecall() {
  try { return localStorage.getItem(_dcCacheKey()); } catch { return null; }
}
function _dcLabel(v) {
  const opt = dcSel.querySelector(`option[value="${v}"]`);
  return opt ? opt.textContent : v;
}

function _syncDcRemovalUi() {
  const v    = dcSel.value;
  const on   = v && v !== 'DC_REMOVAL_OFF';
  const filt = document.getElementById('cfg-filt').value;
  const axes = document.getElementById('cfg-axes').value;

  // The sensor forces DC removal off (keeping the stored value) while the
  // hardware high-pass or slope filter is selected, where it would only add a
  // settling transient.
  const hwHp = (filt === 'FILTER_HIGH_PASS' || filt === 'FILTER_SLOPE_FILTER');
  if (on && hwHp) {
    dcNote.textContent = 'bypassed — hardware high-pass active';
    dcNote.className   = 'ntp-status warn';
  } else if (on) {
    dcNote.textContent = 'on';
    dcNote.className   = 'ntp-status ok';
  } else {
    dcNote.textContent = '';
    dcNote.className   = 'ntp-status';
  }

  const notes = [];
  if (v === 'DC_REMOVAL_OFF') {
    const last = _dcRecall();
    if (last && last !== 'DC_REMOVAL_OFF') {
      notes.push(`Last used ${_dcLabel(last)} — <a href="#" id="cfg-dc-restore">restore</a>.`);
    }
  }
  if (v === 'DC_REMOVAL_0p1_HZ') {
    notes.push('0.1 Hz needs ~7 s to settle after Apply; earlier samples still carry the DC ramp.');
  }
  if (on) {
    notes.push('FFT bin 0 collapses to ~0 while DC removal is on (it reports |DC| when off).');
    if (axes && axes.endsWith('_VECTOR')) {
      notes.push('Vector axis modes filter per axis <em>before</em> the magnitude, giving a rectified '
               + 'AC magnitude: single-axis vibration then appears at double frequency. Not recommended '
               + 'with the FFT stream.');
    }
  }
  dcHint.innerHTML = notes.join(' ');
  const restore = document.getElementById('cfg-dc-restore');
  if (restore) {
    restore.addEventListener('click', (e) => {
      e.preventDefault();
      dcSel.value = _dcRecall();
      _syncDcRemovalUi();
    });
  }
}
dcSel.addEventListener('change', () => { _dcRemember(dcSel.value); _syncDcRemovalUi(); });
document.getElementById('cfg-filt').addEventListener('change', _syncDcRemovalUi);
document.getElementById('cfg-axes').addEventListener('change', _syncDcRemovalUi);

document.getElementById('btn-get-cfg').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/config', sensorBody());
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
  if (!d.detail) {
    document.getElementById('cfg-fs').value     = d.full_scale  || '';
    document.getElementById('cfg-axes').value   = d.axes        || '';
    document.getElementById('cfg-odr').value    = d.odr_div     || '';
    const f = d.filter || {};
    document.getElementById('cfg-filt').value   = f.filter_enabled || '';
    document.getElementById('cfg-cutoff').value = f.filter_cutoff  || '';
    if (d.fft_size) document.getElementById('cfg-fft-size').value = d.fft_size;
    if (d.fft_precision) document.getElementById('cfg-fft-precision').value = d.fft_precision;
    // Firmware ≤ 0x1031 has no dc_removal field; the backend reports OFF for it.
    dcSel.value = d.dc_removal || 'DC_REMOVAL_OFF';
    _dcRemember(dcSel.value);
    _syncDcRemovalUi();
  }
});

document.getElementById('btn-set-cfg').addEventListener('click', async () => {
  if (!(await ensurePassword())) return;
  const d = await apiPost('/api/sensor/config/set', sensorBody({
    full_scale:     document.getElementById('cfg-fs').value    || null,
    axes:           document.getElementById('cfg-axes').value  || null,
    odr_div:        document.getElementById('cfg-odr').value   || null,
    filter_enabled: document.getElementById('cfg-filt').value  || null,
    filter_cutoff:  document.getElementById('cfg-cutoff').value || null,
    fft_size:       document.getElementById('cfg-fft-size').value || null,
    fft_precision:  document.getElementById('cfg-fft-precision').value || null,
    dc_removal:     dcSel.value || null,
  }));
  if (checkAuthError(d)) return;
  _dcRemember(dcSel.value);
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-get-net').addEventListener('click', async () => {
  const d = await apiPost('/api/network/config', sensorBody());
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
  if (!d.detail) {
    document.getElementById('net-ip').value          = d.ip           || '';
    document.getElementById('net-mask').value         = d.netmask      || '';
    document.getElementById('net-gw').value           = d.gateway      || '';
    document.getElementById('net-server-ip').value    = d.server_ip    || '';
    document.getElementById('net-server-port').value  = d.server_port  ?? '';
    document.getElementById('net-ntp-ip').value       = d.ntp_server_ip || '';
    document.getElementById('net-ntp-interval').value = d.ntp_interval_s ?? '';
    document.getElementById('net-ntp-offset').value   = d.ntp_offset_us ?? '';
    document.getElementById('net-ntp-min-error').value = d.ntp_min_ms_error_to_update ?? '';
    // UNDEFINED (fresh factory unit) is treated as POLL in the UI.
    document.getElementById('net-ntp-mode').value = (d.ntp_mode && d.ntp_mode !== 'NTP_MODE_UNDEFINED') ? d.ntp_mode : 'NTP_MODE_POLL';
    _syncNtpModeUi();
    document.getElementById('net-dhcp').value         = d.dhcp         || 'FEATURE_DISABLED';
    document.getElementById('net-stream').value       = d.data_stream  || 'FEATURE_DISABLED';
    if (d.fft_stream) document.getElementById('net-fft-stream').value = d.fft_stream;
    document.getElementById('net-sample-transport').value = d.sample_transport || 'FEATURE_DISABLED';
    document.getElementById('net-fft-transport').value    = d.fft_transport    || 'FEATURE_DISABLED';
  }
});

document.getElementById('btn-set-net').addEventListener('click', async () => {
  if (!(await ensurePassword())) return;
  const port = document.getElementById('net-server-port').value;
  const d = await apiPost('/api/network/config/set', sensorBody({
    ip:          document.getElementById('net-ip').value          || null,
    netmask:     document.getElementById('net-mask').value        || null,
    gateway:     document.getElementById('net-gw').value          || null,
    server_ip:   document.getElementById('net-server-ip').value   || null,
    server_port: port ? Number(port) : null,
    ntp_server_ip: document.getElementById('net-ntp-ip').value    || null,
    ntp_interval_s: document.getElementById('net-ntp-interval').value ? Number(document.getElementById('net-ntp-interval').value) : null,
    ntp_offset_us:  document.getElementById('net-ntp-offset').value ? Number(document.getElementById('net-ntp-offset').value) : null,
    ntp_min_ms_error_to_update: document.getElementById('net-ntp-min-error').value ? Number(document.getElementById('net-ntp-min-error').value) : null,
    ntp_mode:    document.getElementById('net-ntp-mode').value    || null,
    dhcp:        document.getElementById('net-dhcp').value        || null,
    data_stream: document.getElementById('net-stream').value      || null,
    fft_stream:  document.getElementById('net-fft-stream').value  || null,
    sample_transport: document.getElementById('net-sample-transport').value || null,
    fft_transport:    document.getElementById('net-fft-transport').value    || null,
  }));
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
});

// Changing a transport only takes effect after a sensor reboot — remind the operator.
function _transportRestartNotice() {
  showDialog({
    title: 'Sensor restart required',
    message: 'Transport (TCP/UDP) changes take effect only after a sensor restart. Click Apply to save the setting, then power-cycle the sensor (or use Restart Sensor) for it to take effect.',
  });
}
document.getElementById('net-sample-transport').addEventListener('change', _transportRestartNotice);
document.getElementById('net-fft-transport').addEventListener('change', _transportRestartNotice);

// Latest FLAG_NO_TIME_SYNC state from the status broadcast: null = unknown (no
// frame yet), true = clock disciplined, false = not synced. Declared before
// _syncNtpModeUi (which runs at load and calls _renderTimeSync) to avoid a TDZ.
let _lastTimeSync = null;

// The NTP server IP (and poll-related fields) are irrelevant in Listen/Disabled
// mode — grey them out. They're still written for future-proofing.
function _syncNtpModeUi() {
  const mode = document.getElementById('net-ntp-mode').value;
  const pollOnly = (mode === 'NTP_MODE_POLL');
  for (const id of ['net-ntp-ip', 'btn-test-ntp', 'net-ntp-interval', 'net-ntp-min-error']) {
    const el = document.getElementById(id);
    if (el) { el.disabled = !pollOnly; el.style.opacity = pollOnly ? '' : '0.5'; }
  }
  // NTP mode gates the "disabled vs waiting" wording of the time-sync indicator.
  _renderTimeSync();
}
document.getElementById('net-ntp-mode').addEventListener('change', _syncNtpModeUi);
_syncNtpModeUi();

// ── Sensor time-sync indicator ───────────────────────────────────────────────
// Combines _lastTimeSync (above) with the current NTP mode to distinguish
// "waiting for sync" from "disabled by config".
function _renderTimeSync() {
  const mode = (document.getElementById('net-ntp-mode') || {}).value || '';
  let dotCls, label, badgeCls, badgeText, statusCls, statusText;
  if (_lastTimeSync === null) {
    dotCls = 'dot';        label = 'Time sync —';
    badgeCls = 'tr-badge tr-idle'; badgeText = '—';
    statusCls = 'ntp-status';      statusText = '';
  } else if (_lastTimeSync === true) {
    dotCls = 'dot green';  label = 'Time synced';
    badgeCls = 'tr-badge tr-udp';  badgeText = 'OK';
    statusCls = 'ntp-status ok';   statusText = 'Synced';
  } else if (mode === 'NTP_MODE_DISABLED') {
    dotCls = 'dot';        label = 'Time sync off';
    badgeCls = 'tr-badge tr-idle'; badgeText = 'OFF';
    statusCls = 'ntp-status';      statusText = 'Off (NTP disabled)';
  } else {
    dotCls = 'dot orange'; label = 'Waiting for time sync';
    badgeCls = 'tr-badge tr-warn'; badgeText = 'WAIT';
    statusCls = 'ntp-status warn'; statusText = 'Waiting for time sync…';
  }
  if (dotTsync) dotTsync.className = dotCls;
  if (lblTsync) lblTsync.textContent = label;
  const st = document.getElementById('tsync-status');
  if (st) { st.className = statusCls; st.textContent = statusText; }
  const mt = document.getElementById('mon-tsync');
  if (mt) { mt.className = badgeCls; mt.textContent = badgeText; }
}

// ── NTP check ────────────────────────────────────────────────────────────────
async function _checkNtp(ip, statusEl) {
  if (!ip) { statusEl.textContent = ''; statusEl.className = 'ntp-status'; return; }
  statusEl.textContent = 'checking…';
  statusEl.className = 'ntp-status checking';
  try {
    const r = await fetch('/api/ntp/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip }),
    });
    const d = await r.json();
    if (d.reachable) {
      statusEl.textContent = `OK (stratum ${d.stratum}, ${d.offset_ms > 0 ? '+' : ''}${d.offset_ms} ms)`;
      statusEl.className = 'ntp-status ok';
    } else {
      statusEl.textContent = `Not reachable — ${d.error || 'check NTP setup'}`;
      statusEl.className = 'ntp-status fail';
    }
  } catch {
    statusEl.textContent = 'Check failed';
    statusEl.className = 'ntp-status fail';
  }
}

document.getElementById('btn-test-ntp').addEventListener('click', () => {
  const ip = document.getElementById('net-ntp-ip').value.trim();
  _checkNtp(ip, document.getElementById('ntp-status'));
});

document.getElementById('btn-stream-start').addEventListener('click', async () => {
  // Clear burst review state so live FFT updates resume
  if (_burstFFT) {
    _burstFFT = null;
    _showBurstScrubbers();
  }
  const r = await fetch('/api/stream/start', { method: 'POST' });
  const d = await r.json();
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-stream-stop').addEventListener('click', async () => {
  const r = await fetch('/api/stream/stop', { method: 'POST' });
  const d = await r.json();
  outInfo.textContent = JSON.stringify(d, null, 2);
});

// Set the burst button's look/state. `capturing` → acts as a Cancel button.
function _setBurstButton(capturing) {
  _capturing = capturing;
  const btn = document.getElementById('btn-burst');
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = capturing ? 'Cancel Capture' : 'Burst Capture';
  btn.classList.toggle('btn-danger', capturing);
  btn.classList.toggle('btn-primary', !capturing);
}

document.getElementById('btn-burst').addEventListener('click', async () => {
  if (_capturing) {
    // Cancel an in-progress capture.
    _setBurstButton(false);
    try { await fetch('/api/burst/cancel', { method: 'POST' }); } catch (e) {}
    outInfo.textContent = 'Burst capture cancelled.';
    return;
  }
  const dur = Number(document.getElementById('sel-burst-duration').value);
  _setBurstButton(true);   // becomes "Cancel Capture"; backend timeout/status drive the reset
  try {
    const r = await fetch('/api/burst', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ duration: dur }),
    });
    const d = await r.json();
    outInfo.textContent = JSON.stringify(d, null, 2);
  } catch (e) {
    _setBurstButton(false);
    outInfo.textContent = 'Burst request failed: ' + e;
  }
});

// ── FFT stream buttons ──────────────────────────────────────────────────────
document.getElementById('btn-fft-start').addEventListener('click', async () => {
  if (!(await ensurePassword())) return;
  const d = await apiPost('/api/stream/fft/start', sensorBody());
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-fft-stop').addEventListener('click', async () => {
  if (!(await ensurePassword())) return;
  const d = await apiPost('/api/stream/fft/stop', sensorBody());
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-reset').addEventListener('click', async () => {
  if (!confirm('Reset the sensor?')) return;
  if (!(await ensurePassword())) return;
  const d = await apiPost('/api/sensor/reset', sensorBody());
  if (checkAuthError(d)) return;
  outInfo.textContent = JSON.stringify(d, null, 2);
});

// ── device discovery ─────────────────────────────────────────────────────────
async function refreshDevices() {
  try {
    const r = await fetch('/api/devices');
    const devices = await r.json();
    renderDeviceList(devices);
    // Auto-select if only one sensor on the network
    if (devices.length === 1 && !selectedSensor) {
      selectSensor(devices[0]);
    }
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
    const fwLabel = d.fw_app ? `FW ${d.fw_app}` : (d.fw_bl ? `BL ${d.fw_bl}` : '');
    el.innerHTML = `
      <div class="device-row1">
        <span class="dot ${modeClass === 'app' ? 'green' : modeClass === 'boot' ? 'orange' : ''}" title="${modeClass === 'app' ? 'Application mode — sensor is running' : modeClass === 'boot' ? 'Bootloader mode — sensor is waiting to start' : 'Unknown mode'}"></span>
        <span class="device-mac">${d.mac}</span>
        ${modeBadge}
        <button class="btn btn-setup" data-ip="${d.ip}" data-mac="${d.mac}" title="Configure server address, NTP, and IP settings on this sensor">Setup</button>
      </div>
      <div class="device-row2">
        <span class="device-ip">${d.ip}</span>
        <span class="device-fw">${fwLabel}</span>
      </div>
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
  const changed = !selectedSensor || selectedSensor.mac !== d.mac;
  selectedSensor = { ip: d.ip, mac: d.mac };
  selectedInfo.textContent = `${d.mac}  (${d.ip})`;
  deviceListEl.querySelectorAll('.device-item').forEach(el => {
    el.classList.toggle('selected', el.querySelector('.device-mac')?.textContent === d.mac);
  });
  // The status row holds the last Get Info result, which belongs to whichever
  // sensor was selected then — clear it rather than attribute one sensor's CPU,
  // firmware or FFT scale to another.
  if (changed) {
    ['info-cpu', 'info-debug', 'info-fw'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = '—';
    });
    const scWrap = document.getElementById('info-fft-scale-wrap');
    if (scWrap) scWrap.style.display = 'none';
  }
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
  // Auto-check NTP availability
  _checkNtp(host.ip, document.getElementById('setup-ntp-status'));
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
inPw.addEventListener('input', _updateRescueCmd);

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
    // No empty-password pre-flight here — fresh sensors in setup are often
    // password-less — but still surface a clear message if it's rejected.
    if (checkAuthError(res)) { _setupPending = null; return; }
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
        win.plot._log = !!win.logScale;
      } else if (info.group === 'timesync') {
        win.canvas.innerHTML = '';
        win.plot = _createTimeSyncPlot(win.canvas);
      } else {
        // Pass `win` so the Y-scale range closure binds to this window
        // (otherwise fixed Y-scale silently reverts to auto after a theme change).
        win.plot = _createPlot(win.type, win.canvas, info, win);
      }
      _refreshWindow(win);
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

// ── Per-window settings modal (Y-scale + window length) ───────────────────────
const wsOverlay   = document.getElementById('winsettings-overlay');
const wsTitle     = document.getElementById('ws-title');
const wsYAuto     = document.getElementById('ws-y-auto');
const wsYFixed    = document.getElementById('ws-y-fixed');
const wsYMin      = document.getElementById('ws-y-min');
const wsYMax      = document.getElementById('ws-y-max');
const wsFixedRow  = document.getElementById('ws-fixed-row');
const wsYHint     = document.getElementById('ws-y-hint');
const wsYscaleGrp = document.getElementById('ws-yscale-group');
const wsLogGroup  = document.getElementById('ws-log-group');
const wsLog       = document.getElementById('ws-log');
const wsWinlenGrp = document.getElementById('ws-winlen-group');
const wsWinlen    = document.getElementById('ws-winlen');
let _wsWinId = null;

function _wsSyncFixedEnabled() {
  const fixed = wsYFixed.checked;
  wsFixedRow.style.opacity = fixed ? '1' : '0.45';
  wsYMin.disabled = wsYMax.disabled = !fixed;
}

function openWindowSettings(id) {
  const win = _chartWindows[id];
  if (!win) return;
  _wsWinId = id;
  const info = WINDOW_TYPES[win.type] || {};
  wsTitle.textContent = info.label || win.type;

  const isRaw = win.type === 'raw';
  const isSpectro = info.group === 'spectrogram';
  const hasValueAxis = isRaw || info.group === 'fft' || info.group === 'psd';  // line charts
  const hasLog = info.group === 'fft' || info.group === 'psd' || isSpectro;

  // Y-axis scale (line charts only — a spectrogram's Y is frequency, not a value)
  wsYscaleGrp.hidden = !hasValueAxis;
  const ys = win.yScale || { mode: 'auto' };
  wsYAuto.checked  = ys.mode !== 'fixed';
  wsYFixed.checked = ys.mode === 'fixed';
  wsYMin.value = (ys.min != null) ? ys.min : '';
  wsYMax.value = (ys.max != null) ? ys.max : '';
  const unit = win.logScale ? 'log — dB-scale units' :
               (info.group === 'psd' ? '(m/s²)²/Hz' : 'm/s²');
  wsYHint.textContent = `Fixed range is in the values currently plotted (${unit}).`;
  _wsSyncFixedEnabled();

  // Log scale (FFT/PSD/spectrogram)
  wsLogGroup.hidden = !hasLog;
  wsLog.checked = !!win.logScale;

  // Window length only applies to raw (time-domain) windows.
  wsWinlenGrp.hidden = !isRaw;
  if (isRaw) wsWinlen.value = String(win.windowSec || _defaultWindowSec);

  wsOverlay.hidden = false;
}

wsYAuto.addEventListener('change', _wsSyncFixedEnabled);
wsYFixed.addEventListener('change', _wsSyncFixedEnabled);

document.getElementById('ws-y-fit').addEventListener('click', () => {
  const win = _chartWindows[_wsWinId];
  if (!win) return;
  const r = _currentRangeForWindow(win);
  if (!r) { wsYHint.textContent = 'No data available yet to fit.'; return; }
  // A little headroom above the peak; clamp the floor at 0 for non-negative data.
  const [lo, hi] = r;
  wsYFixed.checked = true; _wsSyncFixedEnabled();
  wsYMin.value = (lo >= 0) ? 0 : +(lo * 1.1).toPrecision(4);
  wsYMax.value = +(hi * 1.1).toPrecision(4);
});

document.getElementById('ws-apply').addEventListener('click', () => {
  const win = _chartWindows[_wsWinId];
  if (!win) { wsOverlay.hidden = true; return; }

  if (!wsYscaleGrp.hidden) {
    if (wsYFixed.checked) {
      const mn = wsYMin.value === '' ? null : Number(wsYMin.value);
      const mx = wsYMax.value === '' ? null : Number(wsYMax.value);
      if (mx == null || !isFinite(mx)) { wsYHint.textContent = 'Enter a Max value for a fixed scale.'; return; }
      win.yScale = { mode: 'fixed', min: (mn != null && isFinite(mn)) ? mn : null, max: mx };
    } else {
      win.yScale = { mode: 'auto', min: null, max: null };
    }
  }

  if (!wsLogGroup.hidden) {
    win.logScale = wsLog.checked;
  }

  if (win.type === 'raw') {
    win.windowSec = Number(wsWinlen.value) || _defaultWindowSec;
  }

  // Re-render immediately. Both helpers are safe for any window type:
  //  _rerenderSpectralWindow → re-applies the log transform for fft/psd and the
  //    color-scale for spectrogram (no-op for raw).
  //  _refreshWindow → re-slices raw to its window length and re-runs the Y-scale
  //    range fn on the current data (no-op for spectrogram, which has no .data).
  _rerenderSpectralWindow(win);
  _refreshWindow(win);
  _saveWindowState();
  wsOverlay.hidden = true;
});

document.getElementById('ws-close').addEventListener('click', () => { wsOverlay.hidden = true; });
wsOverlay.addEventListener('click', (e) => { if (e.target === wsOverlay) wsOverlay.hidden = true; });

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
      createChartWindow(s.type, { x: s.x, y: s.y, w: s.w, h: s.h, logScale: s.logScale,
                                  yScale: s.yScale, windowSec: s.windowSec });
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
