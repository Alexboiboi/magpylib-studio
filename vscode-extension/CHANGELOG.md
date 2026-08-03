# Changelog

All notable changes to the Magpylib Studio extension.

## [Unreleased]

### Added

- **A variable can be renamed** — under `⋯` in the Variables panel,
  `rename_variable` on the engine. Everything written in terms of it is
  rewritten through the syntax tree, so the scene is unchanged and the bounds
  follow the name.

- **A script says what its variables are allowed to be**, in the comment on
  each one's line (`n = 10  # 2 to 60, slider 4 to 20, whole`), and reads them
  back. Limits used to be editor-only, so a scene that travelled as a script
  arrived with its sliders gone.

### Changed

- **`⋯` on a variable opens a menu instead of a three-step wizard.** Pick the
  name, a range, the kind, or clear the limits, and answer one box. Each entry
  shows what it currently holds.

### Fixed

- **An object shows the steps that built it, not just the ones that contain
  other objects.** The scene view gave a row its chevron by counting child
  objects alone, so a magnet's own history was there but unreachable — only
  collections, which happen to contain something, ever showed their steps.

- **An object the scene no longer has still shows its history**, under a dimmed
  row saying why it is missing: deleted, rolled back past, or no longer built.
  Deleting is recorded rather than erased, but the tree had nowhere to hang
  those steps, which made deleting the one step it could not undo.

- **Move Step Later on a "created" step is refused** instead of leaving the
  object gone and its whole story broken. Nothing can happen to an object
  before it exists.

- **Pattern-along and mirror steps have their own glyph** instead of the
  anonymous dot that means "no icon for this".

## [0.2.0]

### Added

- **Move By… and Rotate… ask how the path is described, not just how long it
  is.** There was one shape on offer — a total divided into equal steps — and no
  way to say the other things people were holding. Four kinds now: **even
  spread** as before (`np.linspace`); **by increment**, one step repeated
  (`np.arange`), because "1 mm per step" is often the physical quantity and the
  span the derived one; **custom points**, the path as a document, a step a
  line, and the only kind that keeps expressions, since nothing in it is
  divided; and **formula**, below. Which call built a path is recorded rather
  than guessed — a quarter of increment-built paths are also exactly a linspace,
  so the same input would otherwise export two ways depending on whether the
  arithmetic coincided. The `move` and `rotate` tools take the same `spacing`
  argument.

- **A run of points can be stated as the curve that draws it**, in a parameter
  or a path:

  ```
  count: =round(per_turn * turns) + 1
  of:    radius * cos(tau * turns * t)
         radius * sin(tau * turns * t)
         height * t - height / 2
  ```

  Held as points, a helix is sixty rows of that expression with a different
  number in each. A list of rows can say how many points it _has_, never how
  many it _wants_ — so how finely a curve is drawn was the one quantity in a
  parametric scene no variable could reach. It takes a slider now. The script
  says what a person would have written, one `np.linspace` and one vectorised
  expression a column, because the document says the same thing. `min` and `max`
  are refused in a template: neither is elementwise over a sample, so there is
  no vectorised spelling that means the same thing.

- **…and both places you would build one ask for it.** Move By… and Rotate…
  offer **Path — formula**, and Add Object… asks whether a polyline's vertices
  are typed or sampled. Either way: how many points — a number or an expression,
  which is the whole reason to reach for this — then one line per axis, as
  formulas in `t` running 0 to 1 along the curve.

- **A "Helical winding" example**, which is the case that needed all of it:
  every other built-in scene is made of patterns, and a continuous winding
  cannot be. The solenoid stacks separate loops; this is one wire, and what it
  is is a formula.

- **A polyline's vertices are typed a point to a line, in an editor.** Add
  Object… asked for a flat run of numbers in one box — nine for the polyline's
  default, forty-five for a real PCB trace — and reshaped them by counting in
  threes, so a miscount by one silently shifted every vertex after it. The
  Inspector had already concluded that a table of numbers on one line "is not an
  editor, it is a wall"; creation now agrees. A wrong count is caught while the
  editor is still open.

### Changed

