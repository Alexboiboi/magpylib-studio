import * as vscode from 'vscode';

/**
 * Sidebar style inspector: schema-driven widgets for the selected object.
 * Widgets are generated in the webview from `get_schema` (enum -> dropdown,
 * format:color -> picker, bounded number -> slider, boolean -> tri-state) and
 * filled from `get_values` (resolved values shown; explicitly set paths get a
 * reset button). All edits go through the shared engine RPC router.
 */
export class InspectorViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'magpylib-studio.inspectorView';

  private view: vscode.WebviewView | undefined;
  private ready = false;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly request: (method: string, params?: Record<string, unknown>) => Promise<unknown>,
    private readonly onMutation: () => void,
    private readonly getSelection: () => string | undefined,
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    this.ready = false;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };
    webviewView.webview.html = this.html();

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === 'ready') {
        this.ready = true;
        this.select(this.getSelection());
        return;
      }
      if (message.type !== 'rpcRequest') {
        return;
      }
      const { reqId, method, params } = message;
      try {
        const result = await this.request(method, params);
        webviewView.webview.postMessage({ type: 'rpcResult', reqId, method, result });
        if (method === 'apply_edit' || method === 'reset_style') {
          this.onMutation();
        }
      } catch (err) {
        webviewView.webview.postMessage({
          type: 'rpcError',
          reqId,
          method,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    });

    webviewView.onDidDispose(() => {
      this.view = undefined;
      this.ready = false;
    });
  }

  select(objectId: string | undefined): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'select', objectId });
    }
  }

  /** External change (chat tool, tree action): re-pull values. */
  refresh(): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'refresh' });
    }
  }

  private html(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';" />
  <style>
    body { margin: 0; padding: 4px 8px; font-family: var(--vscode-font-family); font-size: 12px; color: var(--vscode-foreground); }
    #header { font-weight: bold; margin: 4px 0; }
    #filter { width: 100%; box-sizing: border-box; margin-bottom: 6px; }
    details { margin: 2px 0; }
    summary { cursor: pointer; text-transform: uppercase; font-size: 11px; opacity: 0.8; user-select: none; }
    .row { display: grid; grid-template-columns: minmax(80px, 1fr) minmax(90px, 1fr) 18px; gap: 4px; align-items: center; padding: 1px 0 1px 8px; }
    .row label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .row.set label { font-weight: bold; }
    .widget { display: flex; gap: 3px; align-items: center; min-width: 0; }
    input, select { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); font-size: 12px; padding: 1px 3px; min-width: 0; width: 100%; box-sizing: border-box; }
    input[type=color] { padding: 0; width: 22px; height: 18px; flex: none; }
    input[type=range] { padding: 0; }
    .num { width: 48px; flex: none; }
    .reset { cursor: pointer; border: none; background: none; color: var(--vscode-foreground); opacity: 0.7; padding: 0; font-size: 12px; visibility: hidden; }
    .row.set .reset { visibility: visible; }
    #status { color: var(--vscode-errorForeground); white-space: pre-wrap; margin-top: 6px; }
    #empty { opacity: 0.7; margin-top: 10px; }
  </style>
