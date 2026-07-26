# Magpylib Studio VS Code Extension

VS Code shell for the headless `magpylib-studio` engine (the Python package in
the repo root). It spawns `python -m magpylib_studio` and talks
newline-delimited JSON-RPC over stdio.

## What it provides

- **Command** `Magpylib Studio: Open Scene View` — the "Magpylib Scene" tab:
  the live plotly 3D view
  (camera held across edits via `uirevision`). Just the plot: selection and
  editing live in the sidebar.
- **Scene view** — the magpylib-logo icon in the activity bar opens a sidebar
  tree of the scene's objects, one icon per type (magnets red, currents blue,
  sensors green). Clicking an object opens/reveals the plot and loads it in
  the Inspector; right-click offers *Remove Object* and *Reset Style*. The
  scene starts **empty**: the view shows a **Load Example Scene** button
  (Halbach ring + coil pair + sensor). Flat for now — it mirrors the engine's
  single-collection document.
- **History view** — the session timeline, newest first: every scene change
  as a checkpoint (the current one marked, future ones dimmed). Click any
  entry to jump the scene there, both backwards and forwards; the timeline
  itself stays put. Undo/redo icons sit on the Scene and History view
  titles (Cmd+Z / Cmd+Shift+Z still work inside the panels).
- **Add Object…** — the `+` icon on the Scene view (or right-click a
  collection to create inside it, or the empty-view welcome): pick a type
  (cuboid, cylinder, segment, sphere, tetrahedron, current loop/polyline,
  dipole, sensor, collection) and it is created with sensible defaults.
- **Right-click → Transform** — Set Position…, Move By…, Rotate… (spin or
  orbit the origin), Clear Path. Move/Rotate accept `× N` at the end of the
  value to spread the operation over an N-step animation path, e.g. `360 × 36`.
- **Transforms** — the Inspector's `transform` section: absolute position and
  rotation-vector fields, relative *rotate* (with "orbit origin" for
  Halbach-style arrangements) and *move*, each with a step count that turns
  the operation into an animation **path**, plus *Clear path*. Same
  operations in chat: `#magpyMove`, `#magpyRotate`, `#magpyPose`.
- **Inspector view** — below the tree: schema-driven widgets generated from
  `get_schema()` (enum → dropdown, `format: color` → color picker + text,
  bounded number → slider, boolean → tri-state with "(default)"), prefilled
  with resolved values. Explicitly set paths are bold with a ↺ reset button;
  a filter box narrows the property list. Edits go through `apply_edit` /
  `reset_style`, so magpylib validation errors show inline.

All three surfaces (plot, tree, inspector) refresh automatically after any
edit, whatever its origin — inspector widget, chat tool, or tree context menu.
- **Language Model Tools** (native Copilot chat, no API key):
  `#magpyObjects` (list scene objects), `#magpySchema` (style JSON Schema for
  an object), `#magpyEdit` (set one dotted style path, validated by magpylib),
  `#magpyAdd` / `#magpyRemove` (add/remove scene objects), `#magpyParam`
  (constructor params: move, resize, repolarize), `#magpyClear` (empty the
  scene in one call), `#magpyBatch` (many operations in one call — the tool
  descriptions steer the model to it for multi-object work). Edits
  auto-refresh all surfaces, debounced so bursts redraw once.

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
In the Extension Development Host run the `Magpylib Studio: Open Scene View`
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
- Engine stderr goes to the "Magpylib Studio Engine" output channel.
