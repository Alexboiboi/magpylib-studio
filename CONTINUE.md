# magpylib-studio — continuation notes

Handoff for resuming in a fresh VS Code session. Read this + `README.md` first.

## What this is

The framework-agnostic **engine** for a magpylib GUI + LLM "studio", initially
targeting a **VS Code extension**. It owns a magpylib scene and exposes
everything a frontend needs over newline-delimited **JSON-RPC on stdio**. The
extension (TypeScript, not written yet) will be a thin shell that spawns this
and drives it.

## Current state — DONE & tested

- `magpylib_studio/session.py` — `MagpylibStudioSession`: `list_objects`, `get_schema`,
  `get_values` (set vs resolved), `get_figure` (plotly JSON), `apply_edit`,
  `add_object`, `remove_object`, `set_param` (move/resize/repolarize),
  `reset_style` (drop from doc + rebuild; the property tree has no unset),
  `load_scene` (dict or JSON file path), `load_example`, `clear_scene`,
  `batch` (list of mutating ops in one call, continues past failures,
  per-op results), `to_dict`, `to_script`. **Sessions start empty**.
  **Nested collections**: specs with `type: "Collection"` carry a recursive
  `children` list; `add_object(parent=...)` nests, `move_object` reparents
  (cycle-checked), removing a Collection removes its subtree; `list_objects`
  is depth-first with a `parent` field; `to_script` emits children before
  their collection. `example_scene()`: `halbach` → `ring1`/`ring2` (10
  rotated cuboids each; ring2 staggered 18° by a *group* rotation) + sensor
  path along the bore. Object specs support an optional `"rotations"` list
  ({angle, axis, anchor?}, applied in order via `rotate_from_angax`; no
  anchor = spin in place; on a Collection rotates the whole group).
  Structural edits go through `_mutate_doc`: mutate doc → rebuild scene; on
  failure the old doc is restored and the error reported (`{"ok": false}`).
- **Field evaluation**: `get_field(sensor_id?, points?, field?)` — summed
  B/H of all leaf sources (SI units) along a sensor path or explicit points
  (numeric, for `#magpyField`). `get_field_figure(output?, animation?,
  template?)` delegates to **magpylib's own 2D rendering**
  (`show(output="B"|"Bx"|...)`) — field at the scene's sensors along their
  paths, animatable. Shown in a dedicated on-demand **Field panel**
  ($(graph-line) icon / "Open Field View") with an output selector, not
  embedded in the 3D view.
- **Script import**: `magpylib_studio/importer.py` + `load_script(path,
  scene?)` — run an existing magpylib script with **show() intercepted**:
  each show() call the script makes is captured as a scene candidate (what
  its author considered "the scene"), plus an "all script objects" fallback
  when that differs; candidates cached, `load_captured(scene)` switches
  (each load one undoable step). **Orientation paths** import exactly via
  the second rotations-entry form `{"rotvec": [[x,y,z],...], "start": 0}`
  (rotate_from_rotvec, elementwise over the path); **path-valued
  properties** (polarization/current/… — from the branch improve-style is
  based on) round-trip through constructor params untouched.
  Extension: "Import Python Script…" command, right-click a `.py` → "Open in
  Magpylib Studio", welcome link, and a post-import "Switch Scene…" prompt +
  "Switch Imported Scene…" palette command when several candidates exist.
- `magpylib_studio/rpc.py` — JSON-RPC stdio loop (`serve`), method allow-list.
- `magpylib_studio/__main__.py` — `python -m magpylib_studio`.
- `tests/test_session.py` — 29 tests, **all green**, ruff clean (`uvx ruff check`).
- Verified: real subprocess driven through pipes; scene document round-trips
  through rebuild; `to_script()` emits code that executes and reproduces edits;
  invalid edits are reported (`{"ok": false, "error": ...}`) not raised.
- `vscode-extension/` — the TS shell, **compiles clean, smoke-tested**:
  - `src/engineClient.ts` — promise-based RPC client (spawns the engine, owns
    the request-id space, line-buffered stdout, rejects on engine exit).
  - `src/extension.ts` — `openStudio` command → webview (bundled plotly.js 3.x
    via webview URI + CSP nonce, `uirevision` holds the camera, object picker,
    schema + set-values panes, manual edit form) and six **`vscode.lm`
    Language Model Tools**: `#magpyObjects`, `#magpySchema`, `#magpyEdit`,
    `#magpyAdd`, `#magpyRemove`, `#magpyParam` (successful edits auto-refresh
    the panel). One shared engine process. Tool names declared in package.json
    must exactly match those registered in `registerLmTools`.
  - `src/sceneTree.ts` + activity-bar **Scene view** (`media/magnet.svg`,
    drawn after the magpylib logo — magnet/magpie/chip silhouette; the
    activity bar renders it as an alpha mask): clickable tree of scene
    objects; context menu Remove Object / Reset Style. Flat tree mirroring
    the flat document — `getChildren` is shaped for nesting later.
  - `src/inspectorView.ts` — **Inspector** sidebar webview view: schema-driven
    widgets (enum → dropdown, format:color → picker+text, bounded number →
    slider, boolean → tri-state '(default)'/true/false), resolved values
    prefilled, set paths bold + ↺ per-path reset (`reset_style`), filter box.
    '(default)' / empty input resets the path. Skips free-form specs
    (`model3d.data`, `path.frames`).
  - Layout: tree click → host `selectedObjectId` → inspector loads it; the
    Studio panel is **plot-only** now (with an "Animate paths" toggle; plot
    template follows the VS Code theme). `broadcastMutation()` (debounced
    150 ms) refreshes plot + tree + inspector + virtual docs after every edit
    from any surface (inspector widgets, LM tools, tree commands).
  - **Script/scene I/O**: read-only virtual docs `magpylib-studio:/scene.py`
    (to_script) and `/scene.json` (to_dict) that live-update on every edit;
    commands View Python Script ($(code) icon on the Scene view), Save Scene
    As… (.py or .json via extension), Load Scene from File… (JSON; also
    linked in the empty-view welcome). Script → doc import stays deferred.
  - Python resolution: `magpylib-studio.pythonPath` setting → workspace/.venv →
    repo-root/.venv → `python3`. Engine stderr → output channel.
  - Verified via `node` smoke test driving compiled `EngineClient` against the
    real engine: all methods, invalid-edit rejection, unknown-method rejection.
  - NOT yet done: run inside an actual Extension Development Host (F5) —
    needs a human with VS Code; everything below the vscode API is tested.

