import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { PythonExtension } from '@vscode/python-extension';
import { EngineClient } from './engineClient';
import { HistoryEntry, HistoryTreeProvider } from './historyView';
import { mediaUri, nonce as webviewNonce } from './webview';
import { Variable, VariablesViewProvider } from './variablesView';
import { InspectorViewProvider } from './inspectorView';
import {
  isOperation,
  SceneNode,
  SceneObject,
  SceneOperation,
  SceneTreeProvider,
} from './sceneTree';

let engine: EngineClient | undefined;
let currentPanel: vscode.WebviewPanel | undefined;
let fieldPanel: vscode.WebviewPanel | undefined;
let selectedObjectId: string | undefined;
let sceneTree: SceneTreeProvider | undefined;
let sceneTreeView: vscode.TreeView<SceneNode> | undefined;
let clipboard: { id: string; cut: boolean } | undefined;
let historyTree: HistoryTreeProvider | undefined;
let variablesTree: VariablesViewProvider | undefined;
let inspector: InspectorViewProvider | undefined;
let engineOutput: vscode.OutputChannel | undefined;
let sceneDocEmitter: vscode.EventEmitter<vscode.Uri> | undefined;

// Read-only virtual document generated from the scene (git is the history:
// the user saves it into their repo; the doc stays canonical).
const SCENE_JSON_URI = vscode.Uri.parse('magpylib-studio:/scene.json');

// The script tab, unlike scene.json, is editable and applied back on save, so
// it is a real file (a content provider has no write side) kept in extension
// storage — scratch space, not something to litter the user's workspace with.
// Being a real file, VS Code restores its tab across a window reload, which is
// why the path is fixed at activation and the restored tab re-rendered: see
// adoptRestoredScriptTab. (That is also why the extension activates on
// startup — a tab it owns is on screen before the user asks for anything.)
let scriptFile: vscode.Uri | undefined;
/** Re-render the script tab from the scene; set during activation. */
let refreshScript: (() => void) | undefined;
/** The tab holds text the engine rejected: leave it alone until it applies. */
let scriptRejected = false;
/** What we last put in that file — the scene changes far more often than its
 *  script does (a style edit renders identically), and rewriting it on every
 *  mutation would reload the editor under the user for nothing. */
let scriptOnDisk: string | undefined;
/** Why the script tab was last saved, from onWillSave — auto-save on a typing
 *  delay must not run half-written code through the engine. */
let scriptSaveReason: vscode.TextDocumentSaveReason | undefined;

/** The file this scene is saved to and from, and whether it has changed since.
 *  The engine holds one scene with no name of its own, so the name lives here:
 *  it is what Save saves to, what the view title shows, and what is reopened
 *  next time this workspace is. */
let sceneFile: vscode.Uri | undefined;
let sceneDirty = false;
/** Written beside the script tab whenever the scene changes, so a crash or a
 *  reload is recoverable — the scene is otherwise only in the subprocess.
 *  Set during activation, like refreshScript. */
let sceneBackupFile: vscode.Uri | undefined;
let writeSceneBackup: (() => Promise<void>) | undefined;
let rememberSceneState: (() => Thenable<void>) | undefined;
let backupTimer: ReturnType<typeof setTimeout> | undefined;
/** Remembered per workspace: the file to reopen, and whether what was in the
 *  editor differed from it. */
const SCENE_STATE_KEY = 'magpylib-studio.scene';
/** Remembered globally (not per workspace): whether the getting-started
 *  walkthrough has already been shown, so it opens once per install. */
const TOUR_SHOWN_KEY = 'magpylib-studio.tourShown';
/** The extension VS Code associates with the studio, and what Save proposes.
 *  Doubled rather than a bare `.magpy`: the file stays JSON to git, to schema
 *  validation and to every editor, while still being a name we can claim. */
const SCENE_EXTENSION = '.magpy.json';

/** What a command that replaces the scene should do about unsaved changes.
 *  Absent means "ask", which is what a person picking the menu item wants;
 *  a caller that is not a person says so. */
type Discard = { discardChanges?: boolean };

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
    detail: 'polarization (0,0,1) T, sides 1×1×1 m',
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
    detail: 'polarization (0,0,1) T, radii 1→2 m, height 1 m, 0°→90°',
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
    detail: 'polarization (0,0,1) T, unit tetrahedron',
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
    detail: '1000 A, 3-point open path',
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
  { label: 'Sensor', type: 'Sensor', detail: 'field probe, single pixel', params: {} },
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