- **The document format is version 2.** A scene saved by this release is refused
  by an older one, with a message saying so, rather than opened and quietly
  emptied: a run of points stated as a formula means nothing to version 1, which
  read the template as expressions over an undefined `t`, reported the load as
  fine and dropped the object. Scenes saved by older releases open here as
  before and are stamped 2 when saved.
- **Add Object… shows the shapes it is offering.** The menu named ten classes in
  words while the Scene tree drew each of them as a wireframe; picking a
  cylinder segment out of a list of nouns and then seeing what you got is a
  round trip nobody asked for. The menu now carries the same glyph the tree will
  show the object as, from the same source, so the two cannot drift apart.
- **…and says what each one is, instead of reciting its defaults.** Every entry
  spent its one line of prose on the numbers it was about to prefill —
  "polarization (0,0,1) T, diameter 1 m" — which the next screen says again, in
  the box that asks for it. With the shape now drawn, that line is free to
  answer the question the menu is actually for: a cylinder segment is "a wedge
  of a ring — arc magnets, rotor and stator poles", a dipole is "a point source
  — for a magnet too small or too far to model as a shape". The defaults are
  unchanged and still prefilled.
- **Every entry names its magpylib class, and can be found by it.** The old
  details gave the class away by accident — "moment (0,0,100) A·m²" could only
  be a Dipole — and dropping them would have left the menu with no machine name
  at all. Each row now carries its class beside the label, and the filter
  matches on it: "Current loop" is the friendlier name for `current.Circle`, but
  typing `Circle` used to match nothing in a menu that offers it.
- **Reading the scene from chat costs a tenth of what it did.** `#magpyObjects`
  listed every copy a pattern had made — at n=60 the Halbach example was 124
  entries, 118 of them generated copies that say, one by one, that they cannot
  be edited. They are now counted on the object that made them (`"copies": 59`),
  which is the same fact in one field: 3,880 tokens down to 356, and an extra
  copy now costs nothing to read instead of a row. The Scene tree still lists
  them one by one, because a ring of twelve should look like twelve.
- **Field results are six significant figures, and do not repeat the question.**
  A reading carried all 17 digits of the float holding it, and every response
  handed back the points the caller had just sent. A 400-point map goes from
  10,734 tokens to 4,413. Values that go back into the document — positions,
  dimensions — keep every digit, because those are not readings.

### Fixed

- **A moved path stays a move.** A four-line script — a cuboid and
  `move(np.linspace(...), start=0)` — came back as a single line of three
  hundred numbers, every pose of the animation baked into `position=` and the
  move that made it gone. The document records transforms as the calls that were
  made, which is the first of its own design rules and which orientation paths
  already followed; position paths now do too. The script says
  `cuboid1.move(np.linspace((0.0, 0.0, 0.0), (0.1, 0.1, 0.1), 100), start=0)`
  again — written as the call that makes it wherever that reproduces the path
  _exactly_, and read back the same way, so the round trip stays byte-identical
  and a path that only looks evenly spaced is still written out in full.
- **A path from Move By… or Rotate… starts where the object is.** A path of n
  movements is n+1 poses and the first of them is the pose you began at; it was
  being left out, so the animation never showed the starting position and the
  path was one that no single call could describe. Including it costs one pose
  and makes the export a plain
  `np.linspace((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 21)`. Paths already saved
  without their origin are still written compactly, as the same call without its
  first point — they are exactly that, and re-deriving them instead would turn a
  stored `0.55` into `0.5499999999999999`.
- **A constructor parameter that is a run of points is written as the call that
  makes it.** A sensor walking twenty-five positions exported as twenty-five
  triples; the compact spelling had only ever been offered to transform paths,
  though a parameter can be just as long. Only tables of points:
  `dimension=(1, 1, 1)` is three numbers describing one box, and
  `np.linspace(1.0, 1.0, 3)` reproduces it exactly while saying something absurd
  about it.
- **An exported script imports the maths its expressions use.** An expression
  goes into the script verbatim, but nothing imported what it called — so a
  scene using `sqrt`, `cos`, `pi` or `tau` anywhere exported a script that
  raised `NameError` on its first line of geometry, including
  `sqrt(2) * radius`, which is the expression help's own worked example.
