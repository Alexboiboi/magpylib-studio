import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { EngineClient } from './engineClient';

let engine: EngineClient | undefined;
let currentPanel: vscode.WebviewPanel | undefined;
let engineOutput: vscode.OutputChannel | undefined;

/** Repo root in the dev layout (vscode-extension/ inside the repo). */
function repoRoot(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, '..');
}

function findPython(context: vscode.ExtensionContext): string {
  const configured = vscode.workspace
    .getConfiguration('magpylib-studio')
    .get<string>('pythonPath');
  if (configured) {
    return configured;
  }
  const candidates: string[] = [];
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    candidates.push(path.join(folder.uri.fsPath, '.venv'));
  }
  candidates.push(path.join(repoRoot(context), '.venv'));
  for (const venv of candidates) {
    for (const python of [
      path.join(venv, 'bin', 'python'),
      path.join(venv, 'Scripts', 'python.exe'),
    ]) {
      if (fs.existsSync(python)) {
        return python;
      }
    }
  }
  return 'python3';
}

function getEngine(context: vscode.ExtensionContext): EngineClient {
  if (engine?.isRunning) {
    return engine;
  }
  const pythonPath = findPython(context);
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? repoRoot(context);
  engineOutput ??= vscode.window.createOutputChannel('magpylib Studio Engine');
  const client = new EngineClient(pythonPath, cwd);
  client.onStderr = (text) => engineOutput?.append(text);
  client.onExit = (code) => {
    engineOutput?.appendLine(`\n[engine exited with code ${code}]`);
    if (engine === client) {
      engine = undefined;
    }
  };
  engine = client;
  return client;
}

function getNonce(): string {
  return Array.from({ length: 32 }, () =>
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'.charAt(
      Math.floor(Math.random() * 62),
    ),
  ).join('');
}

function openStudioPanel(context: vscode.ExtensionContext): void {
  if (currentPanel) {
    currentPanel.reveal();
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    'magpylibStudio',
    'magpylib Studio',
    vscode.ViewColumn.One,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [context.extensionUri],
    },
  );
  currentPanel = panel;
  panel.webview.html = createWebviewHtml(context, panel.webview);

  panel.webview.onDidReceiveMessage(async (message) => {
    if (message.type !== 'rpcRequest') {
      return;
    }
    const { reqId, method, params } = message;
    try {
      const result = await getEngine(context).request(method, params);
      panel.webview.postMessage({ type: 'rpcResult', reqId, method, result });
    } catch (err) {
      panel.webview.postMessage({
        type: 'rpcError',
        reqId,
        method,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  });

  panel.onDidDispose(() => {
    currentPanel = undefined;
  });
}

/** Tell the open Studio panel (if any) to re-pull figure + values. */
function refreshPanel(): void {
  currentPanel?.webview.postMessage({ type: 'refresh' });
}

function toolResult(payload: unknown): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([
    new vscode.LanguageModelTextPart(JSON.stringify(payload)),
  ]);
}

function registerLmTools(context: vscode.ExtensionContext): void {
  /** Read-only tool: forward input as RPC params, return the result. */
  const queryTool = (toolName: string, method: string) =>
    vscode.lm.registerTool(toolName, {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<object>) {
        return toolResult(
          await getEngine(context).request(
            method,
            options.input as Record<string, unknown>,
          ),
        );
      },
    });
  /** Mutating tool: same, but refresh the Studio panel when the edit sticks. */
  const editTool = (toolName: string, method: string) =>
    vscode.lm.registerTool(toolName, {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<object>) {
        const result = (await getEngine(context).request(
          method,
          options.input as Record<string, unknown>,
        )) as { ok: boolean; error?: string };
        if (result.ok) {
          refreshPanel();
        }
        return toolResult(result);
      },
    });
  context.subscriptions.push(
    queryTool('magpylib-studio_listObjects', 'list_objects'),
    queryTool('magpylib-studio_getSchema', 'get_schema'),
    editTool('magpylib-studio_applyEdit', 'apply_edit'),
    editTool('magpylib-studio_addObject', 'add_object'),
    editTool('magpylib-studio_removeObject', 'remove_object'),
    editTool('magpylib-studio_setParam', 'set_param'),
  );
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('magpylib-studio.openStudio', () =>
      openStudioPanel(context),
    ),
    new vscode.Disposable(() => {
      engine?.dispose();
      engine = undefined;
    }),
  );
  registerLmTools(context);
}

export function deactivate(): void {
  engine?.dispose();
  engine = undefined;
}

