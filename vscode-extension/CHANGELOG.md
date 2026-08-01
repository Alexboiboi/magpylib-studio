# Changelog

All notable changes to the Magpylib Studio extension.

## [Unreleased]

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
