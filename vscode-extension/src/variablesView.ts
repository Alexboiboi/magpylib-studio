import * as vscode from 'vscode';

export interface VariableBounds {
  /** Hard limits: the engine rejects a value outside them. */
  min?: number;
  max?: number;
  /** Soft limits: the range worth dragging through; outside stays legal. */
  soft_min?: number;
  soft_max?: number;
}

export interface Variable {
  name: string;
  /** As written: a number, or an "=expr" over the other variables. */
  expression: number | string;
  /** As resolved at the last build. */
  value: number | null;
  bounds?: VariableBounds;
}

/** The range a slider should span: soft limits if given, else the hard ones. */
export function sliderRange(bounds?: VariableBounds): [number, number] | undefined {
  if (!bounds) {
    return undefined;
  }
  const low = bounds.soft_min ?? bounds.min;
  const high = bounds.soft_max ?? bounds.max;
  return low === undefined || high === undefined || low >= high
    ? undefined
    : [low, high];
}

/**
 * The scene's variables, as a webview rather than a tree.
 *
 * A TreeItem can hold a label and an icon and nothing else, and the point of
 * bounding a variable is to be able to *drag* it — so the panel that shows the
 * variables has to be one that can hold a slider. The cost is that per-row
 * actions are buttons in the row instead of a context menu; the view's title
 * bar still carries New Variable… and Sweep…
 */