/** Calls whose params carry values a user typed, so may name new variables. */
const MUTATING_WITH_VALUES = new Set([
  'set_param',
  'set_transform',
  'move',
  'rotate',
  'add_object',
  'duplicate_around',
  // a step's own values are typed the same way, so naming a variable in one
  // has to create it the same way too
  'edit_event',
]);

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
async function askRotationAxis(): Promise<string | (number | string)[] | undefined> {
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
  { value: number | (number | string)[] | undefined } | undefined
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

/**
 * One typed field -> a document value: a number where it is one, otherwise an
 * expression over the scene's variables. The `=` marker the document uses is
 * added here, so users type `gap*2` and never learn the notation.
 */
function asDocumentValue(text: string): number | string {
  const trimmed = text.trim();
  const asNumber = Number(trimmed);
  return Number.isFinite(asNumber) && trimmed !== '' ? asNumber : `=${trimmed}`;
}

/**
 * Comma/space separated numbers *or* expressions, e.g. `0, 0, gap`. Bracket
 * characters are stripped as in parseNumbers, but an expression may itself
 * contain commas inside parentheses (`0, 0, max(a, b)`), so splitting only
 * happens at depth zero.
 */
function parseTerms(text: string): (number | string)[] | undefined {
  const terms: string[] = [];
  let depth = 0;
  let current = '';
  for (const ch of text.replace(/[[\]]/g, ' ')) {
    if (ch === '(') depth++;
    if (ch === ')') depth--;
    if (depth < 0) return undefined;
    if (depth === 0 && (ch === ',' || /\s/.test(ch))) {
      if (current.trim()) terms.push(current.trim());
      current = '';
    } else {
      current += ch;
    }
  }
  if (current.trim()) terms.push(current.trim());
  return depth === 0 && terms.length ? terms.map(asDocumentValue) : undefined;
}

/**
 * "0, 10" / "0," / ", 10" -> [min, max] with null for an open end; undefined
 * if it is not a pair at all. An empty side is "no limit here", which is not
 * the same as no limits.
 */
function parseBoundPair(text: string): [number | null, number | null] | undefined {
  const parts = text.split(',');
  if (parts.length !== 2) {
    return undefined;
  }
  const ends = parts.map((part) => {
    const trimmed = part.trim();
    if (!trimmed) {
      return null;
    }
    const value = Number(trimmed);
    return Number.isFinite(value) ? value : undefined;
  });
  return ends.some((end) => end === undefined)
    ? undefined
    : (ends as [number | null, number | null]);
}

/** Group a flat list into rows of `width` (e.g. points into [x,y,z]). */
function reshape<T>(flat: T[], width: number): T[][] {
  const rows: T[][] = [];
  for (let i = 0; i + width <= flat.length; i += width) {
    rows.push(flat.slice(i, i + width));
  }
  return rows;
}

/** Parse "1, 2, gap" into `count` numbers-or-expressions, else undefined. */
function parseVector(text: string, count: number): (number | string)[] | undefined {
  const parts = parseTerms(text);
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

/** Last resort only, when neither ms-python.python nor uv found anything
 *  usable: a python3 command able to actually bootstrap a venv, resolved
 *  the way a real terminal would rather than however GUI-launched VS
 *  Code's own PATH happens to look. On macOS/Linux a GUI launch does not
 *  source ~/.zprofile or
 *  ~/.zshrc, so a plain PATH lookup finds macOS's bundled /usr/bin/python3
 *  (3.9, an SSL stack too old to fetch anything from PyPI) instead of
 *  whatever the user actually has via Homebrew/pyenv/etc. — a login shell
 *  resolves it the way Terminal.app would. Windows does not have that
 *  failure mode (its PATH is a persistent env var GUI processes inherit
 *  correctly) but frequently has no `python3` at all, only `python` or the
 *  `py` launcher, so this branches instead of shelling out to a login
 *  shell that would not help. Returns a command *prefix* (`py` needs `-3`
 *  before anything else) rather than a single path. */
function resolvePythonCommand(): string[] {
  if (process.platform === 'win32') {
    for (const candidate of [['python'], ['py', '-3']]) {
      const probe = spawnSync(candidate[0], [...candidate.slice(1), '--version'], {
        timeout: 10000,
      });
      if (probe.status === 0) {
        return candidate;
      }
    }
    return ['python'];
  }
  const shell = process.env.SHELL || '/bin/zsh';
  const result = spawnSync(shell, ['-lc', 'command -v python3'], {
    timeout: 10000,
  });
  const resolved = result.status === 0 ? result.stdout.toString().trim() : '';
  return [resolved || 'python3'];
}

/** Kept in sync with pyproject.toml's `requires-python`. A found interpreter
 *  that does not meet this is not a maybe — pip will refuse it with a
 *  message ("no matching distribution") that reads like a network failure,
 *  not a version one, so this is checked explicitly instead of trusting
 *  pip's error text to explain itself. */
const PYTHON_FLOOR = '3.11';

function pythonInstallTip(): string {
  switch (process.platform) {
    case 'darwin':
      return '"brew install python3", or python.org';
    case 'win32':
      return 'python.org, or the Microsoft Store';
    default:
      return 'your package manager (e.g. "apt install python3.11"), or python.org';
  }
}

function checkPythonVersion(pythonExe: string): { ok: boolean; version?: string } {
  const probe = spawnSync(
    pythonExe,
    ['-c', 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")'],
    { timeout: 10000 },
  );
  if (probe.status !== 0) {
    return { ok: false };
  }
  const version = probe.stdout.toString().trim();
  const [major, minor] = version.split('.').map(Number);
  const [floorMajor, floorMinor] = PYTHON_FLOOR.split('.').map(Number);
  const ok = major > floorMajor || (major === floorMajor && minor >= floorMinor);
  return { ok, version };
}

/** The "no interpreter found" error's one-click fix, tried in order:
 *
 *  1. Whatever ms-python.python already has resolved for this workspace
 *     (the same interpreter its status bar shows) — that extension already
 *     solved cross-platform interpreter discovery; reinventing it with our
 *     own PATH probing is exactly how this feature's first version shipped
 *     with a bug (bare `python3` resolving to whatever a GUI-launched VS
 *     Code's minimal PATH happens to contain).
 *  2. `uv`, if installed — it fetches a matching Python itself on demand,
 *     so unlike PATH probing it does not depend on anything already being
 *     installed, and it is what this very repo's own setup already uses.
 *  3. A login-shell-resolved `python3` (or `python`/`py` on Windows) as a
 *     last resort, version-checked before use rather than trusted blindly —
 *     a fresh machine can genuinely have nothing newer than an OS-bundled
 *     Python outside of tool-managed venvs, which is a real state to
 *     report clearly rather than let pip's own error text stand in for.
 *
 *  getEngine() has ~40 call sites, many of which can fire near-simultaneously
 *  on activation, so more than one "no interpreter" dialog can be on screen
 *  at once — this guards against two clicks racing two installs into the
 *  same venv by sharing one in-flight run instead of starting a second. */
let installEnginePromise: Promise<void> | undefined;
async function installEngine(): Promise<void> {
  if (installEnginePromise) {
    return installEnginePromise;
  }
  installEnginePromise = installEngineNow().finally(() => {
    installEnginePromise = undefined;
  });
  return installEnginePromise;
}

async function installEngineNow(): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showErrorMessage(
      'Magpylib Studio: open a folder first, so the engine has somewhere ' +
        'to install to.',
    );
    return;
  }

  let pythonExe: string | undefined;
  try {
    const pythonApi = await PythonExtension.api();
    const active = pythonApi.environments.getActiveEnvironmentPath(folder.uri);
    const resolved = await pythonApi.environments.resolveEnvironment(active);
    const candidate = resolved?.executable.uri?.fsPath;
    if (candidate && checkPythonVersion(candidate).ok) {
      pythonExe = candidate;
    }
  } catch {
    // ms-python.python not installed, or nothing suitable resolved — fall through
  }
  const usingExisting = Boolean(pythonExe);

  const venvDir = path.join(folder.uri.fsPath, '.venv');
  const venvPython =
    process.platform === 'win32'
      ? path.join(venvDir, 'Scripts', 'python.exe')
      : path.join(venvDir, 'bin', 'python');
  pythonExe ??= venvPython;

  try {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: 'Magpylib Studio: installing the engine',
        cancellable: false,
      },
      async (progress) => {
        const hasUv =
          !usingExisting &&
          spawnSync('uv', ['--version'], { timeout: 10000 }).status === 0;
        if (hasUv) {
          progress.report({ message: `fetching Python ${PYTHON_FLOOR} via uv…` });
          const venvResult = spawnSync(
            'uv',
            // --seed: uv venv omits pip by default (it expects `uv pip`
            // instead), which would otherwise make the shared `python -m
            // pip install` step below fail with "No module named pip".
            ['venv', '--python', PYTHON_FLOOR, '--seed', venvDir],
            { timeout: 120000 },
          );
          if (venvResult.status !== 0) {
            throw new Error(
              venvResult.stderr?.toString().trim() || 'uv venv failed',
            );
          }
        } else if (!usingExisting) {
          progress.report({ message: 'creating .venv…' });
          const [pythonCmd, ...pythonPrefixArgs] = resolvePythonCommand();
          const venvResult = spawnSync(
            pythonCmd,
            [...pythonPrefixArgs, '-m', 'venv', venvDir],
            { timeout: 60000 },
          );
          if (venvResult.status !== 0) {
            throw new Error(
              venvResult.stderr?.toString().trim() ||
                venvResult.error?.message ||
                'could not create a virtual environment',
            );
          }
          const check = checkPythonVersion(venvPython);
          if (!check.ok) {
            throw new Error(
              `found Python ${check.version ?? '(unknown)'}, but magpylib-studio ` +
                `needs ${PYTHON_FLOOR} or newer. Install a newer Python ` +
                `(${pythonInstallTip()}) and try again — or install uv ` +
                '(astral.sh/uv), which this button will use automatically.',
            );
          }
        }
        progress.report({ message: 'pip install magpylib-studio…' });
        const pipResult = spawnSync(
          pythonExe!,
          ['-m', 'pip', 'install', 'magpylib-studio'],
          { timeout: 180000 },
        );
        if (pipResult.status !== 0) {
          throw new Error(
            pipResult.stderr?.toString().trim() || 'pip install failed',
          );
        }
      },
    );
  } catch (err) {
    vscode.window.showErrorMessage(
      `Magpylib Studio: could not install the engine — ` +
        `${err instanceof Error ? err.message : err}`,
    );
    return;
  }

  await vscode.workspace
    .getConfiguration('magpylib-studio')
    .update('pythonPath', pythonExe, vscode.ConfigurationTarget.Workspace);
  cachedPython = undefined; // re-probe: the freshly configured path wins next
  refreshSurfaces();

  vscode.window
    .showInformationMessage('Magpylib Studio: engine installed.', 'Open Scene View')
    .then((choice) => {
      if (choice) {
        void vscode.commands.executeCommand('magpylib-studio.openStudio');
      }
    });
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
          'where the engine package is installed, or install it now.',
        'Install the Engine',
        'Open Settings',
      )
      .then((choice) => {
        if (choice === 'Install the Engine') {
          void installEngine();
        } else if (choice === 'Open Settings') {
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

/** The tree item a keyboard shortcut should act on (menus pass it directly).
 *  Steps are selectable too, but the object shortcuts do not apply to them. */
function treeSelection(): SceneObject | undefined {
  const selected = sceneTreeView?.selection[0];
  return selected && !isOperation(selected) ? selected : undefined;
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
      // The exception the webview guide allows: what would be lost is the
      // camera the user has just spent time positioning, and a plotly scene
      // is expensive to rebuild. getState/setState cannot cheaply carry it.
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
      // The exception the webview guide allows: what would be lost is the
      // camera the user has just spent time positioning, and a plotly scene
      // is expensive to rebuild. getState/setState cannot cheaply carry it.
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

/** Show which file the scene is, and whether it has unsaved changes.
 *
 * The tree view's description is the only title bar the studio has — there is
 * no editor tab to carry the name and the dirty dot, so the "•" convention is
 * borrowed rather than invented.
 */
function showSceneFile(): void {
  if (sceneTreeView) {
    const name = sceneFile ? basename(sceneFile) : 'Untitled';
    sceneTreeView.description = sceneDirty ? `${name} •` : name;
  }
  void vscode.commands.executeCommand(
    'setContext',
    'magpylib-studio.sceneFile',
    sceneFile !== undefined,
  );
  void vscode.commands.executeCommand('setContext', 'magpylib-studio.sceneDirty', sceneDirty);
}

function basename(uri: vscode.Uri): string {
  return uri.path.split('/').pop() || uri.path;
}

/** Keep the crash backup roughly current without writing on every keystroke.
 *  Slower than the redraw debounce on purpose: a redraw has to feel instant,
 *  a backup only has to beat the next crash. */
function scheduleBackup(): void {
  if (backupTimer) {
    clearTimeout(backupTimer);
  }
  backupTimer = setTimeout(() => {
    backupTimer = undefined;
    void writeSceneBackup?.();
  }, 1000);
}

/** Bring every surface back in sync with the engine. Debounced so a burst
 *  (an LLM chaining tool calls, a slider drag) causes one redraw, not one
 *  each. Says nothing about the scene having *changed* — see
 *  broadcastMutation; redrawing and editing are not the same event, and
 *  conflating them would put an unsaved-changes mark on a Refresh. */
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
function refreshSurfaces(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
  }
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined;
    currentPanel?.webview.postMessage({ type: 'refresh' });
    fieldPanel?.webview.postMessage({ type: 'refresh' });
    sceneTree?.refresh();
    historyTree?.refresh();
    variablesTree?.refresh();
    inspector?.refresh();
    refreshScript?.();
    sceneDocEmitter?.fire(SCENE_JSON_URI);
  }, 150);
}

/** An edit happened somewhere (inspector, chat tool, tree action, panel):
 *  the document now differs from its file, and every surface is stale. */
function broadcastMutation(): void {
  // Every path that changes the scene ends up here, which makes it the one
  // place that can honestly say the scene no longer matches its file. Set
  // outside the debounce: a caller that saves right after mutating (or that
  // marks the scene clean, like opening a file) must see it immediately.
  if (!sceneDirty) {
    sceneDirty = true;
    showSceneFile();
    // Recorded now rather than with the backup a second later: if the window
    // goes away in between, "there were unsaved changes" is the fact that
    // matters, and it is better to offer a backup one edit stale than to
    // reopen the saved file as though nothing had happened.
    void rememberSceneState?.();
  }
  scheduleBackup();
  refreshSurfaces();
}

function toolResult(payload: unknown): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([
    new vscode.LanguageModelTextPart(JSON.stringify(payload)),
  ]);
}

