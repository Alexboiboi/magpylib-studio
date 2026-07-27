import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { EngineClient } from './engineClient';
import { HistoryEntry, HistoryTreeProvider } from './historyView';
import { InspectorViewProvider } from './inspectorView';
import { SceneObject, SceneTreeProvider } from './sceneTree';

let engine: EngineClient | undefined;
let currentPanel: vscode.WebviewPanel | undefined;
let fieldPanel: vscode.WebviewPanel | undefined;
let selectedObjectId: string | undefined;
let sceneTree: SceneTreeProvider | undefined;
let historyTree: HistoryTreeProvider | undefined;
let inspector: InspectorViewProvider | undefined;
let engineOutput: vscode.OutputChannel | undefined;
let sceneDocEmitter: vscode.EventEmitter<vscode.Uri> | undefined;

// Read-only virtual documents generated from the scene (git is the history:
// the user saves these into their repo; the doc stays canonical).
const SCRIPT_URI = vscode.Uri.parse('magpylib-studio:/scene.py');
const SCENE_JSON_URI = vscode.Uri.parse('magpylib-studio:/scene.json');

/** Object types offered by "Add Object…", with ready-to-build defaults. */
const OBJECT_TEMPLATES: {
  label: string;
  type: string;
  detail: string;
  params: Record<string, unknown>;
}[] = [
  {
    label: 'Cuboid magnet',
    type: 'magnet.Cuboid',
    detail: 'polarization (0,0,1) T, dimension 1×1×1 m',
    params: { polarization: [0, 0, 1], dimension: [1, 1, 1] },
  },
  {
    label: 'Cylinder magnet',
    type: 'magnet.Cylinder',
    detail: 'polarization (0,0,1) T, diameter 1 m, height 1 m',
    params: { polarization: [0, 0, 1], dimension: [1, 1] },
  },
  {
    label: 'Cylinder segment magnet',
    type: 'magnet.CylinderSegment',
    detail: 'r 1→2 m, height 1 m, 0°→90°',
    params: { polarization: [0, 0, 1], dimension: [1, 2, 1, 0, 90] },
  },
  {
    label: 'Sphere magnet',
    type: 'magnet.Sphere',
    detail: 'polarization (0,0,1) T, diameter 1 m',
    params: { polarization: [0, 0, 1], diameter: 1 },
  },
  {
    label: 'Tetrahedron magnet',
    type: 'magnet.Tetrahedron',
    detail: 'unit tetrahedron at the origin',
    params: {
      polarization: [0, 0, 1],
      vertices: [
        [0, 0, 0],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ],
    },
  },
  {
    label: 'Current loop',
    type: 'current.Circle',
    detail: '1000 A, diameter 2 m',
    params: { current: 1000, diameter: 2 },
  },
  {
    label: 'Current polyline',
    type: 'current.Polyline',
    detail: '1000 A along three points',
    params: {
      current: 1000,
      vertices: [
        [0, 0, 0],
        [1, 0, 0],
        [1, 1, 0],
      ],
    },
  },
  {
    label: 'Dipole',
    type: 'misc.Dipole',
    detail: 'moment (0,0,100) A·m²',
    params: { moment: [0, 0, 100] },
  },
  { label: 'Sensor', type: 'Sensor', detail: 'field probe at the origin', params: {} },
  { label: 'Collection', type: 'Collection', detail: 'empty group', params: {} },
];

/**
 * Ask whether a transform applies once or builds an animation path; for a
 * path, the number of steps and magpylib's `start` (passed through verbatim:
 * "auto" appends the new path, an index applies from there).
 */
async function askPathOrSingle(title: string): Promise<{ steps: number } | undefined> {
  const pick = await vscode.window.showQuickPick(
    [
      { label: 'Scalar', detail: 'apply once', path: false },
      { label: 'Path', detail: 'spread over an animation path', path: true },
    ],
    { placeHolder: `${title}: scalar or path?` },
  );
  if (!pick) {
    return undefined;
  }
  if (!pick.path) {
    return { steps: 1 };
  }
  const stepText = await vscode.window.showInputBox({
    prompt: 'Number of path steps',
    value: '20',
    validateInput: (v) =>
      Number.isInteger(Number(v)) && Number(v) >= 1
        ? undefined
        : 'A whole number of steps, 1 or more',
  });
  return stepText ? { steps: Number(stepText) } : undefined;
}

