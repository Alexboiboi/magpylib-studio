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
  `load_scene` (dict or JSON file path), `to_dict`, `to_script`. Structural
  edits go through `_mutate_doc`: mutate doc → rebuild scene; on failure the
  old doc is restored and the error reported (`{"ok": false, ...}`).
- `magpylib_studio/rpc.py` — JSON-RPC stdio loop (`serve`), method allow-list.
- `magpylib_studio/__main__.py` — `python -m magpylib_studio`.
- `tests/test_session.py` — 16 tests, **all green**, ruff clean (`uvx ruff check`).
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
    Studio panel is **plot-only** now. `broadcastMutation()` refreshes plot +
    tree + inspector after every edit from any surface (inspector widgets,
    LM tools, tree commands).
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
   **git is the history**, no bespoke undo stack.
2. **`schema()` is the one contract** — the same JSON Schema drives the
   frontend inspector widgets AND the LLM tool's `input_schema`.
3. **Shared validation** — every edit goes through `style.set`, validated by the
   property tree; bad edits are reported so a GUI shows an error and an LLM
   self-corrects. No second validation layer.
4. **Document canonical, script generated** (not AST-parsed). The hard future
   piece is the reverse (script → document); deferred on purpose.

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
  "magpylib Studio: Open Studio"; in Copilot chat try `make the cube green
  #magpyEdit` or `add a green sphere at [0,2,0] #magpyAdd`.
- **Scene load/save UI** on top of `load_scene`/`to_dict`/`to_script`
  (commands + file pickers; "export as Python script" is a one-liner).
- **Nested collections**: `children` in the doc format, recursive
  `_build`/`to_script`, parent-aware add/remove/move; then the scene tree
  (already a TreeDataProvider) shows real hierarchy.
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