/** What a tool is about to do, in the words of the thing it will do it to. */
function invocationMessage(method: string, input: Record<string, unknown>): string {
  const id = (input.object_id ?? input.event_id ?? input.name) as string | undefined;
  const target = id ? ` ${id}` : '';
  const said: Record<string, string> = {
    add_object: `Adding ${(input.type as string) ?? 'an object'}${target}`,
    remove_object: `Removing${target}`,
    remove_event: `Removing step${target}`,
    set_param: `Setting ${(input.name as string) ?? 'a parameter'} on${target}`,
    apply_edit: `Styling${target}`,
    move: `Moving${target}`,
    rotate: `Rotating${target}`,
    set_transform: `Placing${target}`,
    duplicate_around: `Patterning${target} about an axis`,
    duplicate_along: `Patterning${target} along a direction`,
    mirror: `Mirroring${target}`,
    set_variable: `Setting${target}`,
    set_variable_bounds: `Bounding${target}`,
    edit_event: `Editing step${target}`,
    move_event: `Reordering step${target}`,
    clear_scene: 'Clearing the scene',
    undo: 'Undoing the last change',
    batch: `Applying ${(input.operations as unknown[])?.length ?? 0} changes`,
  };
  return said[method] ?? `Running ${method}`;
}

/**
 * The tools that cannot be shrugged off if the model gets them wrong, with
 * what the user should be told before agreeing. The guide's point is that a
 * confirmation naming nothing in particular is one people click through.
 */
function confirmation(
  method: string,
  input: Record<string, unknown>,
): { title: string; message: vscode.MarkdownString } | undefined {
  const id = (input.object_id ?? input.event_id) as string | undefined;
  const text = {
    clear_scene: ['Clear the scene?', 'Every object, step and variable goes. Undo can bring them back.'],
    remove_object: [
      `Remove ${id}?`,
      `${id} goes, along with everything inside it **and any copies a pattern made from it**.`,
    ],
    remove_event: [
      `Remove step ${id}?`,
      'Later steps that depended on it will be reported as broken rather than removed.',
    ],
  }[method];
  return text && { title: text[0], message: new vscode.MarkdownString(text[1]) };
}