function createWebviewHtml(
  context: vscode.ExtensionContext,
  webview: vscode.Webview,
): string {
  const nonce = getNonce();
  const plotlyUri = webview.asWebviewUri(
    vscode.Uri.joinPath(
      context.extensionUri,
      'node_modules',
      'plotly.js-dist-min',
      'plotly.min.js',
    ),
  );
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} data: blob:; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}' ${webview.cspSource}; font-src ${webview.cspSource};" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>magpylib Studio</title>
  <style>
    body { margin: 0; font-family: var(--vscode-font-family, sans-serif); color: var(--vscode-foreground); }
    #toolbar { padding: 8px 12px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid var(--vscode-panel-border, #444); }
    #toolbar select, #toolbar input { min-width: 160px; }
    #canvas { width: 100%; height: 55vh; }
    #inspector { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 12px; border-top: 1px solid var(--vscode-panel-border, #444); }
    pre { white-space: pre-wrap; font-size: 12px; overflow: auto; max-height: 24vh; background: var(--vscode-textCodeBlock-background, #1e1e1e); border: 1px solid var(--vscode-panel-border, #444); padding: 8px; margin: 4px 0; }
    #log { max-height: 10vh; }
    input, select, button { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, #555); padding: 3px 6px; }
    button { cursor: pointer; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 4px 10px; }
    h3 { margin: 4px 0; font-size: 12px; text-transform: uppercase; opacity: 0.8; }
    label { font-size: 12px; }
  </style>
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="toolbar">
    <button id="refresh">Refresh</button>
    <label for="objectSelect">Object:</label>
    <select id="objectSelect"></select>
    <input id="stylePath" type="text" placeholder="style path, e.g. color" />
    <input id="styleValue" type="text" placeholder="value, e.g. red" />
    <button id="applyEdit">Apply edit</button>
    <span id="status">Starting…</span>
  </div>
  <div id="canvas"></div>
  <div id="inspector">
    <div>
      <h3>Style schema</h3>
      <pre id="schema">–</pre>
    </div>
    <div>
      <h3>Set values</h3>
      <pre id="values">–</pre>
      <h3>Log</h3>
      <pre id="log"></pre>
    </div>
  </div>
  <script nonce="${nonce}">
    const vscodeApi = acquireVsCodeApi();
    const statusEl = document.getElementById('status');
    const logEl = document.getElementById('log');
    const canvasEl = document.getElementById('canvas');
    const objectSelect = document.getElementById('objectSelect');
    const schemaEl = document.getElementById('schema');
    const valuesEl = document.getElementById('values');
    let selectedObjectId = '';
    let nextReqId = 1;
    const pending = new Map();

    function log(message) {
      logEl.textContent += message + '\\n';
      logEl.scrollTop = logEl.scrollHeight;
    }
    function setStatus(message) { statusEl.textContent = message; }

    function rpc(method, params) {
      return new Promise((resolve, reject) => {
        const reqId = nextReqId++;
        pending.set(reqId, { resolve, reject });
        vscodeApi.postMessage({ type: 'rpcRequest', reqId, method, params });
      });
    }

    async function refreshFigure() {
      const figure = await rpc('get_figure');
      const layout = figure.layout || {};
      layout.uirevision = 'magpylib-studio';  // hold camera across edits
      layout.autosize = true;
      Plotly.react(canvasEl, figure.data, layout, { responsive: true });
    }

    async function loadObjectDetails() {
      if (!selectedObjectId) return;
      const [schema, values] = await Promise.all([
        rpc('get_schema', { object_id: selectedObjectId }),
        rpc('get_values', { object_id: selectedObjectId }),
      ]);
      schemaEl.textContent = JSON.stringify(schema, null, 2);
      valuesEl.textContent = JSON.stringify(values.set, null, 2);
    }

    async function refreshAll() {
      setStatus('Loading…');
      const objects = await rpc('list_objects');
      objectSelect.innerHTML = '';
      for (const obj of objects) {
        const opt = document.createElement('option');
        opt.value = obj.id;
        opt.textContent = (obj.label || obj.id) + ' (' + obj.type + ')';
        objectSelect.appendChild(opt);
      }
      if (!objects.some((o) => o.id === selectedObjectId)) {
        selectedObjectId = objects[0] ? objects[0].id : '';
      }
      objectSelect.value = selectedObjectId;
      await Promise.all([refreshFigure(), loadObjectDetails()]);
      setStatus('Ready');
    }

    objectSelect.addEventListener('change', () => {
      selectedObjectId = objectSelect.value;
      loadObjectDetails().catch((err) => log(String(err)));
    });

    document.getElementById('refresh').addEventListener('click', () => {
      refreshAll().catch((err) => { setStatus('Error'); log(String(err)); });
    });

    document.getElementById('applyEdit').addEventListener('click', async () => {
      const pathValue = document.getElementById('stylePath').value.trim();
      const rawValue = document.getElementById('styleValue').value.trim();
      if (!selectedObjectId || !pathValue || !rawValue) {
        setStatus('Select object, path and value');
        return;
      }
      let value = rawValue;
      try { value = JSON.parse(rawValue); } catch { /* keep string */ }
      try {
        const result = await rpc('apply_edit', { object_id: selectedObjectId, path: pathValue, value });
        if (result.ok) {
          setStatus('Edit applied');
          await Promise.all([refreshFigure(), loadObjectDetails()]);
        } else {
          setStatus('Rejected');
          log('rejected: ' + result.error);
        }
      } catch (err) {
        setStatus('Error');
        log(String(err));
      }
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
        // Pushed by the host after a language-model tool edit.
        refreshAll().catch((err) => log(String(err)));
      }
    });

    window.addEventListener('resize', () => {
      if (canvasEl.data) Plotly.Plots.resize(canvasEl);
    });

    refreshAll().catch((err) => { setStatus('Engine failed'); log(String(err)); });
  </script>
</body>
</html>`;
}
