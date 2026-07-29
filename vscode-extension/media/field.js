// Magpylib-rendered 2D field plot (show(output=...)): field at the
// scene's sensors along their paths. Opened on demand from the Studio.
const vscodeApi = acquireVsCodeApi();
const statusEl = document.getElementById('status');
const canvasEl = document.getElementById('canvas');
const outputEl = document.getElementById('output');
const animateEl = document.getElementById('animate');
const modeEl = document.getElementById('mode');
const planeEl = document.getElementById('plane');
const offsetEl = document.getElementById('offset');
const componentEl = document.getElementById('component');
const quantityEl = document.getElementById('quantity');
const logEl = document.getElementById('log');
const resolutionEl = document.getElementById('resolution');
const sourceEl = document.getElementById('source');
const mapComponentEl = document.getElementById('mapComponent');
const mapQuantityEl = document.getElementById('mapQuantity');
const sweepComponentEl = document.getElementById('sweepComponent');
const sweepFieldEl = document.getElementById('sweepField');
const sweepRangeEl = document.getElementById('sweepRange');
let sweep = null; // {variable, values}, set by the Sweep Variable command
let nextReqId = 1;
const pending = new Map();

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const reqId = nextReqId++;
    pending.set(reqId, { resolve, reject });
    vscodeApi.postMessage({ type: 'rpcRequest', reqId, method, params });
  });
}

function plotTemplate() {
  const cls = document.body.className;
  const dark = /vscode-dark|vscode-high-contrast/.test(cls)
    && !cls.includes('vscode-high-contrast-light');
  return dark ? 'plotly_dark' : 'plotly_white';
}

/** Sensors carrying a measuring grid can be read off directly. */
async function loadSources() {
  const objects = await rpc('list_objects', {});
  const grids = objects.filter((o) => o.pixels);
  const chosen = sourceEl.value;
  sourceEl.innerHTML = '';
  sourceEl.append(new Option('on a plane', ''));
  for (const o of grids)
    sourceEl.append(new Option(
      o.label + ' (' + o.pixels[0] + '×' + o.pixels[1] + ')', o.id));
  // a scene with a measuring grid almost certainly wants to read it
  sourceEl.value = grids.some((o) => o.id === chosen) ? chosen
    : (grids.length ? grids[0].id : '');
}

async function refreshField() {
  const mode = modeEl.value;
  const mapMode = mode === 'map';
  const sweepMode = mode === 'sweep';
  for (const el of document.querySelectorAll('.path-only')) el.hidden = mode !== 'path';
  for (const el of document.querySelectorAll('.map-only')) el.hidden = !mapMode;
  for (const el of document.querySelectorAll('.plane-only'))
    el.hidden = !mapMode || !!sourceEl.value;
  for (const el of document.querySelectorAll('.sweep-only')) el.hidden = !sweepMode;
  if (sweepMode && !sweep) {
    statusEl.textContent = 'Run "Sweep a Variable…" to choose one and its range.';
    Plotly.purge(canvasEl);
    return;
  }
  statusEl.textContent = 'Computing…';
  try {
    const fig = sweepMode
      ? await rpc('get_sweep_figure', {
          variable: sweep.variable,
          values: sweep.values,
          component: sweepComponentEl.value,
          field: sweepFieldEl.value,
          template: plotTemplate(),
        })
      : mapMode
      ? await rpc('get_field_map', {
          // a sensor's own grid, when one is chosen: the measuring plane
          // is then a real object, tilting with the sensor
          ...(sourceEl.value
            ? { sensor_id: sourceEl.value }
            : {
                plane: planeEl.value,
                offset: parseFloat(offsetEl.value) || 0,
                resolution: Math.min(200, Math.max(5,
                  parseInt(resolutionEl.value, 10) || 50)),
              }),
          component: mapComponentEl.value,
          field: mapQuantityEl.value,
          log: logEl.checked && mapComponentEl.value === 'magnitude',
          template: plotTemplate(),
        })
      : await rpc('get_field_figure', {
          output: outputEl.value,
          animation: animateEl.checked,
          template: plotTemplate(),
        });
    const layout = fig.layout || {};
    layout.uirevision = 'magpylib-field-' + modeEl.value;
    layout.autosize = true;
    layout.margin = { l: 55, r: 15, t: mapMode ? 30 : 15, b: 40 };
    layout.paper_bgcolor = 'rgba(0,0,0,0)';
    layout.plot_bgcolor = 'rgba(0,0,0,0)';
    await Plotly.react(canvasEl, {
      data: fig.data,
      layout,
      frames: fig.frames || [],
      config: { responsive: true },
    });
    statusEl.textContent = 'Ready';
  } catch (err) {
    statusEl.textContent = sweepMode
      ? 'Could not sweep ' + sweep.variable + '. (' + err + ')'
      : mapMode
      ? 'No field to map - the scene needs at least one source. (' + err + ')'
      : 'No field to plot - the scene needs a source and a sensor. (' + err + ')';
  }
}

for (const el of [outputEl, animateEl, modeEl, planeEl, offsetEl,
                  componentEl, quantityEl, logEl, resolutionEl,
                  sourceEl, mapComponentEl, mapQuantityEl,
                  sweepComponentEl, sweepFieldEl]) {
  el.addEventListener('change', refreshField);
}

new MutationObserver(refreshField)
  .observe(document.body, { attributes: true, attributeFilter: ['class'] });

window.addEventListener('message', (event) => {
  const message = event.data;
  if (message.type === 'rpcResult' || message.type === 'rpcError') {
    const entry = pending.get(message.reqId);
    if (!entry) return;
    pending.delete(message.reqId);
    if (message.type === 'rpcResult') entry.resolve(message.result);
    else entry.reject(new Error(message.method + ': ' + message.error));
  } else if (message.type === 'sweep') {
    sweep = { variable: message.variable, values: message.values };
    const first = sweep.values[0], last = sweep.values[sweep.values.length - 1];
    sweepRangeEl.textContent =
      sweep.variable + ': ' + first + ' → ' + last +
      ' (' + sweep.values.length + ' steps)';
    modeEl.value = 'sweep';
    refreshField();
  } else if (message.type === 'refresh') {
    loadSources().then(refreshField)
      .catch((err) => { statusEl.textContent = String(err); });
  }
});

window.addEventListener('resize', () => {
  if (canvasEl.data) Plotly.Plots.resize(canvasEl);
});

refreshField();
