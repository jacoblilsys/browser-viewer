/**
 * Browser Sensor Viewer — frontend logic
 *
 * WebSocket receives 60 Hz JSON envelopes from the backend:
 *   { type:'frame', t_end_ns, rate_hz, axes:{ accel_x:{min,max,last,n}, ... } }
 *   { type:'status', connected, logging, device_id }
 *
 * uPlot renders 3 series (X/Y/Z) with optional min/max envelope bands.
 * A sliding time window (seconds) is maintained on the client.
 */

'use strict';

// ── config ────────────────────────────────────────────────────────────────────
const WS_URL      = `ws://${location.host}/ws`;
const MAX_PTS     = 6000;   // max points per series in ring buffer
const RECONNECT_MS = 2000;

// ── state ─────────────────────────────────────────────────────────────────────
let plot        = null;
let ws          = null;
let wsAlive     = false;
let _streamUid  = null;

// Ring buffers: timestamps (seconds float) + per-axis {min[], max[], last[]}
const ring = {
  t:     [],
  x_min: [], x_max: [], x_last: [],
  y_min: [], y_max: [], y_last: [],
  z_min: [], z_max: [], z_last: [],
};

let windowSec = 5;
let showEnvelope = true;

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
const chkEnv    = document.getElementById('chk-envelope');
const chartDiv  = document.getElementById('chart');
const outInfo   = document.getElementById('out-info');

// Phase-2 control elements
const inPw         = document.getElementById('in-pw');
const deviceListEl = document.getElementById('device-list');
const selectedInfo = document.getElementById('selected-info');

// ── device discovery state ───────────────────────────────────────────────────
let selectedSensor = null;   // { ip, mac }
let _refreshTimer  = null;

const hostInfo   = document.getElementById('host-info');
const btnTheme   = document.getElementById('btn-theme');
const themeIcon  = document.getElementById('theme-icon');

// ── uPlot setup ───────────────────────────────────────────────────────────────
function makeOpts(w, h) {
  const e = showEnvelope;
  const alpha = (hex) => hex + '55';  // semi-transparent fill
  return {
    width:  w,
    height: h,
    pxAlign: false,
    cursor: { show: false },
    legend: { show: false },
    axes: [
      { stroke: '#666', ticks: { stroke: '#333' }, grid: { stroke: '#333' } },
      { stroke: '#aaa', ticks: { stroke: '#333' }, grid: { stroke: '#2a3a5a' } },
    ],
    series: [
      {},                                           // [0] time (x-axis)
      // X envelope min
      { stroke: 'transparent', fill: alpha('#e05252'), paths: uPlot.paths.linear(), show: e },
      // X envelope max
      { stroke: 'transparent', fill: alpha('#e05252'), paths: uPlot.paths.linear(), show: e },
      // X last
      { stroke: '#e05252', width: 1.5, paths: uPlot.paths.linear() },
      // Y envelope min
      { stroke: 'transparent', fill: alpha('#52c45a'), paths: uPlot.paths.linear(), show: e },
      // Y envelope max
      { stroke: 'transparent', fill: alpha('#52c45a'), paths: uPlot.paths.linear(), show: e },
      // Y last
      { stroke: '#52c45a', width: 1.5, paths: uPlot.paths.linear() },
      // Z envelope min
      { stroke: 'transparent', fill: alpha('#5296e0'), paths: uPlot.paths.linear(), show: e },
      // Z envelope max
      { stroke: 'transparent', fill: alpha('#5296e0'), paths: uPlot.paths.linear(), show: e },
      // Z last
      { stroke: '#5296e0', width: 1.5, paths: uPlot.paths.linear() },
    ],
    scales: {
      x: { time: false },
      y: { auto: true },
    },
  };
}

function initPlot() {
  if (plot) plot.destroy();
  const w = chartDiv.clientWidth  || 800;
  const h = chartDiv.clientHeight || 400;
  plot = new uPlot(makeOpts(w, h), emptyData(), chartDiv);
}

function emptyData() {
  return [[], [], [], [], [], [], [], [], [], []];
}