</head>
<body>
  <div id="header"></div>
  <input id="filter" type="text" placeholder="Filter properties…" />
  <div id="props"></div>
  <div id="empty">Select an object in the Scene view.</div>
  <div id="status"></div>
  <script>
    const vscodeApi = acquireVsCodeApi();
    const headerEl = document.getElementById('header');
    const propsEl = document.getElementById('props');
    const emptyEl = document.getElementById('empty');
    const statusEl = document.getElementById('status');
    const filterEl = document.getElementById('filter');
    let objectId;
    let schema;
    let values = { set: {}, resolved: {} };
    let nextReqId = 1;
    const pending = new Map();

    function rpc(method, params) {
      return new Promise((resolve, reject) => {
        const reqId = nextReqId++;
        pending.set(reqId, { resolve, reject });
        vscodeApi.postMessage({ type: 'rpcRequest', reqId, method, params });
      });
    }

    function leafPaths(props, prefix, out) {
      for (const [name, spec] of Object.entries(props)) {
        const path = prefix ? prefix + '.' + name : name;
        if (spec.properties) leafPaths(spec.properties, path, out);
        else out.push([path, spec]);
      }
      return out;
    }

    async function applyEdit(path, value) {
      statusEl.textContent = '';
      const res = await rpc('apply_edit', { object_id: objectId, path, value });
      if (!res.ok) { statusEl.textContent = res.error; }
      await reloadValues();
    }

    async function resetPath(path) {
      statusEl.textContent = '';
      const res = await rpc('reset_style', { object_id: objectId, path });
      if (!res.ok) { statusEl.textContent = res.error; }
      await reloadValues();
    }

    function makeWidget(path, spec, value) {
      const wrap = document.createElement('div');
      wrap.className = 'widget';
      const types = [].concat(spec.type || []);
      const enums = (spec.enum || []).filter((v) => typeof v === 'string');

      if (spec.format === 'color') {
        const text = document.createElement('input');
        text.type = 'text';
        text.value = value ?? '';
        text.placeholder = 'default';
        text.addEventListener('change', () => {
          if (text.value) applyEdit(path, text.value);
        });
        const pick = document.createElement('input');
        pick.type = 'color';
        if (/^#[0-9a-fA-F]{6}$/.test(value || '')) pick.value = value;
        pick.addEventListener('change', () => applyEdit(path, pick.value));
        wrap.append(text, pick);
      } else if (enums.length) {
        const sel = document.createElement('select');
        sel.append(new Option('(default)', ''));
        for (const opt of enums) sel.append(new Option(opt, opt));
        sel.value = typeof value === 'string' ? value : '';
        sel.addEventListener('change', () => {
          if (sel.value) applyEdit(path, sel.value);
          else if (path in values.set) resetPath(path);
        });
        wrap.append(sel);
      } else if (types.includes('boolean')) {
        const sel = document.createElement('select');
        sel.append(new Option('(default)', ''), new Option('true', 'true'), new Option('false', 'false'));
        sel.value = value === true ? 'true' : value === false ? 'false' : '';
        sel.addEventListener('change', () => {
          if (sel.value) applyEdit(path, sel.value === 'true');
          else if (path in values.set) resetPath(path);
        });
        wrap.append(sel);
      } else if (types.includes('number')) {
        const num = document.createElement('input');
        num.type = 'number';
        num.step = 'any';
        num.className = 'num';
        if (value !== null && value !== undefined) num.value = value;
        num.addEventListener('change', () => {
          if (num.value !== '') applyEdit(path, parseFloat(num.value));
          else if (path in values.set) resetPath(path);
        });
        if (spec.minimum !== undefined && spec.maximum !== undefined) {
          const slider = document.createElement('input');
          slider.type = 'range';
          slider.min = spec.minimum;
          slider.max = spec.maximum;
          slider.step = (spec.maximum - spec.minimum) / 100;
          if (value !== null && value !== undefined) slider.value = value;
          slider.addEventListener('input', () => { num.value = slider.value; });
          slider.addEventListener('change', () => applyEdit(path, parseFloat(slider.value)));
          wrap.append(slider);
        }
        wrap.append(num);
      } else if (types.includes('string')) {
        const text = document.createElement('input');
        text.type = 'text';
        text.value = value ?? '';
        text.placeholder = 'default';
        text.addEventListener('change', () => {
          if (text.value) applyEdit(path, text.value);
          else if (path in values.set) resetPath(path);
        });
        wrap.append(text);
      } else {
        return null; // free-form specs (model3d.data, path.frames): not editable here
      }
      return wrap;
    }

    function render() {
      const openGroups = new Set(
        Array.from(propsEl.querySelectorAll('details[open]')).map((d) => d.dataset.group),
      );
      propsEl.innerHTML = '';
      if (!schema || !objectId) return;
      const filter = filterEl.value.trim().toLowerCase();
      for (const [group, spec] of Object.entries(schema.properties)) {
        const leaves = spec.properties ? leafPaths(spec.properties, group, []) : [[group, spec]];
        const rows = [];
        for (const [path, leafSpec] of leaves) {
          if (filter && !path.toLowerCase().includes(filter)) continue;
          const widget = makeWidget(path, leafSpec, values.resolved[path]);
          if (!widget) continue;
          const row = document.createElement('div');
          row.className = 'row' + (path in values.set ? ' set' : '');
          const label = document.createElement('label');
          label.textContent = path.startsWith(group + '.') ? path.slice(group.length + 1) : path;
          label.title = path + (leafSpec.description ? ' — ' + leafSpec.description : '');
          const reset = document.createElement('button');
          reset.className = 'reset';
          reset.textContent = '↺';
          reset.title = 'Reset to default';
          reset.addEventListener('click', () => resetPath(path));
          row.append(label, widget, reset);
          rows.push(row);
        }
        if (!rows.length) continue;
        const details = document.createElement('details');
        details.dataset.group = group;
        if (filter || openGroups.has(group) || !spec.properties) details.open = true;
        const summary = document.createElement('summary');
        summary.textContent = group;
        details.append(summary, ...rows);
        propsEl.appendChild(details);
      }
    }

    async function reloadValues() {
      values = await rpc('get_values', { object_id: objectId });
      render();
    }

    async function loadObject(id) {
      objectId = id;
      emptyEl.style.display = id ? 'none' : '';
      statusEl.textContent = '';
      if (!id) { headerEl.textContent = ''; propsEl.innerHTML = ''; return; }
      headerEl.textContent = id;
      [schema, values] = await Promise.all([
        rpc('get_schema', { object_id: id }),
        rpc('get_values', { object_id: id }),
      ]);
      render();
    }

    filterEl.addEventListener('input', render);

    window.addEventListener('message', (event) => {
      const message = event.data;
      if (message.type === 'rpcResult' || message.type === 'rpcError') {
        const entry = pending.get(message.reqId);
        if (!entry) return;
        pending.delete(message.reqId);
        if (message.type === 'rpcResult') entry.resolve(message.result);
        else entry.reject(new Error(message.method + ': ' + message.error));
      } else if (message.type === 'select') {
        loadObject(message.objectId).catch((err) => { statusEl.textContent = String(err); });
      } else if (message.type === 'refresh') {
        if (objectId) reloadValues().catch((err) => { statusEl.textContent = String(err); });
      }
    });

    vscodeApi.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}
