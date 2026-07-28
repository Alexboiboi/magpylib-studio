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
- **Clipboard & visibility**: `copy_object(id, parent?)` duplicates a spec
  (subtree included) with magpylib's label convention (`Cube_01`, `Cube_02`)
  and unique ids; `set_visible(id, bool)` hides via magpylib's own switches
  (`_HIDE_STYLE` = `model3d.showdefault: False` + `path.show: False`, applied
  to leaf specs, prior values kept in `hidden_style` for exact restore) —
  **not** by dropping objects from the figure, so the object keeps its slot in
  magpylib's colour sequence and nothing else is recoloured. Hidden sources
  still contribute to `get_field`. Tree: rename
  (F2/Enter), copy/cut/paste (Cmd+C/X/V), delete, and an inline eye toggle;
  hidden items show an eye-closed icon and "· hidden".
- **Physics properties**: `get_params(object_id)` introspects the live object
  for the editable constructor params (polarization, dimension, diameter,
  current, moment, vertices, faces, pixel) with value, kind
  (scalar/vector/matrix) and a doc string; written back via `set_param`.
  Position/orientation are excluded — they are transform-managed. Shown as
  the Inspector's **properties** section, and prompted (prefilled, with
  units) when adding an object.
- **Transforms — the doc records magpylib CALLS, not derived poses**, and
  since the event-log change they live in **one ordered log for the whole
  document**, `doc["events"]`, not per object. Each event is
  `{id, target, op, ...}` with op in (`move`, `rotate_from_angax`,
  `rotate_from_rotvec`, `position`, `orientation`); `_build` constructs every
  object and then folds the log over them in order, so magpylib still owns
  all semantics: paths, anchors, `start`, and **a Collection transform
  carrying its whole subtree**. Objects can be constructed up front because a
  Collection's *constructor* does not move the children handed to it — only
  its position/orientation setters do, and those are events like any other.
  Legacy docs (per-object `transforms`/`rotations`) fold into the log on load
  via `_migrate_events`, children before parents, which is the order the old
  per-object build replayed them in — verified pose- and field-identical on
  the example scene.
  Session API: `move`, `rotate`, `set_transform` (absolute WORLD pose —
  recorded at the end of the log, so **no parent-frame correction is needed**
  and the old `_parent_frame` probe is gone), `clear_path`, `get_transform`.
  Undoable, batchable, exported verbatim by `to_script`. UI: Inspector
  **transform** section, Scene-tree **inline hover icons** (move, rotate, +
  on collections) and a **Transform** submenu; move and rotate first ask
  *single step or N-step path*. LM tools `#magpyMove`, `#magpyRotate`,
  `#magpyPose`.
- **The log is editable — this is the ANSYS-style history, engine side only
  so far.** `get_events()` lists it with a rendered `source` line per event;
  `edit_event(id, changes)`, `remove_event(id)`, `move_event(id, index)`
  mutate it. Editing an early event re-applies every later one for free,
  because `_build` always folds the whole log (1.7 ms for the 24-object
  example) — no invalidation machinery, unlike a solver-backed tool where
  re-running is expensive. Events that cannot replay (unknown target, bad
  axis) roll back through `_mutate_doc` and are reported. `remove_object`
  drops its subtree's events, `copy_object` clones them onto the new ids.
  **Not yet:** any dedicated UI for it (the script tab is the editor).
- **Variables + expressions** (`magpylib_studio/expressions.py`):
  `doc["variables"]` holds numbers or expressions over each other, and any
  value in a param or event field may be one. The rule is spreadsheet-style:
  a string starting with `=` is an expression, anything else is a literal —
  so `"z"` stays an axis name and `"=360/n"` is arithmetic, with no per-field
  whitelist. Evaluated from the AST against an allow-list (arithmetic, a
  handful of math functions, `pi`/`e`/`tau`), never `eval` — a document is
  something you open from someone else. Expressions are stored in canonical
  spacing so the script tab is a fixed point from the first save. API:
  `get_variables`, `set_variable`, `remove_variable`; a definition that
  cycles, is unknown, or that some object rejects rolls back.
  `to_script` emits them as real Python assignments, so the exported script
  is parametric too.
- **Sweeps**: `sweep(variable, values, sensor_id?/points?, field?)` re-folds
  the document once per value and reads the field; `get_sweep_figure(...)`
  plots it (one hue light→dark over observation points — same quantity in
  different places, not unrelated series). Nothing is recorded in history and
  the document ends on the value it started on. This is what variables are
  *for*, and it is only affordable because a rebuild is milliseconds.
