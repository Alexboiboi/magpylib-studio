// Selection and style editing live in the sidebar (Scene tree + Inspector);
// this panel is only the live 3D view.
const vscodeApi = acquireVsCodeApi();
const statusEl = document.getElementById('status');
const canvasEl = document.getElementById('canvas');
const animateEl = document.getElementById('animate');
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
  // VS Code stamps the theme kind on <body>; high-contrast-light is light.
  const cls = document.body.className;
  const dark = /vscode-dark|vscode-high-contrast/.test(cls)
    && !cls.includes('vscode-high-contrast-light');
  return dark ? 'plotly_dark' : 'plotly_white';
}

async function refreshFigure() {
  const figure = await rpc('get_figure', {
    animation: animateEl.checked,
    template: plotTemplate(),
  });
  const layout = figure.layout || {};
  layout.uirevision = 'magpylib-studio';  // hold camera across edits
  layout.autosize = true;
  layout.showlegend = false;  // the Scene tree is the legend
  layout.margin = { l: 0, r: 0, t: 0, b: 0 };
  layout.paper_bgcolor = 'rgba(0,0,0,0)';  // blend into the editor
  layout.scene = layout.scene || {};
  layout.scene.bgcolor = 'rgba(0,0,0,0)';
  await Plotly.react(canvasEl, {
    data: figure.data,
    layout,
    frames: figure.frames || [],
    config: { responsive: true },
  });
  statusEl.textContent = 'Ready';
}

// Re-render when the user switches the VS Code color theme.
new MutationObserver(() => {
  refreshFigure().catch((err) => { statusEl.textContent = String(err); });
}).observe(document.body, { attributes: true, attributeFilter: ['class'] });

animateEl.addEventListener('change', () => {
  statusEl.textContent = 'Loading…';
  refreshFigure().catch((err) => { statusEl.textContent = String(err); });
});

window.addEventListener('message', (event) => {
  const message = event.data;
  if (message.type === 'rpcResult' || message.type === 'rpcError') {
    const entry = pending.get(message.reqId);
    if (!entry) return;
    pending.delete(message.reqId);
    if (message.type === 'rpcResult') entry.resolve(message.result);
    else entry.reject(new Error(message.method + ': ' + message.error));
  } else if (message.type === 'refresh') {
    // Pushed by the host after any edit (inspector, chat tool, tree).
    refreshFigure().catch((err) => { statusEl.textContent = String(err); });
  }
});

window.addEventListener('resize', () => {
  if (canvasEl.data) Plotly.Plots.resize(canvasEl);
});

refreshFigure().catch((err) => { statusEl.textContent = 'Engine failed: ' + err; });