export class VariablesViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'magpylib-studio.variablesView';

  private view: vscode.WebviewView | undefined;
  private ready = false;

  constructor(
    private readonly request: (
      method: string,
      params?: Record<string, unknown>,
    ) => Promise<unknown>,
    private readonly onAction: (action: string, name: string) => void,
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    this.ready = false;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this.html();

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === 'ready') {
        this.ready = true;
        this.refresh();
        return;
      }
      if (message.type === 'action') {
        this.onAction(message.action, message.name);
        return;
      }
      if (message.type !== 'rpcRequest') {
        return;
      }
      const { reqId, method, params } = message;
      try {
        const result = await this.request(method, params);
        webviewView.webview.postMessage({ type: 'rpcResult', reqId, method, result });
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
    body { margin: 0; padding: 4px 8px 8px; font-family: var(--vscode-font-family); font-size: 12px; color: var(--vscode-foreground); }
    .row { display: grid; grid-template-columns: minmax(52px, auto) 1fr 54px auto; gap: 5px; align-items: center; padding: 2px 0; }
    .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    input[type=range] { width: 100%; padding: 0; min-width: 40px; }
    input[type=text] { width: 100%; box-sizing: border-box; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); }
    input.expr { font-style: italic; color: var(--vscode-charts-blue, #3987e5); }
    .acts { display: flex; gap: 2px; }
    .acts button { background: none; border: none; color: var(--vscode-foreground); opacity: 0.55; cursor: pointer; padding: 0 3px; font-size: 12px; line-height: 1; }
    .acts button:hover { opacity: 1; }
    .range { grid-column: 2 / 4; font-size: 10px; opacity: 0.6; margin-top: -3px; }
    #empty { opacity: 0.75; padding: 6px 0; line-height: 1.4; }
    #status { color: var(--vscode-errorForeground); min-height: 14px; padding-top: 4px; }
  </style>
</head>
<body>
  <div id="list"></div>
  <div id="empty" hidden>
    No variables yet. A named number any position, dimension or angle can be
    written in terms of — change it once and the scene follows.
  </div>
  <div id="status"></div>
  <script>
    const vscodeApi = acquireVsCodeApi();
    const listEl = document.getElementById('list');
    const emptyEl = document.getElementById('empty');
    const statusEl = document.getElementById('status');
    let nextReqId = 1;
    const pending = new Map();

    function rpc(method, params) {
      return new Promise((resolve, reject) => {
        const reqId = nextReqId++;
        pending.set(reqId, { resolve, reject });
        vscodeApi.postMessage({ type: 'rpcRequest', reqId, method, params });
      });
    }

    function short(value) {
      if (value === null || value === undefined) return '?';
      return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(6)));
    }

    /** Typed text -> document value: a number if it is one, else "=expr". */
    function asValue(text) {
      const trimmed = String(text).trim();
      if (!trimmed) return 0;
      const number = Number(trimmed);
      return Number.isFinite(number) ? number : '=' + trimmed;
    }

    function commit(name, value) {
      statusEl.textContent = '';
      rpc('set_variable', { name, value })
        .then((res) => {
          if (res && res.ok === false) statusEl.textContent = res.error;
          return load();
        })
        .catch((err) => { statusEl.textContent = String(err); });
    }

    function button(glyph, title, action, name) {
      const el = document.createElement('button');
      el.textContent = glyph;
      el.title = title;
      el.addEventListener('click', () =>
        vscodeApi.postMessage({ type: 'action', action, name }));
      return el;
    }

    async function load() {
      const { variables } = await rpc('get_variables', {});
      listEl.innerHTML = '';
      emptyEl.hidden = variables.length > 0;
      for (const v of variables) {
        const row = document.createElement('div');
        row.className = 'row';

        const name = document.createElement('span');
        name.className = 'name';
        name.textContent = v.name;
        const isExpression = typeof v.expression === 'string';
        name.title = isExpression
          ? v.name + ' = ' + v.expression.slice(1) + ', currently ' + short(v.value)
          : v.name;

        // Soft bounds win: they are the range worth dragging through. A
        // variable defined by an expression is not draggable - its value
        // belongs to the expression, not to the slider.
        const b = v.bounds || {};
        const low = b.soft_min !== undefined ? b.soft_min : b.min;
        const high = b.soft_max !== undefined ? b.soft_max : b.max;
        const slidable = !isExpression && low !== undefined && high !== undefined
          && low < high;

        const text = document.createElement('input');
        text.type = 'text';
        text.spellcheck = false;
        text.value = isExpression ? v.expression.slice(1) : short(v.value);
        if (isExpression) { text.classList.add('expr'); text.title = 'currently ' + short(v.value); }
        text.addEventListener('change', () => commit(v.name, asValue(text.value)));

        const slot = document.createElement('div');
        if (slidable) {
          const slider = document.createElement('input');
          slider.type = 'range';
          slider.min = low;
          slider.max = high;
          slider.step = (high - low) / 100;
          slider.value = v.value;
          slider.title = short(low) + ' .. ' + short(high);
          // live text while dragging, one edit when released
          slider.addEventListener('input', () => { text.value = short(parseFloat(slider.value)); });
          slider.addEventListener('change', () => commit(v.name, parseFloat(slider.value)));
          slot.appendChild(slider);
        } else if (!isExpression) {
          const hint = document.createElement('span');
          hint.style.opacity = '0.5';
          hint.style.fontSize = '10px';
          hint.textContent = 'no range';
          hint.title = 'Give it a range to get a slider';
          slot.appendChild(hint);
        }

        const acts = document.createElement('div');
        acts.className = 'acts';
        acts.append(
          button('⋯', 'Set bounds…', 'bounds', v.name),
          button('✕', 'Remove ' + v.name, 'remove', v.name),
        );
        row.append(name, slot, text, acts);
        listEl.appendChild(row);

        // hard limits worth seeing when they differ from the slider's span
        const hard = b.min !== undefined || b.max !== undefined;
        if (hard && (b.soft_min !== undefined || b.soft_max !== undefined)) {
          const note = document.createElement('div');
          note.className = 'range';
          note.textContent = 'allowed ' +
            (b.min === undefined ? '−∞' : short(b.min)) + ' .. ' +
            (b.max === undefined ? '∞' : short(b.max));
          listEl.appendChild(note);
        }
      }
    }

    window.addEventListener('message', (event) => {
      const message = event.data;
      if (message.type === 'rpcResult' || message.type === 'rpcError') {
        const entry = pending.get(message.reqId);
        if (!entry) return;
        pending.delete(message.reqId);
        if (message.type === 'rpcResult') entry.resolve(message.result);
        else entry.reject(new Error(message.method + ': ' + message.error));
      } else if (message.type === 'refresh') {
        load().catch((err) => { statusEl.textContent = String(err); });
      }
    });

    vscodeApi.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}
