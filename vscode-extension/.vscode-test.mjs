import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineConfig } from '@vscode/test-cli';

/**
 * Tests that run inside a real Extension Development Host, which is the only
 * place the vscode API exists. Everything below that API is covered by the
 * Python suite; everything above it — activation, commands, the virtual
 * document, the script tab — was covered by nothing until now.
 *
 * The workspace is the repo root because that is where the engine's .venv is:
 * the extension finds its interpreter by looking there.
 */

// VS Code opens a unix socket under its user-data dir, and a unix socket path
// cannot exceed 103 characters. The default puts it inside this folder, which
// is already deep enough here to blow the limit — so it goes in the system
// temp dir, where the path is short whatever the repo is called.
const userDataDir = mkdtempSync(join(tmpdir(), 'vsct-'));

export default defineConfig({
  files: 'out/test/**/*.test.js',
  workspaceFolder: '..',
  launchArgs: ['--user-data-dir', userDataDir],
  mocha: {
    ui: 'tdd',
    timeout: 60000, // the engine spawns Python; the first run downloads VS Code
  },
});