/**
 * Last step of a path transform: magpylib's `start`, passed through verbatim.
 * Returns {} for "auto" (magpylib's default) or {start: index}; undefined if
 * the user escaped.
 */
async function askStart(): Promise<{ start?: number } | undefined> {
  const pick = await vscode.window.showQuickPick(
    [
      {
        label: 'auto',
        detail:
          "magpylib default — the new path is appended after the object's current one",
        custom: false,
      },
      {
        label: 'index…',
        detail: 'apply from a path index instead (0 = first step, -1 = last step)',
        custom: true,
      },
    ],
    { placeHolder: 'start' },
  );
  if (!pick) {
    return undefined;
  }
  if (!pick.custom) {
    return {}; // omitted => engine passes "auto"
  }
  const indexText = await vscode.window.showInputBox({
    prompt: 'start — path index (negative counts from the end)',
    value: '0',
    validateInput: (v) => (Number.isInteger(Number(v)) ? undefined : 'A whole number'),
  });
  return indexText === undefined || indexText === ''
    ? undefined
    : { start: Number(indexText) };
}

/** Units shown in the Add Object prompts. */
const PARAM_UNITS: Record<string, string> = {
  polarization: ' (T), as Jx, Jy, Jz',
  dimension: ' (m) — Cuboid a,b,c · Cylinder d,h · Segment r1,r2,h,phi1,phi2',
  diameter: ' (m)',
  current: ' (A)',
  moment: ' (A·m²), as mx, my, mz',
  vertices: ' (m), x,y,z per point',
};

/** Rotation axis: a named axis or a free vector. */
async function askRotationAxis(): Promise<string | number[] | undefined> {
  const pick = await vscode.window.showQuickPick(
    [
      { label: 'x', detail: 'rotate about the x axis' },
      { label: 'y', detail: 'rotate about the y axis' },
      { label: 'z', detail: 'rotate about the z axis' },
      { label: 'Custom vector…', detail: 'any direction, e.g. 1, 1, 0' },
    ],
    { placeHolder: 'Rotation axis' },
  );
  if (!pick) {
    return undefined;
  }
  if (!pick.label.startsWith('Custom')) {
    return pick.label;
  }
  const text = await vscode.window.showInputBox({
    prompt: 'Axis direction as x, y, z',
    value: '1, 1, 0',
    validateInput: (v) =>
      parseVector(v, 3)?.some((n) => n !== 0)
        ? undefined
        : 'Three numbers, not all zero',
  });
  return text ? parseVector(text, 3) : undefined;
}

/** Rotation anchor: spin in place, orbit the origin, or orbit a point. */
async function askRotationAnchor(): Promise<
  { value: number | number[] | undefined } | undefined
> {
  const pick = await vscode.window.showQuickPick(
    [
      { label: 'Itself', detail: 'spin in place, position unchanged' },
      { label: 'Scene origin', detail: 'orbit (0, 0, 0)' },
      { label: 'Custom point…', detail: 'orbit any point' },
    ],
    { placeHolder: 'Rotate around…' },
  );
  if (!pick) {
    return undefined;
  }
  if (pick.label === 'Itself') {
    return { value: undefined };
  }
  if (pick.label === 'Scene origin') {
    return { value: 0 };
  }
  const text = await vscode.window.showInputBox({
    prompt: 'Anchor point as x, y, z (m)',
    value: '0, 0, 0',
    validateInput: (v) => (parseVector(v, 3) ? undefined : 'Three numbers, e.g. 0, 0, 1'),
  });
  const anchor = text && parseVector(text, 3);
  return anchor ? { value: anchor } : undefined;
}

/** Parse a free-form list of numbers ("1, 2 3"); undefined if none/invalid. */
function parseNumbers(text: string): number[] | undefined {
  const parts = text
    .replace(/[[\]]/g, ' ')
    .split(/[\s,]+/)
    .filter(Boolean)
    .map(Number);
  return parts.length && parts.every((n) => Number.isFinite(n)) ? parts : undefined;
}

/** Group a flat number list into rows of `width` (e.g. points into [x,y,z]). */
function reshape(flat: number[], width: number): number[][] {
  const rows: number[][] = [];
  for (let i = 0; i + width <= flat.length; i += width) {
    rows.push(flat.slice(i, i + width));
  }
  return rows;
}

