# Changelog

All notable changes to the Magpylib Studio extension.

## [Unreleased]

### Added

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
