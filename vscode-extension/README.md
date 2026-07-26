# magpylib Studio VS Code Extension

VS Code shell for the headless `magpylib-studio` engine (the Python package in
the repo root). It spawns `python -m magpylib_studio` and talks
newline-delimited JSON-RPC over stdio.

## What it provides

- **Command** `magpylib Studio: Open Studio` — a webview with the live plotly
  scene (camera held across edits via `uirevision`), an object picker, the
  style JSON Schema, the currently set values, and a manual path/value edit
  form.
- **Language Model Tools** (native Copilot chat, no API key):
  `#magpyObjects` (list scene objects), `#magpySchema` (style JSON Schema for
  an object), `#magpyEdit` (set one dotted style path, validated by magpylib),
  `#magpyAdd` / `#magpyRemove` (add/remove scene objects), `#magpyParam`
  (constructor params: move, resize, repolarize). Successful edits
  auto-refresh the open Studio panel.

Both the webview and the LM tools share one engine process
([src/engineClient.ts](src/engineClient.ts) — promise-based RPC client that
owns the request-id space).

## Development

```sh
cd vscode-extension
npm install
npm run compile     # or: npm run watch
```

Then open **this folder** in VS Code and press `F5` (launch config included).
In the Extension Development Host run the `magpylib Studio: Open Studio`
command, or ask Copilot chat e.g. `make the cube green #magpyEdit`.

## Python interpreter resolution

1. `magpylib-studio.pythonPath` setting, if set;
2. `.venv/bin/python` (or `Scripts\python.exe`) in a workspace folder, then in
   the repo root next to this folder;
3. `python3` on PATH.

The engine requires magpylib from the `feat/improve-style` branch (see repo
root `CONTINUE.md`); the repo-root `.venv` has it installed.

## Notes

- plotly.js is bundled from `plotly.js-dist-min` (3.x, matching Python
  plotly 6.x) and loaded via a webview URI — no CDN, works offline, CSP-clean.
- Engine stderr goes to the "magpylib Studio Engine" output channel.