function registerLmTools(context: vscode.ExtensionContext): void {
  /** Read-only tool: forward input as RPC params, return the result. */
  const queryTool = (toolName: string, method: string) =>
    vscode.lm.registerTool(toolName, {
      prepareInvocation(options: vscode.LanguageModelToolInvocationPrepareOptions<object>) {
        return {
          invocationMessage: invocationMessage(
            method,
            options.input as Record<string, unknown>,
          ),
        };
      },
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
      prepareInvocation(options: vscode.LanguageModelToolInvocationPrepareOptions<object>) {
        const input = options.input as Record<string, unknown>;
        return {
          invocationMessage: invocationMessage(method, input),
          confirmationMessages: confirmation(method, input),
        };
      },
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
    queryTool('magpylib-studio_getVariables', 'get_variables'),
    queryTool('magpylib-studio_getEvents', 'get_events'),
    editTool('magpylib-studio_editEvent', 'edit_event'),
    editTool('magpylib-studio_removeEvent', 'remove_event'),
    editTool('magpylib-studio_moveEvent', 'move_event'),
    queryTool('magpylib-studio_sweep', 'sweep'),
    editTool('magpylib-studio_setVariable', 'set_variable'),
    editTool('magpylib-studio_setVariableBounds', 'set_variable_bounds'),
    editTool('magpylib-studio_duplicateAround', 'duplicate_around'),
    editTool('magpylib-studio_duplicateAlong', 'duplicate_along'),
    editTool('magpylib-studio_mirror', 'mirror'),
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
    async (id, parent) => {
      await mutateFromTree('move_object', { object_id: id, parent });
    },
    async () => {
      try {
        const { events, rollback } = await getEngine(context).request<{
          events: Omit<SceneOperation, 'kind'>[];
          rollback: number | null;
        }>('get_events');
        // the engine owns this state — any edit returns to the end of the
        // history — so the context key is read back rather than tracked
        void vscode.commands.executeCommand(
          'setContext', 'magpylib-studio.rolledBack', rollback !== null,
        );
        return events.map((event) => ({ ...event, kind: 'operation' as const }));
      } catch {
        return [];
      }
    },
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

  const variables = new VariablesViewProvider(
    context.extensionUri,
    (method, params) => getEngine(context).request(method, params),
    (action, name) => {
      void (async () => {
        const found = (
          await getEngine(context).request<{ variables: Variable[] }>('get_variables')
        ).variables.find((v) => v.name === name);
        if (!found) {
          return;
        }
        if (action === 'bounds') {
          await setVariableBounds(found);
        } else if (action === 'remove') {
          await mutateFromTree('remove_variable', { name });
        }
      })();
    },
    broadcastMutation,
  );
  variablesTree = variables;

  inspector = new InspectorViewProvider(
    context.extensionUri,
    async (method, params) => {
      // The inspector's fields take expressions too, and a webview cannot
      // raise an input box — so the ask happens here, on the way through.
      if (params && MUTATING_WITH_VALUES.has(method)) {
        if (!(await ensureVariablesDefined(Object.values(params)))) {
          return { ok: false, error: 'cancelled' };
        }
      }
      return getEngine(context).request(method, params);
    },
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

  /**
   * Offered wherever a variable is born, because a range given at that moment
   * is what makes it draggable, and hunting for a second command later is how
   * a slider never gets used. Enter skips it. Only the allowed range is asked
   * for: the slider falls back to it, and Set Bounds… covers the soft range
   * for when the two differ.
   */
  const askAllowedRange = async (name: string) => {
    const text = await vscode.window.showInputBox({
      prompt: `Allowed range for ${name} — optional, and gives it a slider`,
      placeHolder: 'min, max — e.g. 0, 10. Enter to skip',
      validateInput: (v) =>
        v.trim() === '' || parseBoundPair(v) ? undefined : 'min, max',
    });
    const pair = text && parseBoundPair(text);
    if (pair) {
      await mutateFromTree('set_variable_bounds', { name, min: pair[0], max: pair[1] });
    }
  };

  /**
   * Set a variable from an input box. A plain number stays a number; anything
   * else is stored as an expression, so the user types `gap*2` rather than
   * remembering the document's `=` marker.
   */
  /**
   * Validation that teaches: the engine says why a value is not an
   * expression, as it is typed, rather than after it is rejected. Names are
   * not checked here — one that does not exist yet is offered for creation.
   */
  const checkExpression = async (text: string): Promise<string | undefined> => {
    if (!text.trim()) {
      return 'A number, or an expression';
    }
    if (Number.isFinite(Number(text.trim()))) {
      return undefined;
    }
    const result = (await getEngine(context).request('check_expression', {
      text,
    })) as { ok: boolean; error?: string };
    return result.ok ? undefined : result.error;
  };

  /** One line of what expressions can do, read off the engine's allow-list. */
  const expressionHint = async (): Promise<string> => {
    const help = (await getEngine(context).request('expression_help')) as {
      functions: string[];
      constants: string[];
    };
    return (
      `+ - * / ** ( ) · ${help.functions.join(' ')} · ${help.constants.join(' ')}` +
      ' · other variables'
    );
  };

  const editVariable = async (variable: Variable, prompt?: string): Promise<boolean> => {
    const current =
      typeof variable.expression === 'string'
        ? variable.expression.slice(1)
        : String(variable.expression);
    const text = await vscode.window.showInputBox({
      prompt: prompt ?? `${variable.name} — value or expression`,
      value: current,
      placeHolder: await expressionHint(),
      validateInput: checkExpression,
    });
    if (text === undefined) {
      return false;
    }
    return mutateFromTree('set_variable', {
      name: variable.name,
      value: asDocumentValue(text),
    });
  };

  /**
   * Limits for a variable. Hard bounds are refused if broken; soft bounds are
   * only the range a slider spans, which is why they are asked for separately
   * — "never below zero" and "the interesting part is 1 to 5" are different
   * statements about the same variable.
   */
  const setVariableBounds = async (variable: Variable) => {
    const ask = async (prompt: string, current?: [number?, number?]) => {
      const value =
        current && (current[0] !== undefined || current[1] !== undefined)
          ? `${current[0] ?? ''}, ${current[1] ?? ''}`
          : '';
      const text = await vscode.window.showInputBox({
        prompt,
        value,
        placeHolder: 'min, max — or one of them, or empty for none',
        validateInput: (v) =>
          v.trim() === '' || parseBoundPair(v) ? undefined : 'min, max',
      });
      return text === undefined ? undefined : (parseBoundPair(text) ?? [null, null]);
    };
    const bounds = variable.bounds ?? {};
    const hard = await ask(
      `${variable.name} — allowed range (a value outside is refused)`,
      [bounds.min, bounds.max],
    );
    if (hard === undefined) {
      return;
    }
    const soft = await ask(
      `${variable.name} — slider range (empty: use the allowed range)`,
      [bounds.soft_min, bounds.soft_max],
    );
    if (soft === undefined) {
      return;
    }
    // Whole-or-not is a fact about the variable, not a slider setting: a
    // count of 7.3 is meaningless rather than merely precise.
    const kind = await vscode.window.showQuickPick(
      [
        { label: 'Any value', detail: 'a length, an angle, a field', whole: false },
        {
          label: 'Whole numbers only',
          detail: 'it counts things — magnets, turns, copies',
          whole: true,
        },
      ],
      { placeHolder: `${variable.name} — what kind of number?` },
    );
    if (!kind) {
      return;
    }
    await mutateFromTree('set_variable_bounds', {
      name: variable.name,
      min: hard[0],
      max: hard[1],
      soft_min: soft[0],
      soft_max: soft[1],
      integer: kind.whole,
    });
  };

  /**
   * A history edit reports what it broke rather than refusing, so the result
   * needs saying out loud — silently leaving red entries behind would be the
   * one way this feature could mislead.
   */
  const applyLogEdit = async (method: string, params: Record<string, unknown>) => {
    const result = (await getEngine(context).request(method, params)) as {
      ok: boolean;
      error?: string;
      broken?: { source: string; error: string }[];
    };
    if (!result.ok) {
      vscode.window.showErrorMessage(`Magpylib Studio: ${result.error}`);
    } else if (result.broken?.length) {
      const [first] = result.broken;
      vscode.window.showWarningMessage(
        `Magpylib Studio: ${result.broken.length} later ` +
          `${result.broken.length === 1 ? 'entry' : 'entries'} no longer ` +
          `${result.broken.length === 1 ? 'applies' : 'apply'} — ` +
          `${first.source} (${first.error}). Undo to put it back.`,
      );
    }
    broadcastMutation();
  };

  /**
   * A pattern, in the CAD sense: one step standing for N copies. Which kind
   * is asked first, because "around" and "along" are the same idea about a
   * different thing, and a grid is just "along" done twice.
   */
  const patternObject = async (obj: SceneObject) => {
    const kind = await vscode.window.showQuickPick(
      [
        {
          label: 'Around an axis',
          detail: 'evenly spaced about an axis — a ring, a rotor, a Halbach array',
          pattern: 'around',
        },
        {
          label: 'Along a direction',
          detail: 'evenly spaced in a line; pattern the group again for a grid',
          pattern: 'along',
        },
        {
          label: 'Mirrored',
          detail: 'one reflected copy — the polarization reflects as physics has it',
          pattern: 'mirror',
        },
      ],
      { placeHolder: `Pattern "${obj.label}"` },
    );
    if (!kind) {
      return;
    }
    if (kind.pattern === 'around') {
      await duplicateAround(obj);
    } else if (kind.pattern === 'along') {
      await duplicateAlong(obj);
    } else {
      await mirrorObject(obj);
    }
  };

  /** One reflected copy — see session.mirror for why it is not a sign flip. */
  const mirrorObject = async (obj: SceneObject) => {
    const plane = await vscode.window.showQuickPick(
      [
        { label: 'xy', detail: 'reflect through the xy plane (normal z)' },
        { label: 'xz', detail: 'reflect through the xz plane (normal y)' },
        { label: 'yz', detail: 'reflect through the yz plane (normal x)' },
      ],
      { placeHolder: `Mirror "${obj.label}" in which plane?` },
    );
    if (!plane) {
      return;
    }
    const anchor = await vscode.window.showInputBox({
      prompt: 'Point the plane passes through as x, y, z (m)',
      value: '0, 0, 0',
      validateInput: (v) =>
        parseVector(v, 3) ? undefined : 'Three numbers or expressions',
    });
    if (!anchor) {
      return;
    }
    await mutateFromTree('mirror', {
      object_id: obj.id,
      plane: plane.label,
      anchor: parseVector(anchor, 3),
    });
  };

  /** "N of these in a row" — see session.duplicate_along. */
  const duplicateAlong = async (obj: SceneObject) => {
    const count = await vscode.window.showInputBox({
      prompt: `Copies of "${obj.label}" in the row, counting the original`,
      value: '4',
      validateInput: (v) =>
        Number(v) >= 2 || /^[A-Za-z_]/.test(v.trim())
          ? undefined
          : 'A count of 2 or more, or a variable name',
    });
    if (!count) {
      return;
    }
    const step = await vscode.window.showInputBox({
      prompt: 'Step between copies as dx, dy, dz (m)',
      value: '2, 0, 0',
      placeHolder: 'numbers or expressions, e.g. pitch, 0, 0',
      validateInput: (v) =>
        parseVector(v, 3) ? undefined : 'Three numbers or expressions',
    });
    if (!step) {
      return;
    }
    await mutateFromTree('duplicate_along', {
      object_id: obj.id,
      count: asDocumentValue(count),
      step: parseVector(step, 3),
    });
  };

  /** "N of these around an axis" as one event — see session.duplicate_around. */
  const duplicateAround = async (obj: SceneObject) => {
    const count = await vscode.window.showInputBox({
      prompt: `Copies of "${obj.label}" around the axis, counting the original`,
      value: '6',
      validateInput: (v) =>
        Number(v) >= 2 || /^[A-Za-z_]/.test(v.trim())
          ? undefined
          : 'A count of 2 or more, or a variable name',
    });
    if (!count) {
      return;
    }
    const axis = await askRotationAxis();
    if (axis === undefined) {
      return;
    }
    const spin = await vscode.window.showQuickPick(
      [
        { label: 'Orbit only', detail: 'each copy keeps its orientation', spin: 0 },
        {
          label: 'Orbit and spin (Halbach)',
          detail: 'each copy also turns by one step in place',
          spin: 1,
        },
      ],
      { placeHolder: 'How should the copies be oriented?' },
    );
    if (!spin) {
      return;
    }
    const asNumber = Number(count);
    const countValue = Number.isFinite(asNumber) ? asNumber : `=${count.trim()}`;
    await mutateFromTree('duplicate_around', {
      object_id: obj.id,
      count: countValue,
      axis,
      anchor: [0, 0, 0],
      // one step per copy, expressed against the count so it follows it
      spin: spin.spin ? `=360/(${count.trim()})` : 0,
    });
  };

  /** Sweep a variable and show the field against it in the Field panel. */
  const sweepVariable = async () => {
    const { variables: available } = await getEngine(context).request<{
      variables: Variable[];
    }>('get_variables');
    if (!available.length) {
      vscode.window.showInformationMessage(
        'Magpylib Studio: define a variable first — a sweep varies one.',
      );
      return;
    }
    const pick = await vscode.window.showQuickPick(
      available.map((v) => ({ label: v.name, detail: `currently ${v.value}`, v })),
      { placeHolder: 'Variable to sweep' },
    );
    if (!pick) {
      return;
    }
    const range = await vscode.window.showInputBox({
      prompt: `Values for ${pick.label} — from, to, steps`,
      value: `${pick.v.value ?? 0}, ${(pick.v.value ?? 0) * 2 || 1}, 20`,
      validateInput: (v) =>
        (parseNumbers(v)?.length ?? 0) === 3 ? undefined : 'Three numbers: from, to, steps',
    });
    if (!range) {
      return;
    }
    const [from, to, steps] = parseNumbers(range)!;
    const count = Math.max(2, Math.round(steps));
    const values = Array.from(
      { length: count },
      (_, i) => from + ((to - from) * i) / (count - 1),
    );
    openFieldPanel(context);
    fieldPanel?.webview.postMessage({
      type: 'sweep',
      variable: pick.label,
      values,
    });
  };

    /**
   * Writing `a*2` into a field is a clear way to say "and let me set `a`",
   * but the document cannot build until `a` exists — so ask for it here,
   * before the value is stored, rather than reporting a failure after.
   * A definition may itself introduce names (`a = b*2`), hence the loop.
   * Returns false if the user backed out, meaning: abandon the whole edit.
   */
  const ensureVariablesDefined = async (values: unknown): Promise<boolean> => {
    const { unknown } = await getEngine(context).request<{ unknown: string[] }>(
      'unknown_variables',
      { values },
    );
    for (const name of unknown) {
      // A definition naming something that does not exist yet is rejected by
      // the engine, so stay on this one until it takes or the user gives up.
      for (;;) {
        const text = await vscode.window.showInputBox({
          prompt: `${name} is a new variable — give it a value`,
          placeHolder: await expressionHint(),
          validateInput: checkExpression,
        });
        if (text === undefined) {
          return false;
        }
        const result = (await getEngine(context).request('set_variable', {
          name,
          value: asDocumentValue(text),
        })) as { ok: boolean; error?: string };
        if (result.ok) {
          await askAllowedRange(name); // same offer as the explicit flow
          break;
        }
        const retry = await vscode.window.showErrorMessage(
          `Magpylib Studio: ${result.error}`,
          'Try again',
        );
        if (retry !== 'Try again') {
          return false;
        }
      }
    }
    variablesTree?.refresh();
    return true;
  };

  /** Run a mutating engine call from the tree UI, surface failures, refresh.
   *
   * `checkVariables: false` is for a call that carries a whole document
   * rather than something typed into a box: a scene brings its own
   * variables, so scanning it for undefined ones would ask the user to
   * define the very names it is about to load.
   */
  const mutateFromTree = async (
    method: string,
    params: Record<string, unknown>,
    { checkVariables = true } = {},
  ): Promise<boolean> => {
    // whatever was typed may name variables that do not exist yet
    if (checkVariables && !(await ensureVariablesDefined(Object.values(params)))) {
      return false;
    }
    let ok = false;
    try {
      const result = (await getEngine(context).request(method, params)) as {
        ok: boolean;
        error?: string;
        inserted_at?: number;
      };
      ok = result.ok;
      if (!result.ok) {
        vscode.window.showErrorMessage(`Magpylib Studio: ${result.error}`);
      } else if (result.inserted_at !== undefined) {
        // it went into the middle of the history, not the end — worth saying,
        // because the scene on screen is a preview and looks like the whole
        vscode.window.setStatusBarMessage(
          `Magpylib Studio: inserted at step ${result.inserted_at + 1} of the history`,
          3000,
        );
      }
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: ${err instanceof Error ? err.message : err}`,
      );
    }
    broadcastMutation();
    return ok;
  };

  sceneDocEmitter = new vscode.EventEmitter<vscode.Uri>();
  const sceneDocProvider: vscode.TextDocumentContentProvider = {
    onDidChange: sceneDocEmitter.event,
    provideTextDocumentContent: async () =>
      JSON.stringify(await getEngine(context).request('to_dict'), null, 2),
  };

  // Fixed at activation rather than when the tab is first opened. VS Code
  // restores that tab across a window reload, and until the extension knows
  // the path it owns nothing: the restored tab keeps showing whichever scene
  // was open last, and neither refreshing nor save-to-apply reaches it.
  const scriptDir = context.storageUri ?? context.globalStorageUri;
  scriptFile = vscode.Uri.joinPath(scriptDir, 'scene.py');

  const exists = async (u: vscode.Uri) => {
    try {
      await vscode.workspace.fs.stat(u);
      return true;
    } catch {
      return false;
    }
  };

  /** The open editor for the script tab, if the user has it open. */
  const scriptDoc = () =>
    scriptFile &&
    vscode.workspace.textDocuments.find((d) => d.uri.fsPath === scriptFile!.fsPath);

  /**
   * Write the scene's script into the tab. Unsaved edits are never clobbered:
   * a scene change while the user is mid-edit leaves their text alone, and so
   * does text the engine rejected (they are presumably fixing it). `force`
   * re-renders anyway — used when opening the tab and after a successful
   * apply, where the engine's rendering is by definition the truth.
   */
  const writeScriptFile = async (force = false) => {
    if (!scriptFile) {
      return;
    }
    const open = scriptDoc();
    if (!open && !force) {
      return; // no tab to keep in sync; opening one renders it fresh
    }
    if (!force && (open?.isDirty || scriptRejected)) {
      return;
    }
    const text = (await getEngine(context).request<string>('to_script')) + '\n';
    // Identical: don't churn the editor (it would move the cursor). What we
    // last wrote is only a safe stand-in for the file while the file is still
    // there — storage gets cleaned up, and openTextDocument would then fail.
    if (text === scriptOnDisk && (await exists(scriptFile))) {
      return;
    }
    scriptOnDisk = text;
    await vscode.workspace.fs.createDirectory(scriptDir);
    await vscode.workspace.fs.writeFile(scriptFile, Buffer.from(text, 'utf8'));
  };
  refreshScript = () => {
    void writeScriptFile();
  };

  /**
   * Re-render a script tab VS Code restored from the previous window. The file
   * on disk is scratch space holding the last session's scene, which has
   * nothing to do with the scene the engine has now — without this the tab
   * reads as the wrong project's script until it is closed and reopened.
   */
  const adoptRestoredScriptTab = async () => {
    const restored = vscode.window.tabGroups.all.some((group) =>
      group.tabs.some(
        (tab) =>
          tab.input instanceof vscode.TabInputText &&
          tab.input.uri.fsPath === scriptFile!.fsPath,
      ),
    );
    if (!restored) {
      return;
    }
    try {
      // A restored tab in a background group has no loaded document yet; open
      // it (no editor is shown) so its dirty state is knowable.
      const doc = await vscode.workspace.openTextDocument(scriptFile!);
      await writeScriptFile(!doc.isDirty); // hot exit may hold unsaved edits
    } catch (err) {
      engineOutput?.appendLine(
        `script tab: ${err instanceof Error ? err.message : err}`,
      );
    }
  };

  /** Saving the script tab rebuilds the scene from it. */
  const applyScriptFile = async (doc: vscode.TextDocument) => {
    scriptOnDisk = doc.getText(); // the file is the user's text now, not ours
    const result = (await getEngine(context).request('apply_script', {
      path: scriptFile!.fsPath,
    })) as { ok: boolean; error?: string; warnings?: string[] };
    if (!result.ok) {
      scriptRejected = true;
      vscode.window.showErrorMessage(`Magpylib Studio script: ${result.error}`);
      return;
    }
    scriptRejected = false;
    broadcastMutation();
    // The scene, not the text, is canonical: show what the engine actually
    // built (ids sanitised, transforms resolved, comments gone).
    await writeScriptFile(true);
    if (result.warnings?.length) {
      vscode.window.showWarningMessage(
        `Magpylib Studio script applied — ${result.warnings.join('; ')}`,
      );
    } else {
      vscode.window.setStatusBarMessage('Magpylib Studio: scene updated from script', 2000);
    }
  };

  /** Remember which file this workspace was last editing, and whether what
   *  was on screen still matched it. Per workspace, not global: two windows
   *  are two scenes, and each should come back to its own. */
  const rememberScene = () =>
    context.workspaceState.update(SCENE_STATE_KEY, {
      file: sceneFile?.toString(),
      dirty: sceneDirty,
    });
  rememberSceneState = rememberScene;

  /** Point the studio at a file (or at nothing, for an unsaved scene) and
   *  record whether it currently differs from it. */
  const setSceneFile = async (uri: vscode.Uri | undefined, dirty = false) => {
    sceneFile = uri;
    sceneDirty = dirty;
    showSceneFile();
    await rememberScene();
  };

  sceneBackupFile = vscode.Uri.joinPath(scriptDir, 'backup.magpy.json');
  writeSceneBackup = async () => {
    try {
      const doc = await getEngine(context).request('to_dict');
      await vscode.workspace.fs.writeFile(
        sceneBackupFile!,
        Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf8'),
      );
      await rememberScene();
    } catch {
      // A backup that cannot be written must not interrupt editing; the next
      // mutation tries again, and the worst case is what we had before it.
    }
  };

  /**
   * Save the scene. With a file already, that is all it does; without one —
   * or for Save As — it asks where, and from then on the scene has a name.
   */
  const saveScene = async ({ prompt = false } = {}): Promise<boolean> => {
    let target = prompt ? undefined : sceneFile;
    if (!target) {
      const folder = sceneFile
        ? vscode.Uri.joinPath(sceneFile, '..')
        : vscode.workspace.workspaceFolders?.[0]?.uri;
      target = await vscode.window.showSaveDialog({
        // The scene is the document; the script is an export of it, and lives
        // on its own command so that choosing where to save cannot silently
        // choose a lossy format (a script carries no slider bounds and no
        // hidden flags — see "Export as Python Script").
        filters: { 'Magpylib scene': ['magpy.json'] },
        defaultUri: folder && vscode.Uri.joinPath(folder, `scene${SCENE_EXTENSION}`),
        saveLabel: 'Save Scene',
      });
      if (!target) {
        return false;
      }
    }
    try {
      const doc = await getEngine(context).request('to_dict');
      await vscode.workspace.fs.writeFile(
        target,
        Buffer.from(JSON.stringify(doc, null, 2) + '\n', 'utf8'),
      );
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: could not save — ${err instanceof Error ? err.message : err}`,
      );
      return false;
    }
    await setSceneFile(target);
    vscode.window.setStatusBarMessage(`Magpylib Studio: saved ${basename(target)}`, 2000);
    return true;
  };

  /**
   * Open a scene file into the engine.
   *
   * The bytes are read here rather than handed to the engine as a path, so
   * this works wherever VS Code can reach — a remote workspace, a virtual
   * filesystem — instead of only where the Python process can open() it.
   */
  const openSceneFile = async (uri: vscode.Uri): Promise<boolean> => {
    let scene: unknown;
    try {
      scene = JSON.parse(Buffer.from(await vscode.workspace.fs.readFile(uri)).toString('utf8'));
    } catch (err) {
      vscode.window.showErrorMessage(
        `Magpylib Studio: could not read ${basename(uri)} — ` +
          `${err instanceof Error ? err.message : err}`,
      );
      return false;
    }
    if (!(await mutateFromTree('load_scene', { scene }, { checkVariables: false }))) {
      return false; // the engine said why (wrong format, or a newer version)
    }
    await setSceneFile(uri);
    openStudioPanel(context);
    return true;
  };

  /**
   * Stop before something that would throw away unsaved work.
   *
   * Programmatic callers (tests, a URI handler) pass `discardChanges` to say
   * they have already decided; leaving it out is what a person clicking a
   * menu means, and they get asked.
   */
  const confirmDiscard = async (what: string, options?: Discard): Promise<boolean> => {
    if (!sceneDirty || options?.discardChanges) {
      return true;
    }
    const name = sceneFile ? basename(sceneFile) : 'this scene';
    const answer = await vscode.window.showWarningMessage(
      `${name} has unsaved changes.`,
      { modal: true, detail: `They will be lost by ${what}.` },
      'Save',
      "Don't Save",
    );
    if (answer === 'Save') {
      return saveScene();
    }
    return answer === "Don't Save";
  };

  sceneTreeView = vscode.window.createTreeView('magpylib-studio.sceneView', {
    treeDataProvider: tree,
    dragAndDropController: tree,
  });
  showSceneFile();

  context.subscriptions.push(
    sceneTreeView,
    // No retainContextWhenHidden: the guide calls it a last resort for good
    // reason (it keeps the whole webview running), and neither sidebar view
    // needs it — both rebuild from the engine the moment they report ready.
    vscode.window.registerWebviewViewProvider(InspectorViewProvider.viewId, inspector),
    vscode.window.registerTreeDataProvider('magpylib-studio.historyView', history),
    vscode.window.registerWebviewViewProvider(VariablesViewProvider.viewId, variables),
    vscode.commands.registerCommand('magpylib-studio.addVariable', async () => {
      const name = await vscode.window.showInputBox({
        prompt: 'Variable name',
        placeHolder: 'letters, digits, underscores — e.g. gap, n, radius',
        validateInput: (v) =>
          /^[A-Za-z_]\w*$/.test(v)
            ? undefined
            : 'Letters, digits, underscores; must not start with a digit.',
      });
      if (!name) {
        return;
      }
      if (!(await editVariable({ name, expression: 0, value: 0 }, 'Value or expression'))) {
        return;
      }
      await askAllowedRange(name);
    }),
    vscode.commands.registerCommand(
      'magpylib-studio.editVariable',
      async (variable: Variable) => editVariable(variable),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.removeVariable',
      async (variable: Variable) => {
        await mutateFromTree('remove_variable', { name: variable.name });
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.setVariableBounds',
      async (variable: Variable) => setVariableBounds(variable),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.duplicateAround',
      async (obj: SceneObject) => patternObject(obj),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.selectOperation',
      (operation: SceneOperation) => {
        // selecting a step shows the object it acted on, so the 3D view and
        // the Inspector follow the history as you walk it
        selectObjectInStudio(context, operation.target);
        inspector?.showOperation(operation.id);
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.editOperation',
      async (operation: SceneOperation) => {
        // Same as selecting it, but says where the values are: a step's
        // fields appear in the Inspector, and nothing about a tree row
        // suggests looking at another panel.
        selectObjectInStudio(context, operation.target);
        inspector?.showOperation(operation.id);
        await vscode.commands.executeCommand(
          `${InspectorViewProvider.viewId}.focus`,
        );
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.operationEarlier',
      async (operation: SceneOperation) =>
        applyLogEdit('move_event', {
          event_id: operation.id,
          index: Math.max(0, operation.index - 1),
        }),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.operationLater',
      async (operation: SceneOperation) =>
        applyLogEdit('move_event', {
          event_id: operation.id,
          index: operation.index + 1,
        }),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.removeOperation',
      async (operation: SceneOperation) =>
        applyLogEdit('remove_event', { event_id: operation.id }),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.rollbackTo',
      async (operation: SceneOperation) => {
        // "up to and including this step", which is what pointing at a step
        // means; the bar in a CAD tree sits below the feature it stops after
        await getEngine(context).request('set_rollback', {
          index: operation.index + 1,
        });
        // A view of the document, not a change to it — the log is untouched,
        // so this leaves a saved scene saved.
        refreshSurfaces();
      },
    ),
    vscode.commands.registerCommand('magpylib-studio.rollbackClear', async () => {
      await getEngine(context).request('set_rollback', {});
      refreshSurfaces();
    }),
    vscode.commands.registerCommand('magpylib-studio.sweep', async () => sweepVariable()),
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
      scriptRejected = false; // opening the tab starts from the real scene
      await writeScriptFile(!scriptDoc()?.isDirty); // never over unsaved edits
      const doc = await vscode.workspace.openTextDocument(scriptFile!);
      // Reuse the group it is already in, the way the Studio and Field panels
      // reveal themselves. `Beside` is relative to whatever is focused, so
      // running this from the script's own column opens another one each time.
      const open = vscode.window.tabGroups.all
        .flatMap((group) => group.tabs.map((tab) => ({ group, tab })))
        .find(
          ({ tab }) =>
            tab.input instanceof vscode.TabInputText &&
            tab.input.uri.fsPath === scriptFile!.fsPath,
        );
      await vscode.window.showTextDocument(doc, {
        viewColumn: open?.group.viewColumn ?? vscode.ViewColumn.Beside,
        preview: false,
      });
    }),
    vscode.workspace.onWillSaveTextDocument((e) => {
      if (scriptFile && e.document.uri.fsPath === scriptFile.fsPath) {
        scriptSaveReason = e.reason;
      }
    }),
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      if (!scriptFile || doc.uri.fsPath !== scriptFile.fsPath) {
        return;
      }
      const reason = scriptSaveReason;
      scriptSaveReason = undefined;
      // Applying is deliberate. With files.autoSave on a delay, a save lands
      // between keystrokes, and running a half-typed script would spray
      // errors and rewrite the buffer mid-edit; Cmd+S still applies as usual.
      if (reason === vscode.TextDocumentSaveReason.AfterDelay) {
        return;
      }
      await applyScriptFile(doc);
    }),
    vscode.commands.registerCommand('magpylib-studio.saveScene', () => saveScene()),
    vscode.commands.registerCommand('magpylib-studio.saveSceneAs', () =>
      saveScene({ prompt: true }),
    ),
    vscode.commands.registerCommand('magpylib-studio.exportScript', async () => {
      // Export, not save: the script is runnable magpylib anyone can use
      // without the studio, but it carries no slider bounds and no hidden
      // flags, so it is not what Save writes and does not become the file.
      const folder = sceneFile
        ? vscode.Uri.joinPath(sceneFile, '..')
        : vscode.workspace.workspaceFolders?.[0]?.uri;
      const name = sceneFile ? basename(sceneFile).replace(/\.magpy\.json$/, '') : 'scene';
      const target = await vscode.window.showSaveDialog({
        filters: { 'Python script': ['py'] },
        defaultUri: folder && vscode.Uri.joinPath(folder, `${name}.py`),
        saveLabel: 'Export Script',
      });
      if (!target) {
        return;
      }
      const script = await getEngine(context).request<string>('to_script');
      await vscode.workspace.fs.writeFile(target, Buffer.from(script + '\n', 'utf8'));
      const open = await vscode.window.showInformationMessage(
        `Magpylib Studio: exported ${basename(target)}`,
        'Open',
      );
      if (open === 'Open') {
        await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(target));
      }
    }),
    vscode.commands.registerCommand('magpylib-studio.newScene', async (options?: Discard) => {
      if (!(await confirmDiscard('starting a new scene', options))) {
        return;
      }
      if (await mutateFromTree('clear_scene', {})) {
        await setSceneFile(undefined);
      }
    }),
    vscode.commands.registerCommand('magpylib-studio.revertScene', async () => {
      if (!sceneFile) {
        return;
      }
      const answer = await vscode.window.showWarningMessage(
        `Discard changes and reload ${basename(sceneFile)}?`,
        { modal: true },
        'Revert',
      );
      if (answer === 'Revert') {
        await openSceneFile(sceneFile);
      }
    }),
    vscode.commands.registerCommand(
      'magpylib-studio.loadScene',
      async (uri?: vscode.Uri, options?: Discard) => {
        if (!(await confirmDiscard('opening another scene', options))) {
          return;
        }
        const target =
          uri ??
          (
            await vscode.window.showOpenDialog({
              filters: { 'Magpylib scene': ['magpy.json', 'json'] },
              canSelectMany: false,
              defaultUri: vscode.workspace.workspaceFolders?.[0]?.uri,
              openLabel: 'Open Scene',
            })
          )?.[0];
        if (target) {
          await openSceneFile(target);
        }
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.importScript',
      async (options?: Discard) => {
        if (!(await confirmDiscard('importing a script', options))) {
          return;
        }
        const picks = await vscode.window.showOpenDialog({
          filters: { 'Python script': ['py'] },
          canSelectMany: false,
        });
        if (picks?.length) {
          await importScript(picks[0]);
        }
      },
    ),
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
    // A redraw, not an edit: the scene is unchanged, so this must not mark it
    // as having unsaved changes.
    vscode.commands.registerCommand('magpylib-studio.refreshScene', () =>
      refreshSurfaces(),
    ),
    // The name may be passed in — from a keybinding, a task, or a test —
    // in which case there is nothing to ask.
    vscode.commands.registerCommand(
      'magpylib-studio.loadExample',
      async (name?: string, options?: Discard) => {
        if (!(await confirmDiscard('loading an example', options))) {
          return;
        }
        let chosen = name;
        if (!chosen) {
          const { examples } = await getEngine(context).request<{
            examples: { name: string; label: string; description: string }[];
          }>('list_examples');
          // Each leans on a different feature, so the description is the point
          // of the list — it is what tells you the tool can do that at all.
          const pick = await vscode.window.showQuickPick(
            examples.map((e) => ({ label: e.label, detail: e.description, e })),
            { placeHolder: 'Example scene to load' },
          );
          if (!pick) {
            return;
          }
          chosen = pick.e.name;
        }
        if (await mutateFromTree('load_example', { name: chosen })) {
          // An example is a starting point, not a document: it has no file of
          // its own, and it counts as unsaved so that Save asks where to put it
          // rather than writing over whatever was open before.
          await setSceneFile(undefined, true);
        }
        openStudioPanel(context); // loading a scene should show it
      },
    ),
    vscode.commands.registerCommand('magpylib-studio.selectObject', (objectId: string) =>
      selectObjectInStudio(context, objectId),
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.removeObject',
      (obj?: SceneObject) => {
        const target = obj ?? treeSelection();
        return target
          ? mutateFromTree('remove_object', { object_id: target.id })
          : undefined;
      },
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
                return v.trim() ? undefined : 'A number, or an expression';
              }
              return parseTerms(v)
                ? undefined
                : 'Numbers or expressions, e.g. 0, 0, gap';
            },
          });
          if (text === undefined) {
            return; // escaped: abandon the whole creation
          }
          if (isScalar) {
            values[name] = asDocumentValue(text);
          } else {
            const terms = parseTerms(text)!;
            const template = def as number[] | number[][];
            values[name] = Array.isArray(template[0])
              ? reshape(terms, (template[0] as number[]).length)
              : terms;
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
        // only if it was actually created: selecting an id that does not
        // exist leaves the Inspector showing an error about it
        if (await mutateFromTree('add_object', params)) {
          selectObjectInStudio(context, id); // show it in the Inspector
        }
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
            parseVector(v, 3)
              ? undefined
              : 'Three numbers or expressions, e.g. 0, 0, gap',
        });
        const d = text && parseVector(text, 3);
        if (!d) {
          return;
        }
        // A path is divided up here, which needs numbers; a single symbolic
        // displacement is fine and stays tied to its variables.
        const numeric = d.filter((c) => typeof c === 'number') as number[];
        if (kind.steps > 1 && numeric.length !== d.length) {
          vscode.window.showErrorMessage(
            'Magpylib Studio: a multi-step path needs numbers, not expressions.',
          );
          return;
        }
        const displacement =
          kind.steps === 1
            ? d
            : Array.from({ length: kind.steps }, (_, i) =>
                numeric.map((c) => (c * (i + 1)) / kind.steps),
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
      'magpylib-studio.pixelGrid',
      async (obj?: SceneObject) => {
        const target = obj ?? treeSelection();
        if (!target) {
          return;
        }
        const plane = await vscode.window.showQuickPick(['xy', 'xz', 'yz'], {
          placeHolder: `Pixel grid plane for "${target.label}" (in its own frame)`,
        });
        if (!plane) {
          return;
        }
        const sizeText = await vscode.window.showInputBox({
          prompt: 'Grid size (m) — the plane spans ± half of this',
          value: '4',
          validateInput: (v) => (Number(v) > 0 ? undefined : 'A positive number'),
        });
        if (!sizeText) {
          return;
        }
        const resText = await vscode.window.showInputBox({
          prompt: 'Pixels per side',
          value: '30',
          validateInput: (v) =>
            Number.isInteger(Number(v)) && Number(v) >= 2 ? undefined : 'At least 2',
        });
        if (!resText) {
          return;
        }
        await mutateFromTree('set_pixel_grid', {
          object_id: target.id,
          plane,
          size: Number(sizeText),
          resolution: Number(resText),
        });
        openFieldPanel(context); // the map is the point of making a grid
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.toggleVisibility',
      (obj: SceneObject) =>
        mutateFromTree('set_visible', { object_id: obj.id, visible: !obj.visible }),
    ),
    vscode.commands.registerCommand('magpylib-studio.copyObject', (obj?: SceneObject) => {
      const target = obj ?? treeSelection();
      if (target) {
        clipboard = { id: target.id, cut: false };
        vscode.window.setStatusBarMessage(
          `Magpylib Studio: copied "${target.label}"`,
          2000,
        );
      }
    }),
    vscode.commands.registerCommand('magpylib-studio.cutObject', (obj?: SceneObject) => {
      const target = obj ?? treeSelection();
      if (target) {
        clipboard = { id: target.id, cut: true };
        vscode.window.setStatusBarMessage(`Magpylib Studio: cut "${target.label}"`, 2000);
      }
    }),
    vscode.commands.registerCommand(
      'magpylib-studio.pasteObject',
      async (obj?: SceneObject) => {
        if (!clipboard) {
          vscode.window.setStatusBarMessage('Magpylib Studio: nothing to paste', 2000);
          return;
        }
        // Paste into a collection when one is targeted, else at the scene root.
        const target = obj ?? treeSelection();
        const parent =
          target?.type === 'Collection' ? target.id : (target?.parent ?? null);
        if (clipboard.cut) {
          await mutateFromTree('move_object', {
            object_id: clipboard.id,
            parent,
          });
          clipboard = undefined; // a cut object can only land once
        } else {
          await mutateFromTree('copy_object', { object_id: clipboard.id, parent });
        }
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.renameObject',
      async (obj?: SceneObject) => {
        const target = obj ?? treeSelection();
        if (!target) {
          return;
        }
        const label = await vscode.window.showInputBox({
          prompt: `Name for "${target.id}"`,
          value: target.label,
          validateInput: (v) => (v.trim() ? undefined : 'The name cannot be empty'),
        });
        if (label && label !== target.label) {
          await mutateFromTree('apply_edit', {
            object_id: target.id,
            path: 'label',
            value: label,
          });
        }
      },
    ),
    vscode.commands.registerCommand(
      'magpylib-studio.newCollection',
      async (obj?: SceneObject) => {
        const id = await vscode.window.showInputBox({
          prompt: 'Id for the new collection',
          placeHolder: 'letters, digits, underscores — e.g. ring, rotor',
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
  /**
   * Come back to whatever this workspace was editing.
   *
   * The scene lives in a subprocess that dies with the window, so without
   * this a reload silently starts from an empty scene — which is the one way
   * the studio could lose work outright. Nothing remembered means nothing to
   * do, so a workspace that has never opened a scene does not even start the
   * engine.
   */
  const restoreScene = async () => {
    const remembered = context.workspaceState.get<{ file?: string; dirty?: boolean }>(
      SCENE_STATE_KEY,
    );
    if (!remembered?.file && !remembered?.dirty) {
      return;
    }
    const file = remembered.file ? vscode.Uri.parse(remembered.file) : undefined;
    // Unsaved changes go through the backup, which is the only copy of them.
    // Offered rather than restored: coming back to a scene you thought you
    // had abandoned is its own kind of surprise, so the choice stays yours.
    if (remembered.dirty && sceneBackupFile && (await exists(sceneBackupFile))) {
      const name = file ? basename(file) : 'an unsaved scene';
      const answer = await vscode.window.showInformationMessage(
        `Magpylib Studio: ${name} had unsaved changes when the window closed.`,
        'Restore',
        'Discard',
      );
      if (answer === 'Restore') {
        if (await openSceneFile(sceneBackupFile)) {
          // it is those changes, not the backup file, that the user is editing
          await setSceneFile(file, true);
        }
        return;
      }
      if (answer !== 'Discard') {
        return; // dismissed: leave the backup alone, ask again next time
      }
    }
    if (file && (await exists(file))) {
      await openSceneFile(file);
    } else if (file) {
      await setSceneFile(undefined);
      vscode.window.showWarningMessage(
        `Magpylib Studio: ${basename(file)} is no longer there; starting empty.`,
      );
    }
  };

  registerLmTools(context);
  void adoptRestoredScriptTab();
  void restoreScene();

  // First activation on this machine, any workspace: open the walkthrough
  // once. Global rather than per-workspace state — a returning user opening
  // a second project should not see it again.
  if (!context.globalState.get(TOUR_SHOWN_KEY)) {
    void context.globalState.update(TOUR_SHOWN_KEY, true);
    void vscode.commands.executeCommand(
      'workbench.action.openWalkthrough',
      `${context.extension.id}#gettingStarted`,
      false,
    );
  }
}

export async function deactivate(): Promise<void> {
  // A backup is debounced by a second, so on a clean shutdown there is often
  // one still pending — write it before the engine holding the scene goes.
  // (A crash gets no such courtesy, which is what the debounce is short for.)
  if (backupTimer) {
    clearTimeout(backupTimer);
    backupTimer = undefined;
    await writeSceneBackup?.();
  }
  engine?.dispose();
  engine = undefined;
}

/**
 * Which file the scene is, whether it differs from it, and where the crash
 * backup goes — the state that survives a window reload.
 *
 * Exported for the integration tests: a test cannot reload the window, so it
 * checks that what a reload would read back is being written correctly. The
 * reload itself stays a manual check.
 */
export function sceneFileState(): {
  file: string | undefined;
  dirty: boolean;
  backup: string | undefined;
} {
  return {
    file: sceneFile?.toString(),
    dirty: sceneDirty,
    backup: sceneBackupFile?.toString(),
  };
}

export function createWebviewHtml(
  context: vscode.ExtensionContext,
  webview: vscode.Webview,
): string {
  const nonce = webviewNonce();
  const studioStyleUri = mediaUri(webview, context.extensionUri, 'studio.css');
  const studioScriptUri = mediaUri(webview, context.extensionUri, 'studio.js');
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
  <link rel="stylesheet" href="${studioStyleUri}" />
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="canvas"></div>
  <div id="statusbar">
    <label><input type="checkbox" id="animate" /> Animate paths</label>
    <span id="status">Starting…</span>
  </div>
  <script nonce="${nonce}" src="${studioScriptUri}"></script>
</body>
</html>`;
}

export function createFieldViewHtml(
  context: vscode.ExtensionContext,
  webview: vscode.Webview,
): string {
  const nonce = webviewNonce();
  const fieldStyleUri = mediaUri(webview, context.extensionUri, 'field.css');
  const fieldScriptUri = mediaUri(webview, context.extensionUri, 'field.js');
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
  <link rel="stylesheet" href="${fieldStyleUri}" />
  <script nonce="${nonce}" src="${plotlyUri}"></script>
</head>
<body>
  <div id="canvas"></div>
  <div id="statusbar">
    <label>
      <select id="mode">
        <option value="path">Along sensor path</option>
        <option value="map">Plane map</option>
        <option value="sweep">Against a variable</option>
      </select>
    </label>
    <span class="path-only">
      <label>Output
        <select id="output">
          <option>B</option><option>Bx</option><option>By</option><option>Bz</option>
          <option>Bxy</option>
          <option>H</option><option>Hx</option><option>Hy</option><option>Hz</option>
          <option>J</option><option>Jx</option><option>Jy</option><option>Jz</option>
          <option>M</option><option>Mx</option><option>My</option><option>Mz</option>
        </select>
      </label>
      <label><input type="checkbox" id="animate" /> Animate path</label>
    </span>
    <span class="map-only" hidden>
      <label>
        <select id="source"><option value="">on a plane</option></select>
      </label>
    </span>
    <span class="plane-only" hidden>
      <label>Plane
        <select id="plane">
          <option>xy</option><option>xz</option><option>yz</option>
        </select>
      </label>
      <label>at <input type="number" id="offset" step="any" value="0" /> m</label>
      <label>
        <select id="component">
          <option value="magnitude">magnitude</option>
          <option value="x">x</option><option value="y">y</option>
          <option value="z">z</option>
        </select>
      </label>
      <label>of
        <select id="quantity">
          <option>B</option><option>H</option>
          <option>J</option><option>M</option>
        </select>
      </label>
      <label><input type="checkbox" id="log" checked /> log</label>
      <label>res <input type="number" id="resolution" min="5" max="200" value="50" /></label>
    </span>
    <span class="map-only" hidden>
      <label>
        <select id="mapComponent">
          <option value="magnitude">magnitude</option>
          <option value="x">x</option><option value="y">y</option>
          <option value="z">z</option>
        </select>
      </label>
      <label>of
        <select id="mapQuantity">
          <option>B</option><option>H</option>
          <option>J</option><option>M</option>
        </select>
      </label>
    </span>
    <span class="sweep-only" hidden>
      <label>
        <select id="sweepComponent">
          <option value="magnitude">magnitude</option>
          <option value="x">x</option><option value="y">y</option>
          <option value="z">z</option>
        </select>
      </label>
      <label>of
        <select id="sweepField">
          <option>B</option><option>H</option>
          <option>J</option><option>M</option>
        </select>
      </label>
      <span id="sweepRange"></span>
    </span>
    <span id="status">Loading…</span>
  </div>
  <script nonce="${nonce}" src="${fieldScriptUri}"></script>
</body>
</html>`;
}