- **A path no longer begins on a repeated frame.** Once paths carried their own
  first pose, magpylib's `start="auto"` appended it after the pose the object
  was already at, so the animation held still for a frame at every join — 7
  poses where 6 were meant. `auto` was the default and one keystroke away, and
  escaping it meant picking "index…" and typing the `0` that should have been on
  offer. The prompt now asks **Start over** or **Continue**, and is skipped
  entirely for an object with no path, where they mean the same thing. `auto`
  stays the engine's default: a path from a script has no leading pose to
  collide with.
- **A path from Move By… or Rotate… is spaced the way the call that writes it
  spaces it.** Both wrote `(c * i) / steps`, where `np.linspace` divides first
  and scales by the index. The two agree in the last bit only when the
  displacement is a clean 1 — which the prompt's own default is, so every hand
  test passed while 93% of real displacements silently lost their compact form
  and exported as literal triples. Both of numpy's branches are mirrored now,
  and the values it prints are pinned in a test: this is one language
  implementing another's arithmetic, and nothing else would notice it drifting.
- **Importing a script now says what running it flattened.** A loop of eight
  current loops became eight separate objects with no mention that anything had
  been lost, so the next edit changed one of eight where the script had one
  thing to change. The importer collected these warnings from the first release
  and never filled any in — the promise was in the README the whole time.

## [0.1.3]

### Fixed

- **The Inspector showed numbers that were not the numbers.** Its rounding
  helper matched an unescaped `.`, so it ate the last significant digit along
  with the trailing zeros: 2.5 read as "2.", 3.25 as "3.2", a 5 mm dimension as
  "0.00". Editing one component of a position committed the other two as
  displayed, which turned that into real geometry loss — a vector now sends each
  component's document value unless you typed in that box, so untouched ones
  round-trip exactly, full precision included.
- **A pattern step's expressions no longer claim to be "currently NaN".** The
  step form has no resolved value for `=360 / n` and was reading the expression
  itself as a number; it now says so, and shows the real value where the engine
  reports one.
- **An engine that dies takes the scene with it, and now brings it back.** The
  replacement process used to start empty — and the first edit after that wrote
  the empty scene over the crash backup, which was the only copy of anything
  unsaved. A restarted engine is handed the backup before anything else can
  speak to it, and the backup is frozen in the meantime.
- **Installing the engine no longer freezes VS Code.** `uv venv`,
  `python -m venv` and `pip install` ran synchronously on the extension host,
  which is one thread shared by every extension in the window, so the whole
  editor stopped for the length of the install — the first minute a new user
  spends here. Finding an interpreter was synchronous for the same reason and is
  not any more.
- **The 3D and Field panels come back after a window reload**, like the scene
  and the script tab already did.
- **The Field view lists a sensor's measuring grid as soon as it opens**, rather
  than after the next unrelated edit, and **Sweep a Variable…** no longer races
  the panel it just opened.
- Changing `magpylib-studio.pythonPath` restarts the engine on the new
  interpreter — carrying the scene across — instead of doing nothing until the
  window is reloaded.

### Changed

- **Install the Engine names the interpreter it is about to change.** When the
  Python extension has one selected it may be a system Python, and installing
  into it is a different act from making a `.venv`; the prompt says which one it
  found and offers a `.venv` instead.

## [0.1.2]

### Added

- **A variable can be a choice, not only a quantity.** Bounds gain `options`, so
  a variable whose value is a name — a rotation axis, say — offers a dropdown
  the way a bounded number offers a slider, enforced wherever the value came
  from. Creating a variable asks what kind it is up front, since that decides
  the remaining questions, and a whole-number variable stays one even when the
  range is skipped. The Halbach example carries `tilt` and `tilt_axis` to show
  it, defaulting to zero so the scene looks the same until you drag it.

### Fixed

- **A pattern's copies join their group in one call.** `Collection.add` rebuilds
  its source and sensor lists on every call, so adding copies one at a time was
  quadratic — and a pattern's count is a slider, so that ran on every drag. A
  Halbach rebuild at n=500 goes from 147 ms to 95 ms, and the quadratic term is
  gone; a 6000-magnet exported script runs in 0.6 s instead of 2.5 s. Scripts
  written the old way still parse.
