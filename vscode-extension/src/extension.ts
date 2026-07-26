import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { EngineClient } from './engineClient';
import { InspectorViewProvider } from './inspectorView';
import { SceneObject, SceneTreeProvider } from './sceneTree';

let engine: EngineClient | undefined;
let currentPanel: vscode.WebviewPanel | undefined;
let selectedObjectId: string | undefined;
let sceneTree: SceneTreeProvider | undefined;
let inspector: InspectorViewProvider | undefined;
let engineOutput: vscode.OutputChannel | undefined;
let sceneDocEmitter: vscode.EventEmitter<vscode.Uri> | undefined;

// Read-only virtual documents generated from the scene (git is the history:
// the user saves these into their repo; the doc stays canonical).
const SCRIPT_URI = vscode.Uri.parse('magpylib-studio:/scene.py');
const SCENE_JSON_URI = vscode.Uri.parse('magpylib-studio:/scene.json');

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
  engineOutput ??= vscode.window.createOutputChannel('Magpylib Studio Engine');
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
    'Magpylib Studio',
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

function selectObjectInStudio(context: vscode.ExtensionContext, objectId: string): void {
  selectedObjectId = objectId;
  inspector?.select(objectId);
  if (currentPanel) {
    currentPanel.reveal(undefined, true); // keep focus in the sidebar
  } else {
    openStudioPanel(context);
  }
}

/** An edit happened somewhere (inspector, chat tool, tree action, panel):
 *  bring every surface back in sync. Debounced so a burst of edits (an LLM
 *  chaining tool calls, a slider drag) causes one redraw, not one each. */
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
function broadcastMutation(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
  }
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined;
    currentPanel?.webview.postMessage({ type: 'refresh' });
    sceneTree?.refresh();
    inspector?.refresh();
    sceneDocEmitter?.fire(SCRIPT_URI);
    sceneDocEmitter?.fire(SCENE_JSON_URI);
  }, 150);
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
  /** Mutating tool: same, but refresh all surfaces afterwards. A partially
   *  failed batch still changed the scene, so refresh regardless of ok. */
  const editTool = (toolName: string, method: string) =>
    vscode.lm.registerTool(toolName, {
      async invoke(options: vscode.LanguageModelToolInvocationOptions<object>) {
        const result = (await getEngine(context).request(
          method,
          options.input as Record<string, unknown>,
        )) as { ok: boolean; error?: string };
        broadcastMutation();
        return toolResult(result);
      },
    });
  context.subscriptions.push(
    queryTool('magpylib-studio_listObjects', 'list_objects'),
    queryTool('magpylib-studio_getSchema', 'get_schema'),
    queryTool('magpylib-studio_getField', 'get_field'),
    editTool('magpylib-studio_applyEdit', 'apply_edit'),
    editTool('magpylib-studio_addObject', 'add_object'),
    editTool('magpylib-studio_removeObject', 'remove_object'),
    editTool('magpylib-studio_setParam', 'set_param'),
    editTool('magpylib-studio_clearScene', 'clear_scene'),
    editTool('magpylib-studio_batch', 'batch'),
  );
}