function buildPlotData() {
  const t  = ring.t;
  const now = t.length ? t[t.length - 1] : 0;
  const cut = now - windowSec;
  const i0  = t.findIndex(v => v >= cut);
  const sl  = (arr) => arr.slice(i0 < 0 ? 0 : i0);

  return [
    sl(ring.t),
    sl(ring.x_min), sl(ring.x_max), sl(ring.x_last),
    sl(ring.y_min), sl(ring.y_max), sl(ring.y_last),
    sl(ring.z_min), sl(ring.z_max), sl(ring.z_last),
  ];
}

// Resize observer
new ResizeObserver(() => {
  if (!plot) return;
  plot.setSize({ width: chartDiv.clientWidth, height: chartDiv.clientHeight });
}).observe(chartDiv);

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
    lblWs.textContent = 'Reconnecting…';
    setTimeout(connect, RECONNECT_MS);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === 'frame')  handleFrame(msg);
    if (msg.type === 'status') handleStatus(msg);
  };
}

function handleFrame(msg) {
  const axes = msg.axes || {};

  // Stream restart (ODR/FSR change) — clear ring buffers so stale data
  // from the old stream doesn't bleed into the new one.
  if (msg.stream_uid !== undefined && msg.stream_uid !== _streamUid) {
    _streamUid = msg.stream_uid;
    for (const k of Object.keys(ring)) ring[k] = [];
  }

  // Skip ticks where the backend emitted nothing (gap during reconnect).
  // Avoids pushing 0-fallbacks that produce triangular artifacts.
  if (Object.keys(axes).length === 0) return;

  const tSec = msg.t_end_ns / 1e9;

  const get = (label, key) => {
    const a = axes[label];
    return (a && key in a) ? a[key] : null;
  };

  // Only push a time point if at least one axis has real data.
  const xMin = get('accel_x', 'min'), xMax = get('accel_x', 'max'), xLast = get('accel_x', 'last');
  const yMin = get('accel_y', 'min'), yMax = get('accel_y', 'max'), yLast = get('accel_y', 'last');
  const zMin = get('accel_z', 'min'), zMax = get('accel_z', 'max'), zLast = get('accel_z', 'last');

  if (xLast === null && yLast === null && zLast === null) return;

  ring.t.push(tSec);
  // uPlot accepts null for missing points (rendered as gaps, not zeros)
  ring.x_min.push(xMin);  ring.x_max.push(xMax);  ring.x_last.push(xLast);
  ring.y_min.push(yMin);  ring.y_max.push(yMax);  ring.y_last.push(yLast);
  ring.z_min.push(zMin);  ring.z_max.push(zMax);  ring.z_last.push(zLast);

  // Trim old samples
  while (ring.t.length > MAX_PTS) {
    for (const k of Object.keys(ring)) ring[k].shift();
  }

  // Update rate label
  if (msg.rate_hz) lblRate.textContent = `${(msg.rate_hz / 1000).toFixed(1)} kHz`;

  // Update Y axis unit label from MetaData
  if (msg.units) {
    const unit = msg.units['accel_x'] || msg.units['accel_y'] || msg.units['accel_z']
              || msg.units['force_x'] || msg.units[Object.keys(msg.units)[0]] || '';
    if (unit && plot) {
      plot.axes[1].label = unit;
      plot.redraw();
    }
  }

  // Redraw
  if (plot) plot.setData(buildPlotData());
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
    // revert dropdown
    const cur = await (await fetch('/api/logging/format')).json();
    selFmt.value = cur.format;
  }
});

// ── clear button ──────────────────────────────────────────────────────────────
btnClear.addEventListener('click', () => {
  for (const k of Object.keys(ring)) ring[k] = [];
  if (plot) plot.setData(emptyData());
});

// ── window selector ───────────────────────────────────────────────────────────
selWindow.addEventListener('change', () => {
  windowSec = Number(selWindow.value);
});

