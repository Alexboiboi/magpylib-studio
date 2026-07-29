import * as assert from 'assert';

import * as vscode from 'vscode';

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

/** to_dict, read the way any editor tab would read it. */
async function scene(): Promise<{
  events: { op: string; target: string }[];
  objects: { id: string }[];
}> {
  const doc = await vscode.workspace.openTextDocument(SCENE_JSON);
  return JSON.parse(doc.getText());
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
    await vscode.commands.executeCommand('magpylib-studio.loadExample', 'halbach');
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
    await vscode.commands.executeCommand('magpylib-studio.loadExample', 'halbach');

    // the tree hands the command an object; a test can hand it the same thing
    await vscode.commands.executeCommand('magpylib-studio.removeObject', {
      id: 'r1',
      type: 'magnet.Cuboid',
      label: 'Magnet 1',
      parent: 'ring1',
      visible: true,
    });
    await new Promise((resolve) => setTimeout(resolve, 500));

    const ids = new Set<string>();
    const walk = (specs: { id: string; children?: unknown[] }[]) => {
      for (const spec of specs) {
        ids.add(spec.id);
        walk((spec.children ?? []) as { id: string; children?: unknown[] }[]);
      }
    };
    walk((await scene()).objects as { id: string; children?: unknown[] }[]);
    assert.ok(!ids.has('r1'), 'the magnet is still in the document');
    assert.ok(ids.has('r2'), 'the other ring should be untouched');
  });

  test('the script tab renders the scene and applies on save', async function () {
    this.timeout(60000);
    await vscode.commands.executeCommand('magpylib-studio.loadExample', 'halbach');
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
    await new Promise((resolve) => setTimeout(resolve, 1500)); // apply + rebuild

    const doc = await scene();
    assert.strictEqual(
      (doc as unknown as { variables: Record<string, number> }).variables.radius,
      3.25,
      'the saved script did not reach the document',
    );
  });
});