- **Reopening a scene no longer asks about unsaved changes.** It restores them
  the way VS Code's own hot exit restores unsaved editors — in the tree, marked
  unsaved, named in the view title — instead of asking a question that
  dismissing never answered, so it asked again on every window start. Reopening
  also no longer forces the 3D panel open.
- A name-valued variable rendered as an empty box: the variables panel called
  anything string-typed an expression and sliced off its first character. Only a
  leading `=` means an expression now. The same variable could take the whole
  panel down through `short()`, and numeric bounds meeting a name surfaced a raw
  `TypeError` rather than saying what was wrong.

## [0.1.1]

### Added

- **A getting-started walkthrough** opens once on first activation ever (per
  install, not per workspace): Open Studio, Load an Example, Add an Object, edit
  from Copilot Chat, see the Field view, save — each step linking the real
  command.
- **"No Python interpreter found" is now a one-click fix.** The error offers
  **Install the Engine**, which tries, in order: the interpreter already
  selected via the Python extension (if it meets the `>=3.11` floor), `uv` if
  installed (fetches a matching Python on demand, regardless of what's already
  on PATH), or a login-shell-resolved `python3`/`python`/`py` as a last resort —
  with a clear, OS-aware message when nothing suitable is found, instead of
  forwarding pip's cryptic "no matching distribution" text.

### Engine / publishing

- The engine (`magpylib-studio`) is on PyPI: `pip install magpylib-studio`.
- Tag-driven CI now actually publishes both artifacts — a `.vsix` GitHub Release
  and a PyPI release — from the same `v*` tag.

## [0.1.0]

### Added

- **Scenes are files.** Save and open `.magpy.json` scenes: `Cmd/Ctrl+S` with
  the Scene view focused, the file name and a `•` for unsaved changes in the
  view title, _Open Scene_ on a `.magpy.json` in the explorer, and a prompt
  before anything that would discard unsaved work. **Export as Python Script…**
  is separate, because a script carries no slider bounds and no hidden flags.
- **The format has a version and a schema.** A saved scene says which format it
  is and what wrote it; one from a newer studio is refused rather than read
  half-way, one from an older studio is migrated, and fields this version does
  not recognise are kept rather than dropped. Editing a `.magpy.json` by hand
  gets completion and validation from a published JSON Schema.
- **A reload no longer loses the scene.** The scene lives in a subprocess that
  dies with the window; it is now backed up after every edit, and the workspace
  reopens the file it was editing — offering the unsaved changes if there were
  any.
- **Event-based document.** The scene is an ordered log of events — creates,
  removals, reparents, transforms and patterns — and the object tree is a
  projection of it. Past steps can be edited and everything after them
  re-applies; a scene can be built up to any step (_Build Up To Here_) and edits
  made there are inserted at that point.
- **Variables and expressions.** Any number in the document can be written as
  `=gap * 2` over the scene's variables, with bounds, sliders, whole-number
  variables, and a sweep that re-folds the scene per value.
- **Patterns**: circular, linear (twice = a grid) and mirror, each one step
  standing for N copies.
- **Two-way script tab.** The scene exports as runnable magpylib and saving the
  tab rebuilds the scene from what you wrote — parsed when the shape matches (a
  byte-identical round trip), executed otherwise, with what it flattened
  reported.
- Field view over B, H, J or M, reading a sensor's own pixel grid.
- Six example scenes, each leaning on a different feature.

### Fixed

- The Inspector rendered blank: a `\n` inside a TypeScript template literal
  became a real line break in the emitted webview script, so the script never
  parsed. The webview code lives in `media/*.js` now, where the compiler and the
  linter can see it.
- "What can go in a value" opened onto an empty box — the host asked for the
  expression help and the webview had no branch to answer.
- A rotate step's axis showed `NaN`, because `"z"` was being read as a number.
- Deleting a patterned magnet left its copies in the scene, invisible but still
  contributing to every field.
- The exported script did not run after a removal, and copying a patterned
  object failed outright.
- The script tab kept showing the previous window's scene after a reload.
