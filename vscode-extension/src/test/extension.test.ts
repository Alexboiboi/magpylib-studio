import * as assert from 'assert';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import * as vscode from 'vscode';

import { sceneFileState } from '../extension';

/**
 * End-to-end through the real vscode API: activation, the engine subprocess,
 * the RPC round trip, the virtual document and the script tab.
 *
 * These are the seams the Python suite cannot reach and the DOM harness only
 * approximates. What they deliberately do not cover is webview *content* — a
 * test cannot read into a webview, so the panels stay with the harness.
 */

const SCENE_JSON = vscode.Uri.parse('magpylib-studio:/scene.json');
const EXTENSION_ID = 'magpylib.magpylib-studio-vscode';

/** Commands that replace the scene ask about unsaved changes, which in a test
 *  host is a modal nothing will ever click. Saying so up front is what any
 *  non-interactive caller does. */
const DISCARD = { discardChanges: true };

/** to_dict, read the way any editor tab would read it. */
async function scene(): Promise<{
  version: number;
  generator: string;
  events: { op: string; target: string }[];
  objects: { id: string }[];
}> {
  const doc = await vscode.workspace.openTextDocument(SCENE_JSON);
  return JSON.parse(doc.getText());
}

/**
 * Read `scene.json` once it says what the test is waiting for.
 *
 * It is a virtual document, so VS Code serves it from cache until the
 * extension fires onDidChange — which it does on a 150 ms debounce, after the
 * command has already resolved. Waiting a fixed slice of time for that is
 * what this used to do, and it passed on a laptop and failed on CI, which is
 * the usual bargain. Polling for the state instead is both faster and not a
 * bet on how quick the machine is.
 */
