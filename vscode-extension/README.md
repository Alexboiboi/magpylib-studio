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
  dipole, sensor, collection), then set each of its properties — prefilled
  with defaults and labelled with units — and it opens selected in the
  Inspector.
- **Editing in the tree** — Rename (`F2`, `Enter` on macOS), Copy / Cut /
  Paste (`Cmd/Ctrl+C` / `X` / `V`) and Delete (`Delete`, `Cmd+Backspace`)
  while the Scene view has focus, plus the same items on right-click. A copy
  follows magpylib's own convention (`Cube` → `Cube_01` → `Cube_02`) and
  copies a collection's whole subtree; pasting onto a collection puts the
  object inside it. The eye icon on hover hides an object from the 3D view
  (dimmed in the tree, like toggling a plotly trace) — hidden sources still
  count in field calculations.
- **Right-click → Transform** — Set Position…, Move By…, Rotate… (spin or
  orbit the origin), Clear Path. Move/Rotate accept `× N` at the end of the
  value to spread the operation over an N-step animation path, e.g. `360 × 36`.
- **Field maps** — the Field panel's *Plane map* mode: a heatmap of |B| (or a
  signed component) on the xy/xz/yz plane at any offset, with a log option
  because field spans orders of magnitude. Colour follows the data's job —
  one hue light→dark for magnitude, blue↔grey↔red anchored at zero for signed
  components — and the axes are locked 1:1 so geometry is not distorted.
  Right-click a Sensor → **Transform → Pixel Grid…** builds magpylib's own
  pixel grid instead: the measurement plane is then a real object, visible in
  the 3D view, tilting with the sensor and exported in the script.
- **Transforms** — the Inspector's `transform` section: absolute position and
  rotation-vector fields, relative *rotate* (with "orbit origin" for
  Halbach-style arrangements) and *move*, each with a step count that turns
  the operation into an animation **path**, plus *Clear path*. Same
  operations in chat: `#magpyMove`, `#magpyRotate`, `#magpyPose`.
- **Properties** — the Inspector's `properties` section: the object's physics
  parameters (polarization, dimension, diameter, current, moment, vertices,
  pixels) as numeric widgets, with units in the tooltips; matrices like
  polyline vertices are edited as JSON. Edits go through `set_param`, so
  transforms and styles are preserved.
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

## Try it out

**1. Install the engine.** The extension is only the UI; it spawns
`python -m magpylib_studio`, so any interpreter with the engine installed works
(Python ≥ 3.11):

```sh
python3 -m venv ~/magpylib-studio-venv
~/magpylib-studio-venv/bin/pip install \
  "magpylib-studio @ git+https://github.com/Alexboiboi/magpylib-studio.git"
```

That pulls released magpylib and works fully. The [property-tree branch][branch]
is optional and adds path-valued physics properties (`current=[100, 200, 300]`):

```sh
~/magpylib-studio-venv/bin/pip install \
  "magpylib @ git+https://github.com/magpylib/magpylib@feat/improve-style"
```

**2. Run the extension**, either way:

*From source* — clone the repo, then:

```sh
cd vscode-extension
npm install
npm run compile          # or: npm run watch
```

Open **this folder** in VS Code and press `F5`; a second window opens (the
Extension Development Host) with the extension loaded.

*From a package* — build a `.vsix` and install it into your normal VS Code:

```sh
npx @vscode/vsce package
code --install-extension magpylib-studio-vscode-0.0.1.vsix
```

**3. Point it at the interpreter.** Settings → `magpylib-studio.pythonPath` →
`~/magpylib-studio-venv/bin/python`. Skip this only if your workspace has a
`.venv` with the engine in it. If nothing usable is found you get an error with
an *Open Settings* button, not a silent failure.

**4. Open it.** Click the magpylib icon in the activity bar and press
**Load Example Scene** — two Halbach rings and a sensor path. From there: click
objects in the tree to inspect and edit them, open the Field view for the map,
and (with GitHub Copilot installed) ask chat things like
`make the cube green #magpyEdit` or `what is B at the bore centre? #magpyField`.

[branch]: https://github.com/magpylib/magpylib/tree/feat/improve-style

## Python interpreter resolution

1. `magpylib-studio.pythonPath` setting, if set;
2. `.venv/bin/python` (or `Scripts\python.exe`) in a workspace folder, then in
   the repo root next to this folder;
3. `python3` on PATH.

The probe checks each candidate can actually `import magpylib_studio`, so a
workspace `.venv` without the engine cannot shadow a working interpreter.

## Notes

- plotly.js is bundled from `plotly.js-dist-min` (3.x, matching Python
  plotly 6.x) and loaded via a webview URI — no CDN, works offline, CSP-clean.
- Engine stderr goes to the "Magpylib Studio Engine" output channel.