/** Parse "1, 2, 3" / "1 2 3" into numbers; undefined if not `count` values. */
function parseVector(text: string, count: number): number[] | undefined {
  const parts = parseNumbers(text);
  return parts?.length === count ? parts : undefined;
}

/** Repo root in the dev layout (vscode-extension/ inside the repo). */
function repoRoot(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, '..');
}

let cachedPython: string | undefined;

/** First interpreter that can actually import the engine. A workspace .venv
 *  without magpylib-studio installed must not shadow a working one. */
function findPython(context: vscode.ExtensionContext): string | undefined {
  if (cachedPython) {
    return cachedPython;
  }
  const configured = vscode.workspace
    .getConfiguration('magpylib-studio')
    .get<string>('pythonPath');
  const candidates: string[] = configured ? [configured] : [];
  const venvs = (vscode.workspace.workspaceFolders ?? []).map((f) =>
    path.join(f.uri.fsPath, '.venv'),
  );
  venvs.push(path.join(repoRoot(context), '.venv'));
  for (const venv of venvs) {
    for (const python of [
      path.join(venv, 'bin', 'python'),
      path.join(venv, 'Scripts', 'python.exe'),
    ]) {
      if (fs.existsSync(python)) {
        candidates.push(python);
      }
    }
  }
  candidates.push('python3');
  for (const python of candidates) {
    const probe = spawnSync(python, ['-c', 'import magpylib_studio'], {
      timeout: 20000,
    });
    if (probe.status === 0) {
      cachedPython = python;
      return python;
    }
    engineOutput?.appendLine(
      `[skipping ${python}: cannot import magpylib_studio]`,
    );
  }
  return undefined;
}

