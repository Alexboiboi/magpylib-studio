import * as vscode from 'vscode';

const MUTATING = new Set([
  'apply_edit',
  'reset_style',
  'move',
  'rotate',
  'set_transform',
  'clear_path',
  'set_param',
  'edit_event',
]);

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
        if (MUTATING.has(method)) {
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

  /** Show one construction step's own values above the object it acted on. */
  showOperation(eventId: string | undefined): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'operation', eventId });
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
    .vec { display: grid; grid-template-columns: 14px 1fr 14px 1fr 14px 1fr; gap: 3px; align-items: center; padding: 1px 0 1px 8px; }
    .vec span { opacity: 0.7; text-align: right; }
    .vec input { width: 100%; box-sizing: border-box; }
    .vec.readonly input { opacity: 0.6; background: transparent; border-color: transparent; cursor: default; }
    /* a field holding an expression rather than a number, hovering shows its value */
    input.expr { font-style: italic; color: var(--vscode-charts-blue, #3987e5); }
    .trow { display: flex; gap: 4px; align-items: center; padding: 2px 0 2px 8px; flex-wrap: wrap; }
    .trow input[type=number] { width: 52px; }
    .trow select { width: auto; }
    .trow button { background: var(--vscode-button-secondaryBackground, #3a3d41); color: var(--vscode-button-secondaryForeground, inherit); border: none; padding: 2px 8px; cursor: pointer; border-radius: 2px; }
    .trow button:hover { background: var(--vscode-button-secondaryHoverBackground, #45494e); }
    .hint { opacity: 0.65; padding: 0 0 2px 8px; }
    /* the selected construction step, so it is obvious what just changed */
    #step:not(:empty) { border-left: 2px solid var(--vscode-focusBorder, #3987e5); padding-left: 6px; margin: 4px 0; }
    #step summary { color: var(--vscode-charts-blue, #3987e5); }
  </style>
</head>
<body>
  <div id="header"></div>
  <div id="step"></div>
  <input id="filter" type="text" placeholder="Filter properties…" />
  <div id="params"></div>
  <div id="transform"></div>
  <div id="props"></div>
  <div id="empty">Select an object in the Scene view.</div>
  <div id="status"></div>
  <script>
    const vscodeApi = acquireVsCodeApi();
    const headerEl = document.getElementById('header');
    const stepEl = document.getElementById('step');
    const propsEl = document.getElementById('props');
    const transformEl = document.getElementById('transform');
    const paramsEl = document.getElementById('params');
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

    // --- step section: the selected construction step's own values --------
    //
    // Selecting a step in the Scene tree shows what it did, right above the
    // object it did it to: the property grid of a CAD history, rather than a
    // dialog you have to open and close.
    let stepId = null;

    const STEP_SKIP = ['id', 'op', 'target', 'type', 'children', 'style',
                       'hidden_style', 'visible', 'parent'];

    async function loadStep() {
      stepEl.innerHTML = '';
      if (!stepId) return;
      const [listed, document_] = await Promise.all([
        rpc('get_events', {}), rpc('to_dict', {}),
      ]);
      const shown = listed.events.find((e) => e.id === stepId);
      const stored = (document_.events || []).find((e) => e.id === stepId);
      if (!shown || !stored) { stepId = null; return; }

      const box = document.createElement('details');
      box.open = true;
      const summary = document.createElement('summary');
      summary.textContent = 'step — ' + shown.label;
      summary.title = shown.source;
      box.appendChild(summary);
      if (shown.error) {
        const why = document.createElement('div');
        why.className = 'hint';
        why.style.color = 'var(--vscode-errorForeground)';
        why.textContent = shown.error;
        box.appendChild(why);
      }

      // a create step carries the object's constructor parameters; every
      // other kind carries its own arguments
      const isCreate = shown.op === 'create';
      const values = isCreate ? (stored.params || {}) : stored;
      const commit = (name, value) => {
        statusEl.textContent = '';
        const changes = isCreate
          ? { params: Object.assign({}, values, { [name]: value }) }
          : { [name]: value };
        rpc('edit_event', { event_id: stepId, changes })
          .then((res) => {
            if (res && res.ok === false) statusEl.textContent = res.error;
            else if (res && res.broken && res.broken.length)
              statusEl.textContent = res.broken.length +
                ' later step(s) no longer apply — undo to put them back';
            return reloadAll();
          })
          .catch((err) => { statusEl.textContent = String(err); });
      };

      for (const name of Object.keys(values)) {
        if (STEP_SKIP.includes(name)) continue;
        const value = values[name];
        const row = document.createElement('div');
        row.className = 'row';
        const label = document.createElement('label');
        label.textContent = name;
        const wrap = document.createElement('div');
        wrap.className = 'widget';
        if (Array.isArray(value) && !Array.isArray(value[0])) {
          wrap.style.display = 'block';
          const resolved = value.map((v) => (typeof v === 'string' ? 0 : v));
          wrap.appendChild(vecRow(
            value.map((_, i) => String(i + 1)), resolved,
            (v) => commit(name, v), undefined, value,
          ));
        } else if (typeof value === 'number' || typeof value === 'string') {
          wrap.appendChild(numberInput(value, value, (v) => commit(name, v)));
        } else {
          const fixed = document.createElement('span');
          fixed.className = 'hint';
          fixed.textContent = JSON.stringify(value);
          wrap.appendChild(fixed);
        }
        row.append(label, wrap, document.createElement('span'));
        box.appendChild(row);
      }
      stepEl.appendChild(box);
    }

    // --- properties section: the object's physics parameters --------------
    async function loadParams() {
      const params = await rpc('get_params', { object_id: objectId });
      paramsEl.innerHTML = '';
      if (!params.length) return;
      const box = document.createElement('details');
      box.open = true;
      const summary = document.createElement('summary');
      summary.textContent = 'properties';
      box.appendChild(summary);

      for (const p of params) {
        const commit = (value) => {
          statusEl.textContent = '';
          rpc('set_param', { object_id: objectId, name: p.name, value })
            .then((res) => {
              if (res && res.ok === false) statusEl.textContent = res.error;
              return Promise.all([loadParams(), loadTransform()]);
            })
            .catch((err) => { statusEl.textContent = String(err); });
        };
        if (p.kind === 'scalar') {
          const row = document.createElement('div');
          row.className = 'row';
          const label = document.createElement('label');
          label.textContent = p.name;
          label.title = p.doc;
          const input = numberInput(
            p.written === undefined ? p.value : p.written, p.value, commit,
          );
          const wrap = document.createElement('div');
          wrap.className = 'widget';
          wrap.appendChild(input);
          row.append(label, wrap, document.createElement('span'));
          box.appendChild(row);
        } else if (p.kind === 'vector') {
          const head = document.createElement('div');
          head.className = 'hint';
          head.textContent = p.name;
          head.title = p.doc;
          box.appendChild(head);
          box.appendChild(
            vecRow(p.value.map((_, i) => String(i + 1)), p.value, commit,
                   undefined, p.written),
          );
        } else {
          // matrices (vertices, faces, sensor pixels): edit as JSON
          const head = document.createElement('div');
          head.className = 'hint';
          head.textContent = p.name + ' (' + p.value.length + ' rows, JSON)';
          head.title = p.doc;
          const area = document.createElement('input');
          area.type = 'text';
          area.value = JSON.stringify(p.value);
          area.addEventListener('change', () => {
            try {
              commit(JSON.parse(area.value));
            } catch (err) {
              statusEl.textContent = 'invalid JSON: ' + err;
            }
          });
          const wrap = document.createElement('div');
          wrap.className = 'trow';
          wrap.appendChild(area);
          box.append(head, wrap);
        }
      }
      paramsEl.appendChild(box);
    }

    // --- numbers that may be written as expressions -----------------------
    //
    // A field holds either a number or an expression over the document's
    // variables, so the widgets are text inputs, not number inputs: a number
    // input cannot hold "gap*2" at all. What the user types goes back as
    // typed; only a value that parses as a number is sent as one.

    function short(value) {
      return Number(value).toFixed(4).replace(/\.?0+$/, '');
    }

    /** Document value -> what to show in the field. */
    function asWritten(value, resolved) {
      if (typeof value === 'string' && value.startsWith('=')) return value.slice(1);
      return short(resolved);
    }

    /** Field text -> document value: a number if it is one, else "=expr". */
    function asValue(text) {
      const trimmed = String(text).trim();
      if (!trimmed) return 0;
      const number = Number(trimmed);
      return Number.isFinite(number) ? number : '=' + trimmed;
    }

    function numberInput(value, resolved, onCommit) {
      const input = document.createElement('input');
      input.type = 'text';
      input.spellcheck = false;
      input.value = asWritten(value, resolved);
      const isExpression = input.value !== short(resolved);
      if (isExpression) {
        input.classList.add('expr');
        input.title = 'expression — currently ' + short(resolved);
      }
      input.addEventListener('change', () => onCommit(asValue(input.value)));
      return input;
    }

    // --- transform section: absolute pose, relative ops, path tools -------
    function vecRow(labels, values, onCommit, readonly, written) {
      const row = document.createElement('div');
      row.className = 'vec' + (readonly ? ' readonly' : '');
      const inputs = [];
      labels.forEach((name, i) => {
        const tag = document.createElement('span');
        tag.textContent = name;
        const input = numberInput(written ? written[i] : values[i], values[i], () =>
          onCommit(inputs.map((el) => asValue(el.value))),
        );
        if (readonly) {
          input.readOnly = true;
          input.tabIndex = -1;
          input.title = readonly;
        }
        inputs.push(input);
        row.append(tag, input);
      });
      return row;
    }

    function transformOp(method, params) {
      statusEl.textContent = '';
      return rpc(method, Object.assign({ object_id: objectId }, params))
        .then((res) => {
          if (res && res.ok === false) statusEl.textContent = res.error;
          return loadTransform();
        })
        .catch((err) => { statusEl.textContent = String(err); });
    }

    async function loadTransform() {
      const t = await rpc('get_transform', { object_id: objectId });
      transformEl.innerHTML = '';
      const box = document.createElement('details');
      box.open = true;
      const summary = document.createElement('summary');
      summary.textContent = 'transform';
      box.appendChild(summary);

      // With a path there is no single pose to edit: the fields show the last
      // step read-only, and move/rotate/Clear path do the editing instead.
      const pathed = t.path_length > 1
        ? 'read-only while this object has a path (' + t.path_length +
          ' steps) — use move/rotate below, or Clear path'
        : '';
      if (pathed) {
        const hint = document.createElement('div');
        hint.className = 'hint';
        hint.textContent = 'path: ' + t.path_length + ' steps (showing the last)';
        box.appendChild(hint);
      }
      box.appendChild(vecRow(['x', 'y', 'z'], t.position, (v) =>
        transformOp('set_transform', { position: v }),
      pathed, t.written_position));
      box.appendChild(vecRow(['rx', 'ry', 'rz'], t.orientation, (v) =>
        transformOp('set_transform', { orientation: v }),
      pathed, t.written_orientation));

      // relative rotate, optionally orbiting the origin, optionally a path
      const row = document.createElement('div');
      row.className = 'trow';
      const angle = document.createElement('input');
      angle.type = 'text'; angle.value = '45'; angle.spellcheck = false;
      angle.title = 'degrees, or an expression over the variables';
      const axis = document.createElement('select');
      for (const a of ['z', 'x', 'y']) axis.append(new Option(a, a));
      const orbit = document.createElement('label');
      const orbitBox = document.createElement('input');
      orbitBox.type = 'checkbox';
      orbit.append(orbitBox, document.createTextNode(' orbit origin'));
      const steps = document.createElement('input');
      steps.type = 'number'; steps.min = '1'; steps.value = '1'; steps.title = 'path steps';
      const start = document.createElement('input');
      start.type = 'text'; start.value = 'auto'; start.style.width = '46px';
      start.title = "magpylib start: 'auto' appends the new path, or a path index";
      const go = document.createElement('button');
      go.textContent = 'Rotate';
      go.addEventListener('click', () => {
        const n = Math.max(1, parseInt(steps.value, 10) || 1);
        const total = asValue(angle.value);
        if (n > 1 && typeof total === 'string') {
          // the steps are divided up here, which needs a number; a symbolic
          // single rotation is fine and stays live
          statusEl.textContent = 'A multi-step path needs a number, not an expression.';
          return;
        }
        const value = n === 1
          ? total
          : Array.from({ length: n }, (_, i) => (total * (i + 1)) / n);
        const params = { angle: value, axis: axis.value };
        if (orbitBox.checked) params.anchor = 0;
        if (n > 1 && start.value.trim() !== 'auto' && start.value.trim() !== '') {
          params.start = parseInt(start.value, 10);
        }
        transformOp('rotate', params);
      });
      row.append(document.createTextNode('rotate'), angle, axis, orbit,
                 document.createTextNode('steps'), steps,
                 document.createTextNode('start'), start, go);
      box.appendChild(row);

      // relative move, optionally as a path
      const mrow = document.createElement('div');
      mrow.className = 'trow';
      const dxyz = ['0', '0', '1'].map((d) => {
        const el = document.createElement('input');
        el.type = 'text'; el.value = d; el.style.width = '46px';
        el.spellcheck = false;
        el.title = 'metres, or an expression over the variables';
        return el;
      });
      const msteps = document.createElement('input');
      msteps.type = 'number'; msteps.min = '1'; msteps.value = '1'; msteps.title = 'path steps';
      const mstart = document.createElement('input');
      mstart.type = 'text'; mstart.value = 'auto'; mstart.style.width = '46px';
      mstart.title = "magpylib start: 'auto' appends the new path, or a path index";
      const mgo = document.createElement('button');
      mgo.textContent = 'Move';
      mgo.addEventListener('click', () => {
        const d = dxyz.map((el) => asValue(el.value));
        const n = Math.max(1, parseInt(msteps.value, 10) || 1);
        if (n > 1 && d.some((c) => typeof c === 'string')) {
          statusEl.textContent = 'A multi-step path needs numbers, not expressions.';
          return;
        }
        const value = n === 1
          ? d
          : Array.from({ length: n }, (_, i) => d.map((c) => (c * (i + 1)) / n));
        const params = { displacement: value };
        if (n > 1 && mstart.value.trim() !== 'auto' && mstart.value.trim() !== '') {
          params.start = parseInt(mstart.value, 10);
        }
        transformOp('move', params);
      });
      mrow.append(document.createTextNode('move'), ...dxyz,
                  document.createTextNode('steps'), msteps,
                  document.createTextNode('start'), mstart, mgo);
      box.appendChild(mrow);

      if (t.path_length > 1) {
        const prow = document.createElement('div');
        prow.className = 'trow';
        const clear = document.createElement('button');
        clear.textContent = 'Clear path';
        clear.addEventListener('click', () => transformOp('clear_path', {}));
        prow.append(clear);
        box.appendChild(prow);
      }
      transformEl.appendChild(box);
    }

    async function reloadValues() {
      values = await rpc('get_values', { object_id: objectId });
      render();
      await Promise.all([loadParams(), loadTransform()]);
    }

    async function loadObject(id) {
      objectId = id;
      emptyEl.style.display = id ? 'none' : '';
      statusEl.textContent = '';
      if (!id) {
        headerEl.textContent = '';
        propsEl.innerHTML = '';
        transformEl.innerHTML = '';
        paramsEl.innerHTML = '';
        stepEl.innerHTML = '';
        return;
      }
      headerEl.textContent = id;
      [schema, values] = await Promise.all([
        rpc('get_schema', { object_id: id }),
        rpc('get_values', { object_id: id }),
      ]);
      render();
      await Promise.all([loadParams(), loadTransform()]);
    }

    /** The step form and the object's own sections, both back from source. */
    async function reloadAll() {
      await loadStep();
      if (objectId) await reloadValues();
    }

    filterEl.addEventListener('input', render);

    window.addEventListener('message', (event) => {
      const message = event.data;
      const fail = (err) => { statusEl.textContent = String(err); };
      if (message.type === 'rpcResult' || message.type === 'rpcError') {
        const entry = pending.get(message.reqId);
        if (!entry) return;
        pending.delete(message.reqId);
        if (message.type === 'rpcResult') entry.resolve(message.result);
        else entry.reject(new Error(message.method + ': ' + message.error));
      } else if (message.type === 'select') {
        // picking an object directly is not picking a step: clear the form
        // before loading, so a stale step cannot linger above a new object
        stepId = null;
        loadObject(message.objectId).catch(fail);
      } else if (message.type === 'operation') {
        stepId = message.eventId;
        loadStep().catch(fail);
      } else if (message.type === 'refresh') {
        reloadAll().catch(fail);
      }
    });

    vscodeApi.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}