- **Generator events** — `duplicate_around(object_id, count, axis?, anchor?,
  spin?)` records ONE event standing for N copies evenly spaced about an
  axis, each optionally spun by `spin`×index (a Halbach ring is
  `spin = 360/count`). `count` and `spin` may be expressions, so the whole
  arrangement is one number to edit. The copies are generated at build time,
  registered in `_objs` (so they are real field sources and real geometry)
  and reported by `list_objects` with a `derived` key naming their source —
  they have no spec, so they are not individually editable. The source must
  sit in a Collection: that is where the copies go, and it is what lets the
  event export as plain runnable magpylib (`for i in range(1, n): …copy()…`)
  — a loop shape `parse_script` reads straight back into the event.
- **Field maps**: `get_field_map(plane?, offset?, component?, log?, sensor_id?)`
  — plotly heatmap on a plane. Colour by job (dataviz skill): sequential
  one-hue blue for magnitude, diverging blue↔grey↔red with `zmid=0` for signed
  components, never a rainbow; axes locked 1:1; `log` for the orders-of-
  magnitude falloff. With `sensor_id` it reads a **Sensor's pixel grid**
  instead (`set_pixel_grid(id, plane, size, resolution)`) — magpylib's own
  mechanism, so the plane is a real scene object that tilts with the sensor
  and exports to the script. Sensor paths add a leading dimension to `getB`;
  the map uses the last path step.
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
    objects; context menu Move to… / New Collection… (on collections; also
    view-title overflow) / Remove Object / Reset Style. **Drag & drop**
    reparents (`TreeDragAndDropController` → `move_object`): onto a
    Collection = move in, onto a plain object = move next to it, onto empty
    space = move to scene root; cycles rejected by the engine.
  - `src/inspectorView.ts` — **Inspector** sidebar webview view: schema-driven
    widgets (enum → dropdown, format:color → picker+text, bounded number →
    slider, boolean → tri-state '(default)'/true/false), resolved values
    prefilled, set paths bold + ↺ per-path reset (`reset_style`), filter box.
    '(default)' / empty input resets the path. Skips free-form specs
    (`model3d.data`, `path.frames`).
  - **Variables view** (`variablesView.ts`, collapsed by default, welcome
    message when empty): one row per variable showing what it was written as
    *and* what it resolved to — which is what you want when a scene lands
    somewhere unexpected. Click a row to edit, `+` to add, inline trash to
    remove. The input box takes `2.5` or `gap*2`; the `=` marker the document
    uses is added for you.
  - **Every numeric field takes an expression**, not just the variables view:
    inspector properties and the transform pose (text inputs now — a number
    input cannot hold `gap*2` at all), the relative rotate/move fields, the
    Add Object prompts, Set Position…, Move By…, Rotate…. Expression fields
    render italic/blue and show the resolved value on hover. Multi-step paths
    still require numbers, because the UI divides the total across the steps.
    For this to be safe `get_params` and `get_transform` report the value **as
    written** (`written`, `written_position`, `written_orientation`) beside
    the resolved one — an editor showing only the resolved number would
    replace the expression the moment the user touched a neighbouring axis.
    `get_transform` falls back to the constructor param when no event pinned
    the pose, and `set_transform` records an expression as written instead of
    resolving it to a pose.
  - **Naming a variable creates it.** Type `a, a, 2*a` into a new cuboid's
    dimension and the studio asks for `a` before storing anything, then adds
    it to the stack — writing a name is how you say "and let me set this".
    `unknown_variables(values)` reports what a value refers to but the
    document does not define (functions and `pi`/`e`/`tau` are not variables,
    and the `=` marker keeps ordinary strings like `'z'` out of it);
    `ensureVariablesDefined` in the extension does the asking. It sits in
    `mutateFromTree` and in the inspector's request channel — a webview
    cannot raise an input box, so the ask happens on the way through — which
    covers every prompt, tree command and inspector field at once. Backing
    out of the prompt abandons the whole edit. LM tools deliberately bypass
    it: there is no one to ask, so Copilot gets the error and fixes it. **Sweep a Variable…** ($(graph-line) in the same
    title bar) asks for from/to/steps and drives the Field panel's third
    mode, "Against a variable" (`get_sweep_figure`). **Duplicate Around…**
    is in the scene tree's Transform submenu.
  - **Generated copies are inert in the tree**: they come back from
    `list_objects` with a `derived` key, and get `contextValue =
    'derivedCopy'` — deliberately outside the `magpy*` namespace that all 14
    scene-view menu entries are gated on, so none of them match. They are
    also not selectable and not draggable. Without that they rendered as
    ordinary objects whose every command failed on the engine (and whose
    inspector *read* fine, since `get_params` reads the live object, so they
    looked editable and silently weren't).
  - Layout: tree click → host `selectedObjectId` → inspector loads it; the
    Studio panel is **plot-only** now (with an "Animate paths" toggle; plot
    template follows the VS Code theme). `broadcastMutation()` (debounced
    150 ms) refreshes plot + tree + inspector + virtual docs after every edit
    from any surface (inspector widgets, LM tools, tree commands).
  - **Script/scene I/O**: read-only virtual doc `magpylib-studio:/scene.json`
    (to_dict) that live-updates on every edit; commands Edit Python Script
    ($(code) icon on the Scene view), Save Scene As… (.py or .json via
    extension), Load Scene from File… (JSON; also linked in the empty-view
    welcome).
  - **The script tab is editable both ways**: it is a real file in extension
    storage (a content provider has no write side), regenerated from the scene
    on every edit and applied back with `apply_script` on save — one undo step
    labelled "edit script". Edits are never clobbered while the buffer is
    dirty or while it holds text the engine rejected. `to_script` deliberately
    emits no wrapper Collection (`magpy.show(a, b, …)`), and the importer names
    nested children from script variables, so script → doc → script is an
    identity on ids and structure. **Two ways in, reported as `mode`:**
    *parsed* — the file is still in the shape `to_script` emits, so
    `importer.parse_script` reads it as source: variables, event order and
    group transforms all survive, literals keep the form they were written
    in, and the whole document round-trips byte-identically (verified on the
    24-object example). *executed* — anything else (a loop, a helper, numpy)
    is run and introspected, which cannot see how the scene was written, so
    it warns about what it flattened. The script tab is therefore also the
    **only UI variables and duplicate events have**: you write them as
    Python, save, and they land in the document.
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

## magpylib versions — BOTH work

`magpylib_studio/style_compat.py` abstracts the only four branch-specific APIs
(`schema()`, `set(path,value)`, `set_values()`, resolved `get_style`):

- **Released magpylib (≥5.2)** — `pip install magpylib-studio` and done. The
  shim rebuilds set/set_values from `style.update()` / `style.as_dict()`
  (diffing a pristine style so defaults don't pollute the doc), and serves
  `style_schemas.json` — the branch's schema, generated once — as the schema.
  The two style trees match on 32 of 33 paths, so the inspector keeps real
  widgets. **Regenerate that file** (see its generation snippet in git history)
  if the branch's style tree changes.
- **Property-tree branch** (`feat/improve-style`, on the *official* magpylib
  repo) — adds path-valued physics properties (`current=[100,200,300]`), the
  only feature the released version lacks.

Run the suite against both: `.venv/bin/python -m pytest -q` (branch) and a
second venv with released magpylib (49 passed, 1 skipped — the property-path
test skips via `supports_property_paths()`).

## Key design decisions (keep these)

1. **The scene document is the source of truth.** Every edit updates the live
   object AND `self.doc`, so `to_script()` always reflects current state →
   **git is the durable history**. (An *in-session* undo/redo stack of doc
   snapshots exists for quick reverts — `undo`/`redo`/`get_history`/
   `goto_history(index)`, batch = one step, Cmd+Z in the panels, undo/redo
   icons + a clickable **History view** in the sidebar, `#magpyUndo` — it
   complements git, it does not replace it.)
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

- **Run it in an Extension Development Host** — overdue. The document schema
  changed twice (events, variables) and the UI gained a view, three commands
  and a Field-panel mode, all verified only by `tsc`, the engine test suite
  and a node smoke test against the real engine. F5 and check: the Variables
  view edits and re-renders, Duplicate Around… produces inert `m#1…` rows,
  the sweep plots, and the script tab still applies on save.
- **LM tools for the parametric surface** — `#magpyVar`, `#magpySweep`,
  `#magpyDuplicate` would let Copilot build parametric scenes; today it can
  only place concrete objects.
- **Units are still absent, deliberately** — everything is bare SI, as
  magpylib wants. If ANSYS-style `5mm` values are ever wanted, that is a
  layer over `expressions.py`, and it needs deciding before variables get
  used widely enough that migrating them hurts.
- **Not events yet**: creation, deletion and reparenting still mutate the
  document directly; only transforms and duplicates are logged. Making them
  events would let the whole scene be reconstructed from the log alone, but
  nothing needs it yet.
- **Optimisation** on top of `sweep()` (find the gap that flattens the field)
  is a small step now that a rebuild-and-measure loop exists.
- **Try it live**: open `vscode-extension/` in VS Code, F5, run
  "Magpylib Studio: Open Scene View"; in Copilot chat try `make the cube green
  #magpyEdit` or `add a green sphere at [0,2,0] #magpyAdd`.
- **Click-to-select in the 3D view** — needs solving magpylib's merged traces
  first (one mesh per collection, so hit-testing needs per-object rendering or
  a vertex-range → object map). Spike before promising.
- **Package a .vsix** (vsce) once features settle, for real installs — also
  needs an install story for the engine (unreleased magpylib branch).
- **No TypeScript tests** — ~1.5k lines verified only by `tsc` + manual F5;
  a `@vscode/test-electron` harness would cover tree/clipboard/commands.
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