// ── envelope toggle ───────────────────────────────────────────────────────────
chkEnv.addEventListener('change', () => {
  showEnvelope = chkEnv.checked;
  initPlot();  // rebuild with updated series visibility
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
  // Populate fields
  if (!d.detail) {
    document.getElementById('cfg-fs').value     = d.full_scale  || '';
    document.getElementById('cfg-axes').value   = d.axes        || '';
    document.getElementById('cfg-odr').value    = d.odr_div     || '';
    const f = d.filter || {};
    document.getElementById('cfg-filt').value   = f.filter_enabled || '';
    document.getElementById('cfg-cutoff').value = f.filter_cutoff  || '';
  }
});

document.getElementById('btn-set-cfg').addEventListener('click', async () => {
  const d = await apiPost('/api/sensor/config/set', sensorBody({
    full_scale:     document.getElementById('cfg-fs').value    || null,
    axes:           document.getElementById('cfg-axes').value  || null,
    odr_div:        document.getElementById('cfg-odr').value   || null,
    filter_enabled: document.getElementById('cfg-filt').value  || null,
    filter_cutoff:  document.getElementById('cfg-cutoff').value || null,
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
  }));
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-stream-start').addEventListener('click', async () => {
  const d = await apiPost('/api/stream/start', sensorBody());
  outInfo.textContent = JSON.stringify(d, null, 2);
});

document.getElementById('btn-stream-stop').addEventListener('click', async () => {
  const d = await apiPost('/api/stream/stop', sensorBody());
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
    el.innerHTML = `
      <span class="dot ${modeClass === 'app' ? 'green' : modeClass === 'boot' ? 'orange' : ''}"></span>
      <span class="device-mac">${d.mac}</span>
      <span class="device-ip">${d.ip}</span>
      <button class="btn btn-setup" data-ip="${d.ip}" data-mac="${d.mac}" title="Set server address to this machine">Setup</button>
    `;

    // Click row to select
    el.addEventListener('click', (e) => {
      if (e.target.classList.contains('btn-setup')) return; // handled separately
      selectSensor(d);
    });

    // Setup button — point sensor at this server
    el.querySelector('.btn-setup').addEventListener('click', async (e) => {
      e.stopPropagation();
      selectSensor(d);
      try {
        const host = await (await fetch('/api/host')).json();
        const body = sensorBody({
          server_ip:   host.ip,
          server_port: host.tcp_port,
          data_stream: 'FEATURE_ENABLED',
        });
        const res = await apiPost('/api/network/config/set', body);
        outInfo.textContent = 'Setup sent: ' + JSON.stringify(res, null, 2);
      } catch (err) {
        if (err.message !== 'no sensor selected')
          outInfo.textContent = 'Setup error: ' + err.message;
      }
    });

    deviceListEl.appendChild(el);
  }
}

function selectSensor(d) {
  selectedSensor = { ip: d.ip, mac: d.mac };
  selectedInfo.textContent = `${d.mac}  (${d.ip})`;
  // Highlight selected row
  deviceListEl.querySelectorAll('.device-item').forEach(el => {
    el.classList.toggle('selected', el.querySelector('.device-mac')?.textContent === d.mac);
  });
}

document.getElementById('btn-refresh-devices').addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  refreshDevices();
});

// Auto-refresh every 10s
_refreshTimer = setInterval(refreshDevices, 10000);

// ── theme toggle ─────────────────────────────────────────────────────────────
function applyTheme(isLight) {
  document.body.classList.toggle('light', isLight);
  themeIcon.innerHTML = isLight ? '&#9790;' : '&#9788;';  // moon / sun
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  initPlot();
}
btnTheme.addEventListener('click', () => {
  applyTheme(!document.body.classList.contains('light'));
});
// Restore saved theme
if (localStorage.getItem('theme') === 'light') {
  applyTheme(true);
}

// ── host info in topbar ──────────────────────────────────────────────────────
async function updateHostInfo() {
  try {
    const host = await (await fetch('/api/host')).json();
    hostInfo.textContent = `This host (Server): ${host.ip}:${host.tcp_port}`;
  } catch {
    hostInfo.textContent = 'This host (Server): unavailable';
  }
}

// ── init ──────────────────────────────────────────────────────────────────────
initPlot();
connect();
refreshDevices();
updateHostInfo();

// Fetch initial log format
fetch('/api/logging/format').then(r => r.json()).then(d => {
  selFmt.value = d.format;
}).catch(() => {});
