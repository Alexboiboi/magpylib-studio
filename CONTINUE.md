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
  path along the bore — and **three more scenes beside it** (`EXAMPLES`,
  `list_examples()`, `load_example(name)`), because an example is the
  shortest documentation there is and each one leans on a different feature:
  a **solenoid** (linear pattern of current loops), a **facing pair** (mirror),
  a **magnet array** (two linear patterns composing into a grid), a
  **parametric measuring plane** (a pixel grid whose coordinates are
  expressions), and **a turning magnet under field arrows** — magpylib's own
  animated-quiver example: a pose that is a *path*, so the scene animates,
  and a sensor styled to draw its own reading
  (`pixel.field.symbol = arrow3d`).
  All four are **written the way the studio is meant to be used**: the Halbach
  is two rings of one magnet and one circular pattern each — nine steps and
  four variables instead of twenty declared magnets and sixty-five steps, for
  a field identical to the hand-built version it replaced. Changing `n`
  rebuilds both rings and `stagger` follows it, being `360/(2*n)` by
  definition. The soft minimum on `radius` is 1.6 for a real reason: below
  it, 2πr < n and the default ring's unit cubes would have to overlap.
  Object specs support an optional `"rotations"` list
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
- **The document IS the log.** `doc["events"]` is the whole scene: `create`,
  `remove` and `reparent` alongside the transforms and `duplicate_around`.
  `doc["objects"]` is a **projection**, rebuilt by `_project()` on every
  build and never written to — strip it from a document and the log
  reconstructs the same scene, ids, parents, field and all (there is a test
  for exactly that). Two stored representations of one structure would
  drift, so there is only one.
  - `_build` is a single pass: create → construct and attach, remove → detach
    the subtree, reparent → move it, transforms → replay. `_create` accepts
    either form of nesting: `parent` on the child (what the API records) or
    `children` on the Collection (what a script's `Collection(a, b)` means).
  - **What an object *is* lives on its create event**, so `set_param`,
    `apply_edit`, `reset_style` and `set_visible` edit that event in place
    rather than appending. Same reason a CAD history lets you change the box
    you made instead of recording that you changed it — and it keeps the log
    from growing without bound while a slider is dragged. This is also what
    makes "change that magnet's dimensions after the fact" work: it is an
    edit to the step that made it, reachable from the Inspector *and* from
    the ✎ on its Construction row, which offers the constructor parameters.
  - **A pose pin supersedes the pin it follows.** `set_transform` writes
    `position`/`orientation` events, and the Inspector's position field calls
    it on every change — so without coalescing, four nudges left eight
    entries, six of them dead. It now rewrites the trailing pair when they
    are the last thing in the log and belong to the same object; once
    anything else has happened, order matters and it appends.
  - **What happened *to* it is appended**: transforms, removals, reparents.
    A removal does not delete the earlier events; they ran while the object
    existed. A reparent's position in the log is what decides which group
    transforms carried it, which is why it is not a rewrite of the create.
  - **One bad event does not break the document.** The fold catches per-event
    failures into `self._broken` and carries on, so the rest of the log still
    describes a scene to look at while fixing the entry that went wrong.
    `get_events` marks those entries with `error`.
  - **Strict for ordinary edits, tolerant for edits to the log.** Any normal
    mutation that would break something is rolled back and reported, as
    before. `edit_event` / `move_event` / `remove_event` go through
    `_edit_log` instead: they apply and return what fell over under
    `"broken"` — refusing would mean history is only editable when nothing
    depends on it, which is not the interesting case. The edited event itself
    must still work; if it is the thing that cannot replay, the edit is
    undone. `load_scene` is tolerant too: a document may hold broken events,
    so it has to be able to open again.
  - Consequences worth knowing: an event cannot be reordered above its
    object's create (it lands in `broken`, naming the object), and a document
    with neither `objects` nor `events` is now rejected by `load_scene`
    rather than read as an empty scene.
- **Transforms — the doc records magpylib CALLS, not derived poses**, and
  they live in that same ordered log, not per object. Each event is
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
- **The rule is visible, and says so as you type.** `expression_help()`
  returns the operators, functions and constants **read off the allow-list
  that enforces them**, so the help cannot drift from what evaluates — there
  is a test comparing the two, and its examples are themselves checked. The
  Variables panel shows it under "what can go in a value"; every variable
  input box carries a one-line version as placeholder text and validates
  through `check_expression()`, which names what went wrong ("'sinh' is not
  one of the functions an expression may call", "Subscript is not allowed in
  an expression") while it is being typed rather than after it is rejected.
  Names are deliberately *not* checked there: one that does not exist yet is
  well formed, and gets offered for creation instead.
- **Sweeps**: `sweep(variable, values, sensor_id?/points?, field?)` re-folds
  the document once per value and reads the field; `get_sweep_figure(...)`
  plots it (one hue light→dark over observation points — same quantity in
  different places, not unrelated series). Nothing is recorded in history and
  the document ends on the value it started on. This is what variables are
  *for*, and it is only affordable because a rebuild is milliseconds.
- **Pattern events**, the CAD feature family: ONE event standing for N copies.
  `duplicate_around(id, count, axis?, anchor?, spin?)` is the **circular**
  one, each copy optionally spun by `spin`×index (a Halbach ring is
  `spin = 360/count`); `duplicate_along(id, count, step)` is the **linear**
  one. **A rectangular grid is the linear pattern applied twice** — to the
  object, then to the Collection holding it — so there is no grid op to keep
  in step with the other two; composing the log already expresses it, and
  both counts stay editable (`nx`, `ny`). `mirror(id, plane?, normal?,
  anchor?)` is the **reflection**, and is the one with real physics in it:
  - a reflection has determinant −1 and an orientation is a *proper*
    rotation, so a mirrored frame cannot be stored as one; and polarization
    is an **axial** vector, so its component along the normal survives while
    the tangential ones reverse — the opposite of what position does. "The
    polarization is in the local frame so nothing changes" gives the wrong
    magnet.
  - both are solved by borrowing the body's own z-flip symmetry `T`:
    `orientation' = S·R·T` (proper again) and `polarization' = −T·J`. The
    test checks it against physics rather than against itself: **B** is axial
    too, so the copy's field at the mirrored point must come out as
    `2(B·n)n − B`, for a magnet in a thoroughly general pose.
  - only shapes with that symmetry can be reflected (Cuboid, Cylinder,
    CylinderSegment, Sphere, Dipole, Sensor). A Tetrahedron, mesh or Polyline
    is refused by name: reflecting it means flipping its vertices, which is a
    different object rather than the same one placed differently.
  - magpylib has no mirror, so `to_script` emits a `_mirror` helper
    (`_MIRROR_HELPER`) and calls it — the script stays runnable *and*
    parametric, the copy still following whatever the source does, and
    `parse_script` reads the call back so the round trip is exact.
  Path-driven patterns are the family member still missing.
  **A removal takes the copies with it** — they are part of the object they
  came from, and left behind they were invisible (nothing lists a copy whose
  source is gone) while still standing in the scene and contributing to every
  field it computed. **`to_script` emits only what happened to objects the
  log still holds**, since a removed object leaves no definition and a step
  naming it was a `NameError` in the exported file. Both are invariants in
  the tests now, checked across every example rather than in one scene.
  **Copies are named after their source, numbered like their id** (`r1#3` →
  "Magnet 1 #3"), by `_name_copy` at the fold and by a line the loop emits
  into the script. magpylib's `copy()` increments a trailing number in the
  label, which is right for copying one object by hand and wrong for a
  pattern: every copy comes from the same source, so a ring of ten arrived as
  one "Magnet 1" and nine identical "Magnet 2"s — a name already belonging to
  a different magnet in the same scene. `parse_script` skips that assignment
  (it is regenerated from the source), so the round trip stays exact.
  In the UI all three are behind one **Pattern…** command that asks which
  kind first, because they are the same idea about a different thing. `count` and `spin` may be expressions, so the whole
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
  the map uses the last path step. **The Field panel can now choose that
  source**: its map mode has an "on a plane / off a sensor" selector built
  from `list_objects`, which reports `pixels: [rows, cols]` for any Sensor
  carrying a grid, and defaults to a sensor when the scene has one — without
  it, `get_field_map(sensor_id=…)` existed but was unreachable from the UI,
  which made the pixel-grid example pointless.
- **Field evaluation**: `get_field(sensor_id?, points?, field?)` — summed
  **B, H, J or M** of all leaf sources (`_FIELDS` maps each to its magpylib
  getter and unit; J and M are zero outside a magnet and constant inside it,
  which makes them the quick way to see what a shape covers) along a sensor
  path or explicit points
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
  - **Variables view** (`variablesView.ts`) — a **webview, not a tree**. A
    TreeItem holds a label and an icon and nothing else, and the point of
    bounding a variable is to be able to *drag* it, so the panel showing the
    variables has to be one that can hold a slider. Each row is name, slider
    (when bounds give it a span), value box, and ⋯ / ✕ buttons; per-row
    actions are buttons rather than a context menu, which is what the change
    cost. The title bar keeps `+ New Variable…` and Sweep…, which work for
    webview views. The value box takes `2.5` or `gap*2`; the `=` marker the
    document uses is added for you. A variable defined by an expression shows
    no slider — its value belongs to the expression — and one with no bounds
    says "no range" rather than silently offering nothing.
    **Set Bounds…** (the ⋯ button) asks for the allowed range and then the
    slider range.
  - **A range is offered wherever a variable is born** — after the value, in
    both `+ New Variable…` and the auto-create prompt that fires when a typed
    expression names something new. Enter skips it. Only the *allowed* range
    is asked for there (the slider falls back to it; Set Bounds… covers the
    soft range for when the two differ), and `"0,"` / `", 10"` give a
    half-open range. Asking at creation is the difference between a slider
    that exists and one nobody finds.
  - **`integer`** marks a variable that counts things (`n`, `turns`, `nx`).
    It lives beside the bounds because it is the same kind of statement — a
    constraint on the domain, not a hint for the slider — and it is enforced
    the same way, at the fold, wherever the value came from: an expression
    that lands on 10.5 is refused like a typed 7.3. The patterns now refuse a
    fractional count too (`_whole`) instead of `int()`-ing it, which was a
    silent way to end up with one magnet fewer than the scene claimed.
    **Units are the obvious next tenant of that dict** and deliberately not
    there yet: nothing consumes them, and a slot nothing reads is a promise
    rather than a feature.
  - **Bounds, hard and soft** (`doc["variable_bounds"]`, `set_variable_bounds`):
    hard `min`/`max` are enforced **in `_build`**, not where a value is typed,
    so they hold however the variable arrived — including when it is driven by
    another variable's expression, which is the case a validate-on-input check
    would miss. Soft `soft_min`/`soft_max` constrain nothing; they are the
    range worth dragging or sweeping through, must lie inside the hard ones,
    and are what a slider spans (falling back to the hard range). Bounds are
    editor metadata: a script has nowhere to put them, so `apply_script`
    carries them across for variables that survived the edit rather than
    dropping them on every save, and `_canonical` deletes any whose variable
    is gone.
  - **The history lives in the Scene tree, under the object it happened to.**
    Each object expands to its own steps ("created", "orbit 36° about z")
    before its children, so reading the tree is reading how the scene was
    built. Selecting a step shows its values in the Inspector, editable in
    place. That is AEDT's shape — a model tree plus a property grid — and it
    is why there is no separate history panel: **there was one for two
    commits and it was a worse script tab**, one line of Python per row,
    duplicating both the tree and the script and adding only reordering.
    Steps are labelled for what they did (`_event_label`); the call that did
    it is the tooltip, and the script.
  - **Editing a step**: click it, or ✎ *Edit Step…* which also focuses the
    Inspector — clicking a tree row and having values appear in another panel
    is not discoverable on its own, which is how it was found. The form takes
    expressions like every other numeric field, so **a variable can be put on
    a step after the fact**: type a name into an orbit's `angle` and the scene
    becomes parametric in something nobody planned for, prompting for the
    value if it is new (`edit_event` is in `MUTATING_WITH_VALUES` for exactly
    that). The label follows: "orbit stagger° about z".
  - **Rollback**, the other half of a CAD feature tree: right-click a step →
    *Build Up To Here* folds only the events up to it, so you can watch the
    scene assemble. Steps after the point are dimmed and marked "not
    applied"; the Scene title bar gains *Show The Whole Scene* while it is
    active (context key `magpylib-studio.rolledBack`, read back from the
    engine rather than tracked in the UI). It is a **view**: `to_dict`,
    `to_script` and Save Scene As still see the whole document — only what is
    built is partial (`_objects_view`).
  - **Editing while rolled back inserts at that step**, the other half of the
    gesture: `_reposition_for_rollback` moves whatever a mutation appended
    into the rollback position and advances it, so several edits stack up in
    the order they were made, and the result carries `inserted_at` (the UI
    says so in the status bar — the scene on screen is a preview and looks
    like the whole). This is well defined *because* a rolled-back scene holds
    only the objects that existed then: whatever you can act on is already
    there, so nothing inserted can refer to something created later. Anything
    that did not simply append — loading a document, editing the log itself —
    returns to the end instead, and `_folded_events` clamps the step when undo
    restores a shorter log.
  - The remaining panel called **Undo** is the session's snapshot stack, and
    is not history in the document sense: document copies, capped at 100,
    gone on reload.
  - Sliders commit on release, with the value box updating live during the
    drag. (They lived in the Inspector briefly, as a workaround for the tree;
    that section is gone now the Variables view can hold them itself — two
    places to edit the same number was worse than the tree limitation.)
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
    it: there is no one to ask, so Copilot gets the error and fixes it.
  - **LM tools cover the parametric surface**: `#magpyVars`, `#magpyVar`,
    `#magpyDuplicate`, `#magpySweep`, and the existing add/set/move/rotate
    descriptions now teach the `=expression` convention (a model that is not
    told will only ever write literals). `#magpyAdd` is explicitly steered
    away from adding ring magnets one at a time. `set_variable`,
    `remove_variable` and `duplicate_around` are batchable, so a whole
    parametric scene — variables, the object written in terms of them, the
    arrangement — is one undoable call. **Sweep a Variable…** ($(graph-line) in the same
    title bar) asks for from/to/steps and drives the Field panel's third
    mode, "Against a variable" (`get_sweep_figure`). **Duplicate Around…**
    is in the scene tree's Transform submenu.
  - **Generated copies are read-only, not inert**: they come back from
    `list_objects` with a `derived` key, and get `contextValue =
    'derivedCopy'` — deliberately outside the `magpy*` namespace that all 14
    scene-view menu entries are gated on, so none of them match. They are not
    draggable. They **are** selectable: they were not, and since 18 of the
    24 rows in the Halbach example are copies, clicking a magnet did nothing
    at all and the Inspector stayed blank — which read as the panel being
    broken. Selecting one now opens it read-only, headed "generated from r1 —
    change that object, its pattern step, or the variables", and every write
    path in the Inspector refuses through one `refuseIfGenerated()` rather
    than each failing its own way at the engine. Without that they rendered as
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
    Python, save, and they land in the document. Being a real file, VS Code
    restores its tab across a window reload — so the path is fixed at
    activation (not when the tab is first opened) and `adoptRestoredScriptTab`
    re-renders whatever was restored against the scene the engine has *now*;
    otherwise the tab silently shows the previous window's project until it is
    closed and reopened. That is also why the extension activates on
    `onStartupFinished`: a tab it owns can be on screen before the user asks
    for anything. Unsaved edits (hot exit) are still left alone.
  - Python resolution: `magpylib-studio.pythonPath` setting → workspace/.venv →
    repo-root/.venv → `python3`. Engine stderr → output channel.
  - Verified via `node` smoke test driving compiled `EngineClient` against the
    real engine: all methods, invalid-edit rejection, unknown-method rejection.
  - **Webview JavaScript lives in `media/*.js`**, loaded with `asWebviewUri`
    under a nonce CSP — the layout every `vscode-extension-samples` webview
    uses, and the reason is not tidiness. It used to sit inside TypeScript
    template literals, where the compiler sees only a string: a `\n` written
    singly was resolved by TypeScript into a real line break *inside a quoted
    string*, the emitted script had a syntax error, and the Inspector rendered
    as blank HTML with no error visible anywhere, because a script that cannot
    parse cannot report that it did not parse. 1,031 lines moved out;
    `src/webview.ts` holds the `nonce()` and `mediaUri()` both ends need, and
    `media/jsconfig.json` makes the editor check that code. Two guards now:
    `harness/check-webview-scripts.js` parses every `media/*.js` and refuses
    any `src/*.ts` that has grown a webview script back inside a template
    literal (wired into `npm run compile`, so a broken panel fails the build),
    and `harness/webview-harness.js` executes a panel's own
    script under a DOM shim against a real engine and prints the resulting DOM
    as text — `npm run inspect -- halbach`, or
    `node harness/webview-harness.js variables halbach`. Escapes meant for the
    webview must be doubled; if a panel is ever blank, run the checker first.
  - The other half of the same blind spot is the **message contract**: the
    host posting a type the webview does not handle is silent. That is how
    "what can go in a value" stayed empty — the Variables provider posted
    `{type:'help'}` on ready and the script had no branch for it, so
    `loadHelp()` was dead code. Both panels now end their handler with an
    `else` that puts `unhandled message: X` on screen.
  - **Tests now run inside a real Extension Development Host**: `npm test`
    (`@vscode/test-cli`, config in `.vscode-test.mjs`, suite in `src/test/`).
    Four so far, and they cover the seams nothing else could reach —
    activation, every declared command being registered, the engine
    subprocess answering through the virtual `scene.json`, a removal through
    the real command path taking a pattern's copies with it, and the script
    tab exporting the scene and applying an edited `radius` back on save.
    Webview *content* stays with the DOM harness: a test cannot read into a
    webview. Two notes for whoever runs it: the user-data dir is forced into
    the system temp dir because a unix socket path cannot exceed 103
    characters and this repo's path is deep enough to blow that; and
    `.vscode-test/` holds a 300 MB VS Code download, which is gitignored.
  - **Language model tools carry `prepareInvocation`**: an invocation line
    saying what is about to happen ("Removing r1", "Patterning r1 about an
    axis"), and for the three that destroy something — `clear_scene`,
    `remove_object`, `remove_event` — a confirmation that names it. The
    guide's point is that a dialog naming nothing in particular is one people
    click through, and `remove_object` in particular now says out loud that
    a pattern's copies go with the object. `check-contributions.js` also
    checks the tool contract (declared vs registered, and that each carries
    displayName / modelDescription / inputSchema), and an integration test
    asserts all 24 are live in a real host.
  - **Checked against the API guides**, not only the samples: the two sidebar
    webviews dropped `retainContextWhenHidden` (the guide calls it a last
    resort for its memory cost, and both rebuild from the engine on ready);
    the two plotly panels keep it with the reason written down, since what
    would be lost is a camera the user positioned. Commands whose handler
    needs a tree object are hidden from the Command Palette with
    `when: false`, because the palette invokes them with nothing —
    `editOperation` and `toggleVisibility` were still leaking.
    `harness/check-contributions.js` now enforces all of that at build time:
    declared/registered/referenced commands agree, palette rules cover every
    argument-taking command, and every menu `when` clause matches a
    contextValue the tree can actually produce.
  - `npm run compile` is tsc + **eslint** (`eslint.config.mjs`, flat config,
    type-aware rules for `src/` and browser globals for `media/`) + the
    webview script check + the contribution check, so all of it runs before
    F5 and before packaging.

## Developing the extension

Open the **repo root** and press F5. There is no second window to open first:
the launch config lives at `.vscode/launch.json` and points
`--extensionDevelopmentPath` at `vscode-extension/`, so the engine stays in
the workspace where you can edit it. Two windows total — yours and the
Extension Development Host — which is how extension debugging works and as
few as it goes.

The host opens `sandbox/`. That is deliberate: extension storage is *per
workspace*, and the script tab's `scene.py` lives there, so a host with no
folder open falls back to global storage shared with every other folder-less
window — which is how the script tab ended up showing a previous project's
scene. A fixed folder makes it deterministic.

Three configs: **Run Extension** (compiles first), **Run Extension (no
build)** for when `npm run watch` is already going, and **Extension Tests**.

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
- **Units are still absent, deliberately** (see below) — the one open
  question on the parametric side.
- **Units are still absent, deliberately** — everything is bare SI, as
  magpylib wants. If ANSYS-style `5mm` values are ever wanted, that is a
  layer over `expressions.py`, and it needs deciding before variables get
  used widely enough that migrating them hurts.
- **Undo is still snapshots.** `_undo` holds whole document copies, so the
  History view and the event log remain two mechanisms that look alike. Now
  that structure is event-sourced, undo could become a pointer into the log —
  except variables and bounds are not events, so it would only be honest once
  they are too. That is the remaining half of the unification.
- **Undo stays snapshots, on purpose.** It cannot become a pointer into the
  log, because the log is deliberately *not* append-only: what an object is
  lives on its create event and is edited in place, so the log does not hold
  the previous value to step back to. Making it hold one means appending
  every slider drag. AEDT is the same shape — a history tree plus a separate
  undo — and for the same reason.
- The events panel shows the log but cannot yet **drag** to reorder (↑/↓
  buttons only), and editing a create event's params redirects you to the
  Inspector rather than doing it in place.
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

- `to_script` writes **every definition before every step**, so a scene that
  creates an object *after* transforming another comes back from the script
  with the creates hoisted above the transforms. The document is unaffected;
  it is the script round trip that reorders. Emitting in log order would mean
  children joining their Collection with `.add()` instead of constructor
  args, which `parse_script` would then have to tell apart from the `.add()`
  a mirror uses.

- The `get_figure` result is `json.loads(fig.to_json())` — plotly's encoder
  handles numpy/bdata; don't use `to_plotly_json()` (leaves numpy in, not
  JSON-safe).
- Style paths are **dotted** (`magnetization.arrow.width`); `to_script` nests
  them for the `style=` kwarg. Constructor `style=` needs nested, not dotted.
- LLM: for a VS Code extension prefer `vscode.lm` (Copilot, zero key). Use the
  Anthropic SDK path from the Solara POC only if you specifically want Claude
  and are OK managing keys/chat UI.