## Setup (this folder)

```sh
uv venv --python 3.13 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -e ../magpylib   # REQUIRED: see below
VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Already set up: `.venv/` exists with the above installed. Git repo on `main`.
For the extension: `cd vscode-extension && npm install && npm run compile`
(node installed via Homebrew).

## CRITICAL dependency

Depends on the **property-tree branch of magpylib**, installed editable from the
sibling repo `../magpylib` (branch `feat/improve-style`). That branch adds the
`PropertyNode` API this engine relies on:
`schema()`, `set(path, value)`, `observe()`, `is_set`/`set_values`, `merged()`,
plus `get_style`. Released PyPI magpylib does NOT have these. If imports fail,
check that `../magpylib` is on `feat/improve-style` and installed `-e`.

## Key design decisions (keep these)

1. **The scene document is the source of truth.** Every edit updates the live
   object AND `self.doc`, so `to_script()` always reflects current state →
   **git is the durable history**. (An *in-session* undo/redo stack of doc
   snapshots exists for quick reverts — `undo`/`redo`/`get_history`, batch =
   one step, Cmd+Z in the panels, `#magpyUndo` — it complements git, it does
   not replace it.)
2. **`schema()` is the one contract** — the same JSON Schema drives the
   frontend inspector widgets AND the LLM tool's `input_schema`.
3. **Shared validation** — every edit goes through `style.set`, validated by the
   property tree; bad edits are reported so a GUI shows an error and an LLM
   self-corrects. No second validation layer.
4. **Document canonical, script generated** (not AST-parsed). The reverse now
   exists as a pragmatic bridge: `load_script(path)` **executes** the user's
   script (show() patched out) and introspects the live objects into a
   document (`importer.py`) — variable names → ids, Collections keep nesting,
   orientation → one `rotations` entry. Parametric structure flattens (loops
   arrive as concrete objects). True AST parsing stays deferred on purpose.

## Reference material (in ../magpylib, branch feat/improve-style)

- `__temp_solara_app.py` — a WORKING Solara POC of the same GUI+LLM idea:
  schema-driven inspector + live plotly view + `claude-opus-5` chat editing the
  same style via `set_property`, with undo. Good reference for the frontend +
  the LLM tool-loop pattern (manual tool loop, `output_config={"effort":"low"}`).
- `src/magpylib/_src/defaults/property_tree.py` — the descriptor core
  (`PropertyNode`, `schema()`, `observe()`, `merged()`, ...).
- `src/magpylib/_src/style.py` — the ported style classes + `get_style`.
- Memory: the property-tree refactor rationale is in the magpylib repo's
  Claude memory (`style-property-tree-refactor`).

## Next steps (pick one)

- **Try it live**: open `vscode-extension/` in VS Code, F5, run
  "Magpylib Studio: Open Studio"; in Copilot chat try `make the cube green
  #magpyEdit` or `add a green sphere at [0,2,0] #magpyAdd`.
- **Tree drag & drop** (`TreeDragAndDropController` → `move_object`) and a
  "New Collection / move to..." context menu.
- **Field maps**: `get_field` covers points/paths; a 2D plane-slice heatmap /
  streamline figure would complete the analysis story.
- **Package a .vsix** (vsce) once features settle, for real installs.
- **Chat Participant `@magpy`** if richer chat UX than plain tools is wanted.

## Gotchas

- The `get_figure` result is `json.loads(fig.to_json())` — plotly's encoder
  handles numpy/bdata; don't use `to_plotly_json()` (leaves numpy in, not
  JSON-safe).
- Style paths are **dotted** (`magnetization.arrow.width`); `to_script` nests
  them for the `style=` kwarg. Constructor `style=` needs nested, not dotted.
- LLM: for a VS Code extension prefer `vscode.lm` (Copilot, zero key). Use the
  Anthropic SDK path from the Solara POC only if you specifically want Claude
  and are OK managing keys/chat UI.
