# magpylib-studio

Headless **magpylib editing engine** plus a **VS Code extension** built on it —
a GUI *and* LLM studio for magnetic scenes. The engine owns a magpylib scene and
exposes everything a frontend needs over a tiny JSON-RPC protocol on stdio; the
presentation layer (VS Code webview, Solara, a CLI…) is a thin client.

The VS Code side is in [`vscode-extension/`](vscode-extension/) — scene tree,
schema-driven inspector, 3D view, field maps, history, and Copilot chat tools.

## Try it out

```sh
# 1. the engine (Python >= 3.11)
python3 -m venv ~/magpylib-studio-venv
~/magpylib-studio-venv/bin/pip install \
  "magpylib-studio @ git+https://github.com/Alexboiboi/magpylib-studio.git"

# 2. the extension
git clone https://github.com/Alexboiboi/magpylib-studio.git
cd magpylib-studio/vscode-extension && npm install && npm run compile
```

Open `vscode-extension/` in VS Code and press `F5`. In the window that opens,
set `magpylib-studio.pythonPath` to `~/magpylib-studio-venv/bin/python`, click
the magpylib icon in the activity bar and press **Load Example Scene**.

Full walkthrough, including building an installable `.vsix`, in the
[extension README](vscode-extension/README.md#try-it-out).

## Install the engine on its own

```sh
pip install "magpylib-studio @ git+https://github.com/Alexboiboi/magpylib-studio.git"
```

That is enough: the engine works with **released magpylib** (≥ 5.2). The
optional [property-tree branch][branch] adds a first-class style API and
path-valued physics properties (`current=[100, 200, 300]`):

```sh
pip install "magpylib @ git+https://github.com/magpylib/magpylib@feat/improve-style"
```

`magpylib_studio/style_compat.py` detects which one you have. On released
magpylib it reproduces the four style operations the engine needs from
`style.update()` / `style.as_dict()`, and falls back to a generated copy of the
branch's JSON Schema (`style_schemas.json`) — the two style trees are the same
shape, so the inspector keeps real widgets (enum dropdowns, ranges, colour
pickers) either way. The test suite runs against both.

[branch]: https://github.com/magpylib/magpylib/tree/feat/improve-style

### Development

```sh
uv venv --python 3.13 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## Design decisions

- **The scene document is the source of truth.** Every edit updates the live
  magpylib object *and* the document, so `to_dict()` / `to_script()` always
  reflect the current state and can be versioned in git — that is the durable
  history. An in-session undo/redo stack (`undo`, `redo`, `goto_history`)
  complements it for quick reverts; it does not replace it.
- **Transforms are recorded magpylib calls, not derived poses.** Each object
  spec carries an ordered log (`move`, `rotate_from_angax`, …) replayed after
  the object — and, for a Collection, its children — is built. magpylib owns
  every semantic: paths, anchors, `start`, and group transforms carrying a
  subtree.
- **One schema contract.** The same JSON Schema drives the inspector widgets
  *and* the LLM tool inputs.
- **Validation is shared.** Every edit goes through magpylib, and a bad edit is
  *reported* (`{"ok": false, "error": …}`), not raised — so a GUI shows an error
  and an LLM self-corrects. There is no second validation layer.
- **Document canonical, script generated.** `to_script()` emits runnable
  magpylib code. The reverse exists as a pragmatic bridge: `load_script()`
  *executes* a script with `show()` intercepted and introspects the live
  objects, so each `show()` call becomes an importable scene. Parametric
  structure flattens; true AST parsing is deliberately out of scope.

## JSON-RPC protocol (stdio)

The host spawns `python -m magpylib_studio` and exchanges one JSON object per
line — no ports, no framework.

```
-> {"id": 1, "method": "get_schema", "params": {"object_id": "cube"}}
<- {"id": 1, "result": { ...JSON Schema... }}
<- {"id": 2, "error": {"type": "KeyError", "message": "..."}}
```

| group | methods |
|---|---|
| inspect | `list_objects` · `get_schema` · `get_values` (style) · `get_params` (physics) · `get_transform` · `get_history` |
| structure | `add_object` · `remove_object` · `copy_object` · `move_object` (reparent) · `set_visible` |
| edit | `apply_edit` (style) · `set_param` · `reset_style` |
| transform | `move` · `rotate` · `set_transform` · `clear_path` · `set_pixel_grid` |
| view | `get_figure` (3D) · `get_field_figure` (along a sensor path) · `get_field_map` (plane heatmap) |
| field | `get_field` — summed B/H at points or along a sensor |
| history | `undo` · `redo` · `goto_history` |
| I/O | `load_scene` · `load_script` · `load_captured` · `load_example` · `clear_scene` · `to_dict` · `to_script` |
| bulk | `batch` — many mutating ops in one call, one undo step |

Mutating methods return `{"ok": bool, "error"?: str}`. Everything is
JSON-serializable in both directions.

Try it:

```sh
printf '%s\n' \
  '{"id":1,"method":"load_example"}' \
  '{"id":2,"method":"list_objects"}' \
  '{"id":3,"method":"get_field","params":{"points":[[0,0,0]]}}' \
  '{"id":4,"method":"to_script"}' \
| python -m magpylib_studio
```

## Status

The engine is covered by ~50 tests against both magpylib versions. The VS Code
extension compiles clean and is exercised by a stdio smoke test, but has no
automated UI tests yet and is not packaged as a `.vsix` — see
[CONTINUE.md](CONTINUE.md) for the current state and what is next.