export function activate(context: vscode.ExtensionContext): void {
  const tree = new SceneTreeProvider(async () => {
    try {
      return await getEngine(context).request<SceneObject[]>('list_objects');
    } catch (err) {
      engineOutput?.appendLine(`scene view: ${err instanceof Error ? err.message : err}`);
      return [];
    }
  });
  sceneTree = tree;

  inspector = new InspectorViewProvider(
    context.extensionUri,
    (method, params) => getEngine(context).request(method, params),
    () => {
      currentPanel?.webview.postMessage({ type: 'refresh' });
      tree.refresh(); // label edits change tree captions
    },
    () => selectedObjectId,
  );

  /** Run a mutating engine call from the tree UI, surface failures, refresh. */
  const mutateFromTree = async (method: string, params: Record<string, unknown>) => {
    try {
      const result = (await getEngine(context).request(method, params)) as {
        ok: boolean;
        error?: string;
      };
      if (!result.ok) {
        vscode.window.showErrorMessage(`Magpylib Studio: ${result.error}`);
      }
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: ${err instanceof Error ? err.message : err}`,
      );
    }
    broadcastMutation();
  };

  sceneDocEmitter = new vscode.EventEmitter<vscode.Uri>();
  const sceneDocProvider: vscode.TextDocumentContentProvider = {
    onDidChange: sceneDocEmitter.event,
    provideTextDocumentContent: async (uri) =>
      uri.path.endsWith('.py')
        ? getEngine(context).request<string>('to_script')
        : JSON.stringify(await getEngine(context).request('to_dict'), null, 2),
  };

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('magpylib-studio.sceneView', tree),
    vscode.window.registerWebviewViewProvider(InspectorViewProvider.viewId, inspector, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.workspace.registerTextDocumentContentProvider('magpylib-studio', sceneDocProvider),
    vscode.commands.registerCommand('magpylib-studio.viewScript', async () => {
      const doc = await vscode.workspace.openTextDocument(SCRIPT_URI);
      await vscode.window.showTextDocument(doc, {
        viewColumn: vscode.ViewColumn.Beside,
        preview: false,
      });
    }),
    vscode.commands.registerCommand('magpylib-studio.saveScene', async () => {
      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri;
      const target = await vscode.window.showSaveDialog({
        filters: { 'Python script': ['py'], 'Scene JSON': ['json'] },
        defaultUri: workspaceRoot && vscode.Uri.joinPath(workspaceRoot, 'scene.py'),
      });
      if (!target) {
        return;
      }
      const content = target.path.endsWith('.json')
        ? JSON.stringify(await getEngine(context).request('to_dict'), null, 2) + '\n'
        : (await getEngine(context).request<string>('to_script')) + '\n';
      await vscode.workspace.fs.writeFile(target, Buffer.from(content, 'utf8'));
      vscode.window.showInformationMessage(`Magpylib Studio: saved ${target.fsPath}`);
    }),
    vscode.commands.registerCommand('magpylib-studio.loadScene', async () => {
      const picks = await vscode.window.showOpenDialog({
        filters: { 'Scene JSON': ['json'] },
        canSelectMany: false,
      });
      if (!picks?.length) {
        return;
      }
      await mutateFromTree('load_scene', { scene: picks[0].fsPath });
      openStudioPanel(context);
    }),
    vscode.commands.registerCommand('magpylib-studio.openStudio', () =>
      openStudioPanel(context),
    ),
    vscode.commands.registerCommand('magpylib-studio.refreshScene', () =>
      broadcastMutation(),
    ),
    vscode.commands.registerCommand('magpylib-studio.loadExample', async () => {
      await mutateFromTree('load_example', {});
      openStudioPanel(context); // loading a scene should show it
    }),
    vscode.commands.registerCommand('magpylib-studio.selectObject', (objectId: string) =>
      selectObjectInStudio(context, objectId),
    ),
    vscode.commands.registerCommand('magpylib-studio.removeObject', (obj: SceneObject) =>
      mutateFromTree('remove_object', { object_id: obj.id }),
    ),
    vscode.commands.registerCommand('magpylib-studio.resetStyle', (obj: SceneObject) =>
      mutateFromTree('reset_style', { object_id: obj.id }),
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
  <title>Magpylib Studio</title>
  <style>
    html, body { margin: 0; height: 100%; font-family: var(--vscode-font-family, sans-serif); color: var(--vscode-foreground); }
    body { display: flex; flex-direction: column; }
    #canvas { flex: 1; min-height: 0; }
    #fieldCanvas { height: 32%; min-height: 0; border-top: 1px solid var(--vscode-panel-border, #444); }
    #fieldCanvas[hidden] { display: none; }
    #statusbar { display: flex; gap: 12px; align-items: center; padding: 2px 10px; font-size: 11px; opacity: 0.8; border-top: 1px solid var(--vscode-panel-border, #444); }
    #statusbar label { display: flex; gap: 4px; align-items: center; cursor: pointer; }
  </style>
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="canvas"></div>
  <div id="fieldCanvas" hidden></div>
  <div id="statusbar">
    <label><input type="checkbox" id="animate" /> Animate paths</label>
    <span id="status">Starting…</span>
  </div>
  <script nonce="${nonce}">
    // Selection and style editing live in the sidebar (Scene tree + Inspector);
    // this panel is only the live 3D view.
    const vscodeApi = acquireVsCodeApi();
    const statusEl = document.getElementById('status');
    const canvasEl = document.getElementById('canvas');
    const fieldEl = document.getElementById('fieldCanvas');
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

    async function refreshField() {
      try {
        const fig = await rpc('get_field_figure', { template: plotTemplate() });
        const layout = fig.layout || {};
        layout.uirevision = 'magpylib-field';
        layout.autosize = true;
        layout.margin = { l: 50, r: 10, t: 10, b: 35 };
        layout.paper_bgcolor = 'rgba(0,0,0,0)';
        layout.plot_bgcolor = 'rgba(0,0,0,0)';
        layout.legend = { orientation: 'h' };
        const wasHidden = fieldEl.hidden;
        fieldEl.hidden = false;
        await Plotly.react(fieldEl, { data: fig.data, layout, config: { responsive: true } });
        if (wasHidden && canvasEl.data) Plotly.Plots.resize(canvasEl);
      } catch {
        // No sources or no sensor in the scene - nothing to plot.
        if (!fieldEl.hidden) {
          fieldEl.hidden = true;
          if (canvasEl.data) Plotly.Plots.resize(canvasEl);
        }
      }
    }

    function refreshAllPlots() {
      return Promise.all([refreshFigure(), refreshField()]);
    }

    // Re-render when the user switches the VS Code color theme.
    new MutationObserver(() => {
      refreshAllPlots().catch((err) => { statusEl.textContent = String(err); });
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
        refreshAllPlots().catch((err) => { statusEl.textContent = String(err); });
      }
    });

    window.addEventListener('resize', () => {
      if (canvasEl.data) Plotly.Plots.resize(canvasEl);
      if (!fieldEl.hidden && fieldEl.data) Plotly.Plots.resize(fieldEl);
    });

    refreshAllPlots().catch((err) => { statusEl.textContent = 'Engine failed: ' + err; });
  </script>
</body>
</html>`;
}