function getEngine(context: vscode.ExtensionContext): EngineClient {
  if (engine?.isRunning) {
    return engine;
  }
  engineOutput ??= vscode.window.createOutputChannel('Magpylib Studio Engine');
  const pythonPath = findPython(context);
  if (!pythonPath) {
    vscode.window
      .showErrorMessage(
        'Magpylib Studio: no Python interpreter with the magpylib-studio ' +
          'engine found. Set "magpylib-studio.pythonPath" to an interpreter ' +
          'where the engine package is installed.',
        'Open Settings',
      )
      .then((choice) => {
        if (choice) {
          vscode.commands.executeCommand(
            'workbench.action.openSettings',
            'magpylib-studio.pythonPath',
          );
        }
      });
    throw new Error('no usable Python interpreter for the engine');
  }
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? repoRoot(context);
  engineOutput.appendLine(`[starting engine: ${pythonPath}]`);
  const client = new EngineClient(pythonPath, cwd);
  let stderrTail = '';
  client.onStderr = (text) => {
    engineOutput?.append(text);
    stderrTail = (stderrTail + text).slice(-400);
  };
  client.onExit = (code) => {
    engineOutput?.appendLine(`\n[engine exited with code ${code}]`);
    if (engine === client) {
      engine = undefined;
    }
    if (code !== 0) {
      cachedPython = undefined; // re-probe interpreters on the next attempt
      const lastLine = stderrTail.trim().split('\n').pop() ?? '';
      vscode.window
        .showErrorMessage(
          `Magpylib Studio engine crashed (exit ${code})` +
            (lastLine ? `: ${lastLine}` : ''),
          'Show Output',
        )
        .then((choice) => {
          if (choice) {
            engineOutput?.show();
          }
        });
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

/** The magpylib logo, shared by the activity bar and the panel tabs. */
function logoUri(context: vscode.ExtensionContext): vscode.Uri {
  return vscode.Uri.joinPath(context.extensionUri, 'media', 'magnet.svg');
}

/** Route webview 'rpcRequest' messages through the shared engine. */
function wireRpcRouter(context: vscode.ExtensionContext, webview: vscode.Webview): void {
  webview.onDidReceiveMessage(async (message) => {
    if (message.type !== 'rpcRequest') {
      return;
    }
    const { reqId, method, params } = message;
    try {
      const result = await getEngine(context).request(method, params);
      webview.postMessage({ type: 'rpcResult', reqId, method, result });
    } catch (err) {
      webview.postMessage({
        type: 'rpcError',
        reqId,
        method,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  });
}

function openStudioPanel(context: vscode.ExtensionContext): void {
  if (currentPanel) {
    currentPanel.reveal();
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    'magpylibStudio', // panel type id: keep, keybinding when-clauses match it
    'Magpylib Scene',
    vscode.ViewColumn.One,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [context.extensionUri],
    },
  );
  currentPanel = panel;
  panel.iconPath = logoUri(context); // tabs render icons in full colour
  panel.webview.html = createWebviewHtml(context, panel.webview);
  wireRpcRouter(context, panel.webview);
  panel.webview.onDidReceiveMessage((message) => {
    if (message.type === 'uiCommand') {
      vscode.commands.executeCommand(`magpylib-studio.${message.command}`);
    }
  });
  panel.onDidDispose(() => {
    currentPanel = undefined;
  });
}

function openFieldPanel(context: vscode.ExtensionContext): void {
  if (fieldPanel) {
    fieldPanel.reveal(undefined, true);
    return;
  }
  const panel = vscode.window.createWebviewPanel(
    'magpylibField',
    'Magpylib Field',
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [context.extensionUri],
    },
  );
  fieldPanel = panel;
  panel.iconPath = logoUri(context);
  panel.webview.html = createFieldViewHtml(context, panel.webview);
  wireRpcRouter(context, panel.webview);
  panel.onDidDispose(() => {
    fieldPanel = undefined;
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
    fieldPanel?.webview.postMessage({ type: 'refresh' });
    sceneTree?.refresh();
    historyTree?.refresh();
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
    editTool('magpylib-studio_rotate', 'rotate'),
    editTool('magpylib-studio_move', 'move'),
    editTool('magpylib-studio_setTransform', 'set_transform'),
    editTool('magpylib-studio_clearScene', 'clear_scene'),
    editTool('magpylib-studio_batch', 'batch'),
    editTool('magpylib-studio_undo', 'undo'),
  );
}

export function activate(context: vscode.ExtensionContext): void {
  const tree = new SceneTreeProvider(
    context.extensionUri,
    async () => {
      try {
        return await getEngine(context).request<SceneObject[]>('list_objects');
      } catch (err) {
        engineOutput?.appendLine(`scene view: ${err instanceof Error ? err.message : err}`);
        return [];
      }
    },
    (id, parent) => mutateFromTree('move_object', { object_id: id, parent }),
  );
  sceneTree = tree;

  const history = new HistoryTreeProvider(async () => {
    try {
      return await getEngine(context).request<{
        entries: HistoryEntry[];
        current: number;
      }>('get_history');
    } catch {
      return { entries: [], current: 0 };
    }
  });
  historyTree = history;

  inspector = new InspectorViewProvider(
    context.extensionUri,
    (method, params) => getEngine(context).request(method, params),
    () => {
      currentPanel?.webview.postMessage({ type: 'refresh' });
      tree.refresh(); // label edits change tree captions
    },
    () => selectedObjectId,
  );

  /** Undo/redo: refresh on success; a quiet status message when empty. */
  const undoRedo = async (method: 'undo' | 'redo') => {
    try {
      const result = (await getEngine(context).request(method)) as {
        ok: boolean;
        error?: string;
      };
      if (result.ok) {
        broadcastMutation();
      } else {
        vscode.window.setStatusBarMessage(`Magpylib Studio: ${result.error}`, 2000);
      }
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: ${err instanceof Error ? err.message : err}`,
      );
    }
  };

  /** Scene candidates captured by the last script import (one per show()
   *  call in the script, plus "all script objects" when that differs). */
  let importedScenes: string[] = [];

  const switchImportedScene = async () => {
    if (importedScenes.length < 2) {
      vscode.window.showInformationMessage(
        'Magpylib Studio: no alternative scenes from the last script import.',
      );
      return;
    }
    const pick = await vscode.window.showQuickPick(importedScenes, {
      placeHolder: 'Scene to load (one per show() call in the script)',
    });
    if (pick === undefined) {
      return;
    }
    await mutateFromTree('load_captured', { scene: importedScenes.indexOf(pick) });
    openStudioPanel(context);
  };

  /** Run a user script through the engine importer and show the result. */
  const importScript = async (uri: vscode.Uri) => {
    try {
      const result = (await getEngine(context).request('load_script', {
        path: uri.fsPath,
      })) as { ok: boolean; error?: string; warnings?: string[]; scenes?: string[] };
      if (!result.ok) {
        vscode.window.showErrorMessage(`Magpylib Studio import failed: ${result.error}`);
        return;
      }
      importedScenes = result.scenes ?? [];
      if (result.warnings?.length) {
        vscode.window.showWarningMessage(
          `Magpylib Studio import: ${result.warnings.join('; ')}`,
        );
      }
      broadcastMutation();
      openStudioPanel(context);
      if (importedScenes.length > 1) {
        const choice = await vscode.window.showInformationMessage(
          `Magpylib Studio: imported "${importedScenes[0]}" — the script has ${importedScenes.length} scene candidates.`,
          'Switch Scene…',
        );
        if (choice) {
          await switchImportedScene();
        }
      }
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: ${err instanceof Error ? err.message : err}`,
      );
    }
  };

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
    vscode.window.createTreeView('magpylib-studio.sceneView', {
      treeDataProvider: tree,
      dragAndDropController: tree,
    }),
    vscode.window.registerWebviewViewProvider(InspectorViewProvider.viewId, inspector, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.window.registerTreeDataProvider('magpylib-studio.historyView', history),
    vscode.commands.registerCommand(
      'magpylib-studio.gotoHistory',
      async (entry: HistoryEntry) => {
        const result = (await getEngine(context).request('goto_history', {
          index: entry.index,
        })) as { ok: boolean; error?: string };
        if (!result.ok) {
          vscode.window.showErrorMessage(`Magpylib Studio: ${result.error}`);
        }
        broadcastMutation();
      },
    ),
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
    vscode.commands.registerCommand('magpylib-studio.importScript', async () => {
      const picks = await vscode.window.showOpenDialog({
        filters: { 'Python script': ['py'] },
        canSelectMany: false,
      });
      if (picks?.length) {
        await importScript(picks[0]);
      }
    }),
    vscode.commands.registerCommand(
      'magpylib-studio.openScriptInStudio',
      async (uri?: vscode.Uri) => {
        const target = uri ?? vscode.window.activeTextEditor?.document.uri;
        if (target) {
          await importScript(target);
        }
      },
    ),
    vscode.commands.registerCommand('magpylib-studio.switchScene', switchImportedScene),
    vscode.commands.registerCommand('magpylib-studio.openStudio', () =>
      openStudioPanel(context),
    ),
    vscode.commands.registerCommand('magpylib-studio.openFieldView', () =>
      openFieldPanel(context),
    ),
    vscode.commands.registerCommand('magpylib-studio.undo', () => undoRedo('undo')),
    vscode.commands.registerCommand('magpylib-studio.redo', () => undoRedo('redo')),
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
    vscode.commands.registerCommand('magpylib-studio.moveTo', async (obj: SceneObject) => {
      const objects = await getEngine(context).request<SceneObject[]>('list_objects');
      const subtree = new Set([obj.id]); // no moving into itself/descendants
      for (let grew = true; grew; ) {
        grew = false;
        for (const o of objects) {
          if (o.parent && subtree.has(o.parent) && !subtree.has(o.id)) {
            subtree.add(o.id);
            grew = true;
          }
        }
      }
      const targets: { label: string; parent: string | null }[] = [];
      if (obj.parent !== null) {
        targets.push({ label: '(scene root)', parent: null });
      }
      for (const o of objects) {
        if (o.type === 'Collection' && !subtree.has(o.id) && o.id !== obj.parent) {
          targets.push({ label: `${o.label} (${o.id})`, parent: o.id });
        }
      }
      if (!targets.length) {
        vscode.window.showInformationMessage('Magpylib Studio: nowhere to move this to.');
        return;
      }
      const pick = await vscode.window.showQuickPick(
        targets.map((t) => t.label),
        { placeHolder: `Move "${obj.label}" to…` },
      );
      if (pick === undefined) {
        return;
      }
      const parent = targets.find((t) => t.label === pick)?.parent ?? null;
      await mutateFromTree('move_object', { object_id: obj.id, parent });
    }),
    vscode.commands.registerCommand(
      'magpylib-studio.addObject',
      async (obj?: SceneObject) => {
        const pick = await vscode.window.showQuickPick(
          OBJECT_TEMPLATES.map((t) => ({ label: t.label, detail: t.detail, t })),
          { placeHolder: 'Object to add' },
        );
        if (!pick) {
          return;
        }
        const suggestion = pick.t.type.split('.').pop()!.toLowerCase();
        const id = await vscode.window.showInputBox({
          prompt: `Id for the new ${pick.label.toLowerCase()}`,
          value: suggestion,
          validateInput: (v) =>
            /^[A-Za-z_]\w*$/.test(v)
              ? undefined
              : 'Letters, digits, underscores; must not start with a digit.',
        });
        if (!id) {
          return;
        }
        // Let the user set each parameter, prefilled with the default.
        const values: Record<string, unknown> = { ...pick.t.params };
        for (const [name, def] of Object.entries(pick.t.params)) {
          const isScalar = typeof def === 'number';
          const flat = isScalar ? String(def) : JSON.stringify(def);
          const text = await vscode.window.showInputBox({
            prompt: `${pick.label} — ${name}${PARAM_UNITS[name] ?? ''}`,
            value: isScalar ? flat : flat.replace(/[[\]]/g, (m) => (m === '[' ? '' : '')),
            validateInput: (v) => {
              if (isScalar) {
                return Number.isFinite(Number(v)) ? undefined : 'A number';
              }
              return parseNumbers(v) ? undefined : 'Numbers, e.g. 0, 0, 1';
            },
          });
          if (text === undefined) {
            return; // escaped: abandon the whole creation
          }
          if (isScalar) {
            values[name] = Number(text);
          } else {
            const flatNums = parseNumbers(text)!;
            const template = def as number[] | number[][];
            values[name] = Array.isArray(template[0])
              ? reshape(flatNums, (template[0] as number[]).length)
              : flatNums;
          }
        }
        const params: Record<string, unknown> = {
          object_id: id,
          type: pick.t.type,
          params: values,
          style: { label: pick.label },
        };
        if (obj?.type === 'Collection') {
          params.parent = obj.id; // right-clicked a group: create inside it
        }
        await mutateFromTree('add_object', params);
        selectObjectInStudio(context, id); // show it in the Inspector
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.setPosition',
      async (obj: SceneObject) => {
        const current = (await getEngine(context).request('get_transform', {
          object_id: obj.id,
        })) as { position: number[] };
        const text = await vscode.window.showInputBox({
          prompt: `Position of "${obj.label}" as x, y, z (m)`,
          value: current.position.join(', '),
          validateInput: (v) =>
            parseVector(v, 3) ? undefined : 'Three numbers, e.g. 0, 0, 1.5',
        });
        const position = text && parseVector(text, 3);
        if (position) {
          await mutateFromTree('set_transform', { object_id: obj.id, position });
        }
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.moveBy',
      async (obj: SceneObject) => {
        const kind = await askPathOrSingle(`Move "${obj.label}"`);
        if (!kind) {
          return;
        }
        const text = await vscode.window.showInputBox({
          prompt:
            kind.steps === 1
              ? 'Displacement dx, dy, dz (m)'
              : `Total displacement dx, dy, dz (m) — spread over ${kind.steps} steps`,
          value: '0, 0, 1',
          validateInput: (v) =>
            parseVector(v, 3) ? undefined : 'Three numbers, e.g. 0, 0, 1',
        });
        const d = text && parseVector(text, 3);
        if (!d) {
          return;
        }
        const displacement =
          kind.steps === 1
            ? d
            : Array.from({ length: kind.steps }, (_, i) =>
                d.map((c) => (c * (i + 1)) / kind.steps),
              );
        let startArg: { start?: number } = {};
        if (kind.steps > 1) {
          const chosen = await askStart();
          if (!chosen) {
            return;
          }
          startArg = chosen;
        }
        await mutateFromTree('move', {
          object_id: obj.id,
          displacement,
          ...startArg,
        });
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.rotateBy',
      async (obj: SceneObject) => {
        const kind = await askPathOrSingle(`Rotate "${obj.label}"`);
        if (!kind) {
          return;
        }
        const axis = await askRotationAxis();
        if (axis === undefined) {
          return;
        }
        const anchor = await askRotationAnchor();
        if (anchor === undefined) {
          return;
        }
        const text = await vscode.window.showInputBox({
          prompt:
            kind.steps === 1
              ? 'Angle in degrees'
              : `Total angle in degrees — spread over ${kind.steps} steps (360 = full turn)`,
          value: kind.steps === 1 ? '45' : '360',
          validateInput: (v) =>
            Number.isFinite(Number(v)) && v.trim() ? undefined : 'A number, e.g. 45',
        });
        if (text === undefined || !text.trim()) {
          return;
        }
        const total = Number(text);
        const angle =
          kind.steps === 1
            ? total
            : Array.from(
                { length: kind.steps },
                (_, i) => (total * (i + 1)) / kind.steps,
              );
        let startArg: { start?: number } = {};
        if (kind.steps > 1) {
          const chosen = await askStart();
          if (!chosen) {
            return;
          }
          startArg = chosen;
        }
        await mutateFromTree('rotate', {
          object_id: obj.id,
          angle,
          axis,
          ...(anchor.value !== undefined ? { anchor: anchor.value } : {}),
          ...startArg,
        });
      },
    ),
    vscode.commands.registerCommand('magpylib-studio.clearPath', (obj: SceneObject) =>
      mutateFromTree('clear_path', { object_id: obj.id }),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.newCollection',
      async (obj?: SceneObject) => {
        const id = await vscode.window.showInputBox({
          prompt: 'Id for the new collection',
          validateInput: (v) =>
            /^[A-Za-z_]\w*$/.test(v)
              ? undefined
              : 'Letters, digits, underscores; must not start with a digit.',
        });
        if (!id) {
          return;
        }
        const params: Record<string, unknown> = { object_id: id, type: 'Collection' };
        if (obj?.type === 'Collection') {
          params.parent = obj.id; // context-menu on a collection: create inside
        }
        await mutateFromTree('add_object', params);
      },
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
    #statusbar { display: flex; gap: 12px; align-items: center; padding: 2px 10px; font-size: 11px; opacity: 0.8; border-top: 1px solid var(--vscode-panel-border, #444); }
    #statusbar label { display: flex; gap: 4px; align-items: center; cursor: pointer; }
    #statusbar button { display: inline-flex; align-items: center; justify-content: center; background: none; border: none; color: inherit; cursor: pointer; padding: 2px; border-radius: 3px; opacity: 0.85; }
    #statusbar button:hover { background: var(--vscode-toolbar-hoverBackground, rgba(128,128,128,0.2)); opacity: 1; }
    #statusbar button svg { display: block; }
  </style>
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="canvas"></div>
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
  </script>
</body>
</html>`;
}

function createFieldViewHtml(
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
  <title>Magpylib Field</title>
  <style>
    html, body { margin: 0; height: 100%; font-family: var(--vscode-font-family, sans-serif); color: var(--vscode-foreground); }
    body { display: flex; flex-direction: column; }
    #canvas { flex: 1; min-height: 0; }
    #statusbar { display: flex; gap: 12px; align-items: center; padding: 2px 10px; font-size: 11px; opacity: 0.8; border-top: 1px solid var(--vscode-panel-border, #444); }
    #statusbar label { display: flex; gap: 4px; align-items: center; cursor: pointer; }
    select { background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); font-size: 11px; }
  </style>
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="canvas"></div>
  <div id="statusbar">
    <label>Output
      <select id="output">
        <option>B</option><option>Bx</option><option>By</option><option>Bz</option>
        <option>Bxy</option>
        <option>H</option><option>Hx</option><option>Hy</option><option>Hz</option>
      </select>
    </label>
    <label><input type="checkbox" id="animate" /> Animate path</label>
    <span id="status">Loading…</span>
  </div>
  <script nonce="${nonce}">
    // Magpylib-rendered 2D field plot (show(output=...)): field at the
    // scene's sensors along their paths. Opened on demand from the Studio.
    const vscodeApi = acquireVsCodeApi();
    const statusEl = document.getElementById('status');
    const canvasEl = document.getElementById('canvas');
    const outputEl = document.getElementById('output');
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
      const cls = document.body.className;
      const dark = /vscode-dark|vscode-high-contrast/.test(cls)
        && !cls.includes('vscode-high-contrast-light');
      return dark ? 'plotly_dark' : 'plotly_white';
    }

    async function refreshField() {
      try {
        const fig = await rpc('get_field_figure', {
          output: outputEl.value,
          animation: animateEl.checked,
          template: plotTemplate(),
        });
        const layout = fig.layout || {};
        layout.uirevision = 'magpylib-field';
        layout.autosize = true;
        layout.margin = { l: 55, r: 15, t: 15, b: 40 };
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
        statusEl.textContent = 'No field to plot - the scene needs a source and a sensor. (' + err + ')';
      }
    }

    outputEl.addEventListener('change', refreshField);
    animateEl.addEventListener('change', refreshField);

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
      } else if (message.type === 'refresh') {
        refreshField();
      }
    });

    window.addEventListener('resize', () => {
      if (canvasEl.data) Plotly.Plots.resize(canvasEl);
    });

    refreshField();
  </script>
</body>
</html>`;
}
