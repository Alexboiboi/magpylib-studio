# Changelog

All notable changes to the Magpylib Studio extension.

## [Unreleased]

### Added

- **A getting-started walkthrough** opens once on first activation ever
  (per install, not per workspace): Open Studio, Load an Example, Add an
  Object, edit from Copilot Chat, see the Field view, save — each step
  linking the real command.
- **"No Python interpreter found" is now a one-click fix.** The error
  offers **Install the Engine**, which tries, in order: the interpreter
  already selected via the Python extension (if it meets the `>=3.11`
  floor), `uv` if installed (fetches a matching Python on demand,
  regardless of what's already on PATH), or a login-shell-resolved
  `python3`/`python`/`py` as a last resort — with a clear, OS-aware message
  when nothing suitable is found, instead of forwarding pip's cryptic
  "no matching distribution" text.

### Engine / publishing

- The engine (`magpylib-studio`) is on PyPI: `pip install magpylib-studio`.
- Tag-driven CI now actually publishes both artifacts — a `.vsix` GitHub
  Release and a PyPI release — from the same `v*` tag.

## [0.1.0]

### Added

- **Scenes are files.** Save and open `.magpy.json` scenes: `Cmd/Ctrl+S` with
  the Scene view focused, the file name and a `•` for unsaved changes in the
  view title, *Open Scene* on a `.magpy.json` in the explorer, and a prompt
  before anything that would discard unsaved work. **Export as Python
  Script…** is separate, because a script carries no slider bounds and no
  hidden flags.
- **The format has a version and a schema.** A saved scene says which format
  it is and what wrote it; one from a newer studio is refused rather than
  read half-way, one from an older studio is migrated, and fields this
  version does not recognise are kept rather than dropped. Editing a
  `.magpy.json` by hand gets completion and validation from a published JSON
  Schema.
- **A reload no longer loses the scene.** The scene lives in a subprocess that
  dies with the window; it is now backed up after every edit, and the
  workspace reopens the file it was editing — offering the unsaved changes if
  there were any.
- **Event-based document.** The scene is an ordered log of events — creates,
  removals, reparents, transforms and patterns — and the object tree is a
  projection of it. Past steps can be edited and everything after them
  re-applies; a scene can be built up to any step (*Build Up To Here*) and
  edits made there are inserted at that point.
- **Variables and expressions.** Any number in the document can be written as
  `=gap * 2` over the scene's variables, with bounds, sliders, whole-number
  variables, and a sweep that re-folds the scene per value.
- **Patterns**: circular, linear (twice = a grid) and mirror, each one step
  standing for N copies.
- **Two-way script tab.** The scene exports as runnable magpylib and saving the
  tab rebuilds the scene from what you wrote — parsed when the shape matches
  (a byte-identical round trip), executed otherwise, with what it flattened
  reported.
- Field view over B, H, J or M, reading a sensor's own pixel grid.
- Six example scenes, each leaning on a different feature.

### Fixed

- The Inspector rendered blank: a `\n` inside a TypeScript template literal
  became a real line break in the emitted webview script, so the script never
  parsed. The webview code lives in `media/*.js` now, where the compiler and
  the linter can see it.
- "What can go in a value" opened onto an empty box — the host asked for the
  expression help and the webview had no branch to answer.
- A rotate step's axis showed `NaN`, because `"z"` was being read as a number.
- Deleting a patterned magnet left its copies in the scene, invisible but
  still contributing to every field.
- The exported script did not run after a removal, and copying a patterned
  object failed outright.
- The script tab kept showing the previous window's scene after a reload.