async function sceneWhere(
  predicate: (doc: Awaited<ReturnType<typeof scene>>) => boolean,
  what: string,
  timeoutMs = 20000,
): Promise<Awaited<ReturnType<typeof scene>>> {
  const deadline = Date.now() + timeoutMs;
  let last = await scene();
  while (!predicate(last)) {
    if (Date.now() > deadline) {
      throw new Error(
        `timed out after ${timeoutMs} ms waiting for ${what}; ` +
          `scene has ${last.objects.length} top-level objects`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
    last = await scene();
  }
  return last;
}

/** Every id in the tree, nesting included. */
function ids(specs: { id: string; children?: unknown[] }[]): Set<string> {
  const found = new Set<string>();
  const walk = (list: { id: string; children?: unknown[] }[]) => {
    for (const spec of list) {
      found.add(spec.id);
      walk((spec.children ?? []) as { id: string; children?: unknown[] }[]);
    }
  };
  walk(specs);
  return found;
}

// A predicate has to tell the state it is waiting for apart from the one
// already there, or it returns instantly with the *previous* test's scene —
// which is not a hypothetical: "any object at all" did exactly that, and the
// test that captured a scene to compare against captured the wrong one.
const holding = (id: string) => (doc: { objects: { id: string }[] }) =>
  ids(doc.objects as { id: string }[]).has(id);
const without = (id: string) => (doc: { objects: { id: string }[] }) =>
  !ids(doc.objects as { id: string }[]).has(id);
const nothing = (doc: { objects: unknown[] }) => doc.objects.length === 0;

/** Same, for a file the extension writes on its own schedule. */
async function fileWhere(
  uri: vscode.Uri,
  predicate: (doc: Record<string, unknown>) => boolean,
  what: string,
  timeoutMs = 20000,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const doc = await readJson(uri);
      if (predicate(doc)) {
        return doc;
      }
    } catch {
      // not written yet, or written half-way: try again
    }
    if (Date.now() > deadline) {
      let seen = '(unreadable)';
      try {
        seen = JSON.stringify(await readJson(uri)).slice(0, 400);
      } catch {
        // leave it as unreadable
      }
      throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}; file holds ${seen}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

/** A real wait, for the cases that assert something does NOT happen — there
 *  is no event to poll for, so the only honest option is to give it time. */
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Load an example from a known-empty scene.
 *
 * Waiting for "the scene contains halbach" is not enough on its own: the
 * previous test leaves one loaded, so the predicate is already true and the
 * wait returns the *old* scene. That is not theoretical — it let a test write
 * a stale scene to its file and then delete an object the file did not
 * contain, which surfaced as an "unknown object id" popup and, worse, made
 * the assertion that the removal landed pass because the object had never
 * been there. Clearing first makes empty -> loaded a transition that can only
 * mean this call.
 */
async function loadExample(name: string, rootId: string) {
  await vscode.commands.executeCommand('magpylib-studio.newScene', DISCARD);
  await sceneWhere(nothing, 'the scene to clear');
  await vscode.commands.executeCommand('magpylib-studio.loadExample', name, DISCARD);
  return sceneWhere(holding(rootId), `the ${name} example to load`);
}

/** A scratch file that goes away with the test run. */
function tempScene(name: string): vscode.Uri {
  return vscode.Uri.file(join(mkdtempSync(join(tmpdir(), 'magpy-')), name));
}

async function readJson(uri: vscode.Uri): Promise<Record<string, unknown>> {
  return JSON.parse(Buffer.from(await vscode.workspace.fs.readFile(uri)).toString('utf8'));
}

async function writeJson(uri: vscode.Uri, value: unknown): Promise<void> {
  await vscode.workspace.fs.writeFile(uri, Buffer.from(JSON.stringify(value), 'utf8'));
}

suite('magpylib-studio', () => {
  suiteSetup(async function () {
    this.timeout(120000);
    const extension = vscode.extensions.getExtension(EXTENSION_ID);
    assert.ok(extension, `extension ${EXTENSION_ID} is not installed`);
    await extension.activate();
  });

  test('every declared command is registered', async () => {
    const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
    const declared: string[] = extension.packageJSON.contributes.commands.map(
      (c: { command: string }) => c.command,
    );
    const registered = new Set(await vscode.commands.getCommands(true));
    const missing = declared.filter((c) => !registered.has(c));
    assert.deepStrictEqual(missing, [], 'declared but never registered');
  });

  test('every declared language model tool is live', () => {
    const extension = vscode.extensions.getExtension(EXTENSION_ID)!;
    const declared: string[] = extension.packageJSON.contributes.languageModelTools.map(
      (t: { name: string }) => t.name,
    );
    const live = new Set(vscode.lm.tools.map((t) => t.name));
    assert.deepStrictEqual(
      declared.filter((name) => !live.has(name)),
      [],
      'declared in package.json but not registered',
    );
  });

  test('the engine builds a scene and the virtual document shows it', async function () {
    this.timeout(60000);
    await loadExample('halbach', 'halbach');
    const doc = await scene();
    // one magnet and one pattern step per ring, not twenty declared magnets
    assert.ok(doc.events.length > 0, 'the log is empty');
    assert.ok(
      doc.events.some((e) => e.op === 'duplicate_around'),
      'the halbach example should carry a circular pattern',
    );
    assert.ok(doc.objects.length > 0, 'nothing was built');
  });

  test('removing a patterned magnet takes its copies with it', async function () {
    this.timeout(60000);
    await loadExample('halbach', 'halbach');

    // the tree hands the command an object; a test can hand it the same thing
    await vscode.commands.executeCommand('magpylib-studio.removeObject', {
      id: 'r1',
      type: 'magnet.Cuboid',
      label: 'Magnet 1',
      parent: 'ring1',
      visible: true,
    });
    const after = await sceneWhere(without('r1'), 'the removal to land');

    const present = ids(after.objects as { id: string }[]);
    assert.ok(!present.has('r1'), 'the magnet is still in the document');
    assert.ok(present.has('r2'), 'the other ring should be untouched');
  });

  test('the script tab renders the scene and applies on save', async function () {
    this.timeout(60000);
    await loadExample('halbach', 'halbach');
    await vscode.commands.executeCommand('magpylib-studio.viewScript');

    const tab = vscode.workspace.textDocuments.find((d) => d.fileName.endsWith('scene.py'));
    assert.ok(tab, 'no scene.py document is open');
    assert.match(tab.getText(), /^import magpylib as magpy/m);
    assert.match(tab.getText(), /for i in range\(1, n\)/, 'the pattern should export as a loop');

    // Saving it rebuilds the scene from what it says: change a variable in
    // the text, save, and the document should come back with the new value.
    const edited = tab.getText().replace(/^radius = [\d.]+$/m, 'radius = 3.25');
    assert.notStrictEqual(edited, tab.getText(), 'radius assignment not found');
    const editor = await vscode.window.showTextDocument(tab);
    await editor.edit((builder) => {
      builder.replace(new vscode.Range(0, 0, tab.lineCount, 0), edited);
    });
    await tab.save();

    // The save is applied and the scene rebuilt asynchronously, so wait for
    // the value to arrive rather than for a length of time.
    const doc = await sceneWhere(
      (d) => (d as unknown as { variables: Record<string, number> }).variables?.radius === 3.25,
      'the saved script to reach the document',
    );
    assert.strictEqual(
      (doc as unknown as { variables: Record<string, number> }).variables.radius,
      3.25,
    );
  });

  test('a scene saved to a file opens again as the same scene', async function () {
    this.timeout(60000);
    const before = await loadExample('halbach', 'halbach');

    // The save dialog cannot be driven from a test, so the file is written the
    // way Save writes it and opened through the real command — which is the
    // half that has somewhere to go wrong (reading, parsing, the engine).
    const file = tempScene(`round-trip.magpy.json`);
    await writeJson(file, before);

    await vscode.commands.executeCommand('magpylib-studio.newScene', DISCARD);
    await sceneWhere(nothing, 'the scene to clear');

    await vscode.commands.executeCommand('magpylib-studio.loadScene', file, DISCARD);
    const after = await sceneWhere(holding('halbach'), 'the file to open');
    assert.deepStrictEqual(after.events, before.events, 'the log came back different');
    assert.deepStrictEqual(after.objects, before.objects);
  });

  test('saving a scene that has a file writes to it without asking', async function () {
    this.timeout(60000);
    const fresh = await loadExample('halbach', 'halbach');
    const file = tempScene('save.magpy.json');
    await writeJson(file, fresh);
    await vscode.commands.executeCommand('magpylib-studio.newScene', DISCARD);
    await sceneWhere(nothing, 'the scene to clear');
    await vscode.commands.executeCommand('magpylib-studio.loadScene', file, DISCARD);
    await sceneWhere(holding('r2'), 'the file to open');

    await vscode.commands.executeCommand('magpylib-studio.removeObject', {
      id: 'r2',
      type: 'magnet.Cuboid',
      label: 'Magnet 2',
      parent: 'ring2',
      visible: true,
    });
    await sceneWhere(without('r2'), 'the removal to land');

    // No showSaveDialog here: the scene knows its file. If that ever regresses
    // this test hangs on a modal rather than failing, which is its own signal.
    await vscode.commands.executeCommand('magpylib-studio.saveScene');
    assert.deepStrictEqual(
      await readJson(file),
      await scene(),
      'the file on disk is not what the engine holds',
    );
  });

  test('a saved file says what format and what wrote it', async function () {
    this.timeout(60000);
    const file = tempScene('stamp.magpy.json');
    await writeJson(file, await loadExample('halbach', 'halbach'));
    await vscode.commands.executeCommand('magpylib-studio.loadScene', file, DISCARD);
    await vscode.commands.executeCommand('magpylib-studio.saveScene');

    const saved = await readJson(file);
    assert.strictEqual(typeof saved.version, 'number');
    assert.match(String(saved.generator), /^magpylib-studio /);
    // it reads first, so `head -2` on the file identifies it
    assert.deepStrictEqual(Object.keys(saved).slice(0, 2), ['version', 'generator']);
  });

  test('the scene knows its file, and says so when it drifts from it', async function () {
    this.timeout(60000);
    await loadExample('halbach', 'halbach');
    // an example is a starting point, not a document: no file, and unsaved
    assert.strictEqual(sceneFileState().file, undefined);
    assert.strictEqual(sceneFileState().dirty, true);

    const file = tempScene('state.magpy.json');
    await writeJson(file, await scene());
    await vscode.commands.executeCommand('magpylib-studio.newScene', DISCARD);
    await sceneWhere(nothing, 'the scene to clear');
    await vscode.commands.executeCommand('magpylib-studio.loadScene', file, DISCARD);
    await sceneWhere(holding('r2'), 'the file to open');
    assert.strictEqual(sceneFileState().file, file.toString(), 'the file was not adopted');
    assert.strictEqual(sceneFileState().dirty, false, 'a freshly opened scene is not dirty');

    await vscode.commands.executeCommand('magpylib-studio.removeObject', {
      id: 'r2',
      type: 'magnet.Cuboid',
      label: 'Magnet 2',
      parent: 'ring2',
      visible: true,
    });
    await sceneWhere(without('r2'), 'the removal to land');
    assert.strictEqual(sceneFileState().dirty, true, 'an edit went unnoticed');

    await vscode.commands.executeCommand('magpylib-studio.saveScene');
    assert.strictEqual(sceneFileState().dirty, false, 'saving did not settle it');

    // Redrawing is not editing. Every surface refresh goes through the same
    // path as a real change, so it is one line's difference between "the
    // view is stale" and "your file is out of date" — and the second one
    // nags, and blocks opening anything else.
    await vscode.commands.executeCommand('magpylib-studio.refreshScene');
    await pause(500); // asserting that nothing happens; there is no event to await
    assert.strictEqual(sceneFileState().dirty, false, 'a refresh claimed an edit');
  });

  test('unsaved work is backed up where a reload can find it', async function () {
    this.timeout(60000);
    // The scene lives in a subprocess that dies with the window, so this file
    // is the only copy of anything unsaved. The reload that reads it back is a
    // manual check; that it is written, and correct, is not.
    await loadExample('coil', 'coil');
    const backup = sceneFileState().backup;
    assert.ok(backup, 'no backup location');

    // Wait for the backup to be *written* with this scene; whether it is
    // faithful is the assertion below, not the thing being waited for — a
    // wait that is also the check can only ever time out when it fails.
    const saved = await fileWhere(
      vscode.Uri.parse(backup),
      (doc) => ids(doc.objects as { id: string }[]).has('coil'),
      'the backup to be written',
    );
    assert.deepStrictEqual(saved, await scene(), 'the backup is not the scene');

    // And it restores. This is what activation does with it after a reload,
    // minus the reload — so what stays unverified here is only whether
    // activation reaches for it, not whether reaching for it works.
    await vscode.commands.executeCommand('magpylib-studio.newScene', DISCARD);
    await sceneWhere(nothing, 'the scene to clear');

    await vscode.commands.executeCommand(
      'magpylib-studio.loadScene',
      vscode.Uri.parse(backup),
      DISCARD,
    );
    assert.deepStrictEqual(
      await sceneWhere(holding('coil'), 'the backup to open'),
      saved,
      'the backup did not come back',
    );
  });

  test('a scene from a newer version is refused, leaving the open one alone', async function () {
    this.timeout(60000);
    const intact = await loadExample('halbach', 'halbach');

    const file = tempScene('from-the-future.magpy.json');
    await writeJson(file, { version: 99, events: [], objects: [] });
    await vscode.commands.executeCommand('magpylib-studio.loadScene', file, DISCARD);
    await pause(500); // asserting that nothing happens; there is no event to await

    assert.deepStrictEqual(
      (await scene()).events,
      intact.events,
      'a document we cannot read replaced the one we could',
    );
  });
});
