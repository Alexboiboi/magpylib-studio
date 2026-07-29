import * as vscode from 'vscode';

export interface SceneEvent {
  index: number;
  id: string;
  target: string;
  op: string;
  /** The line this event stands for, e.g. "cube.rotate_from_angax(45, 'z')". */
  source: string;
  /** Present when the last rebuild could not apply it. */
  error?: string;
}

/**
 * The scene's construction history: every event in order, the ones that could
 * not be applied marked with why.
 *
 * This is the document, not a record of it — the object tree is folded out of
 * these. Editing one re-applies everything after it, which is why the panel
 * exists at all: the script tab can express the same log, but not reordering
 * it, and order is semantic (an orbit then a move lands elsewhere than a move
 * then an orbit).
 */
export class EventsViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'magpylib-studio.eventsView';

  private view: vscode.WebviewView | undefined;
  private ready = false;

  constructor(
    private readonly request: (
      method: string,
      params?: Record<string, unknown>,
    ) => Promise<unknown>,
    private readonly onAction: (action: string, event: SceneEvent) => void,
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
        this.onAction(message.action, message.event);
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
    body { margin: 0; padding: 4px 6px 8px; font-family: var(--vscode-font-family); font-size: 12px; color: var(--vscode-foreground); }
    .row { display: grid; grid-template-columns: 1fr auto; gap: 4px; align-items: start; padding: 2px 0 2px 4px; border-left: 2px solid transparent; }
    .row:hover { background: var(--vscode-list-hoverBackground); }
    .row.broken { border-left-color: var(--vscode-errorForeground); }
    .row.create { opacity: 0.85; }
    .src { font-family: var(--vscode-editor-font-family, monospace); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .why { grid-column: 1 / -1; color: var(--vscode-errorForeground); font-size: 10px; padding-left: 2px; }
    .acts { display: flex; gap: 1px; visibility: hidden; }
    .row:hover .acts { visibility: visible; }
    .acts button { background: none; border: none; color: var(--vscode-foreground); opacity: 0.6; cursor: pointer; padding: 0 2px; font-size: 11px; line-height: 1; }
    .acts button:hover { opacity: 1; }
    #empty { opacity: 0.75; padding: 6px 2px; line-height: 1.4; }
    #status { color: var(--vscode-errorForeground); min-height: 14px; padding-top: 4px; }
  </style>
</head>
<body>
  <div id="list"></div>
  <div id="empty" hidden>
    Nothing has happened yet. Everything that builds the scene — objects
    appearing, moves, rotations — lands here in order, and editing one entry
    re-applies the ones after it.
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

    function button(glyph, title, action, event, enabled) {
      const el = document.createElement('button');
      el.textContent = glyph;
      el.title = title;
      el.disabled = !enabled;
      if (!enabled) el.style.opacity = '0.2';
      el.addEventListener('click', () =>
        vscodeApi.postMessage({ type: 'action', action, event }));
      return el;
    }

    async function load() {
      const { events } = await rpc('get_events', {});
      listEl.innerHTML = '';
      emptyEl.hidden = events.length > 0;
      events.forEach((event, i) => {
        const row = document.createElement('div');
        row.className = 'row' + (event.error ? ' broken' : '')
          + (event.op === 'create' ? ' create' : '');

        const src = document.createElement('div');
        src.className = 'src';
        src.textContent = event.source;
        src.title = event.source + '  [' + event.id + ']';

        const acts = document.createElement('div');
        acts.className = 'acts';
        acts.append(
          button('↑', 'Move earlier', 'up', event, i > 0),
          button('↓', 'Move later', 'down', event, i < events.length - 1),
          // on a create this edits the object's constructor parameters —
          // changing a dimension after the fact, from the step that set it
          button('✎', event.op === 'create'
            ? 'Change a parameter…'
            : 'Change a value…', 'edit', event, true),
          button('✕', 'Remove from the history', 'remove', event, true),
        );
        row.append(src, acts);
        if (event.error) {
          const why = document.createElement('div');
          why.className = 'why';
          why.textContent = event.error;
          row.appendChild(why);
        }
        listEl.appendChild(row);
      });
    }

    window.addEventListener('message', (message) => {
      const data = message.data;
      if (data.type === 'rpcResult' || data.type === 'rpcError') {
        const entry = pending.get(data.reqId);
        if (!entry) return;
        pending.delete(data.reqId);
        if (data.type === 'rpcResult') entry.resolve(data.result);
        else entry.reject(new Error(data.method + ': ' + data.error));
      } else if (data.type === 'refresh') {
        load().catch((err) => { statusEl.textContent = String(err); });
      }
    });

    vscodeApi.postMessage({ type: 'ready' });
  </script>
</body>
</html>`;
  }
}
