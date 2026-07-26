# magpylib-studio

Headless **magpylib editing engine** — the framework-agnostic core for a GUI +
LLM studio (initially targeting a VS Code extension). It owns a magpylib scene
and exposes everything a frontend needs over a tiny JSON-RPC protocol. The same
engine backs any frontend; the presentation layer (webview, Solara, CLI) is a
thin client.

## Design

- **The scene document is the source of truth.** Every edit updates both the
  live magpylib object *and* the document, so `to_dict()` / `to_script()` always
  reflect the current state and can be versioned in git — that's the intended
  "history" mechanism, not a bespoke undo stack.
- **One contract, driven by `magpylib`'s `schema()`.** The same JSON Schema
  drives the frontend's inspector widgets *and* the LLM tool's `input_schema`.
- **Validation is shared.** Every edit goes through `style.set(path, value)`,
  which the property tree validates; a bad edit is *reported*, not raised, so a
  GUI shows an error and an LLM self-corrects — no second validation layer.

## Setup

```sh
uv venv --python 3.13 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -e ../magpylib   # property-tree branch
VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## JSON-RPC protocol (stdio)

The host spawns `python -m magpylib_studio` and exchanges one JSON object per line.

```
-> {"id": 1, "method": "get_schema", "params": {"object_id": "cube"}}
<- {"id": 1, "result": { ...JSON Schema... }}
```

| method | params | result |
|---|---|---|
| `list_objects` | — | `[{id, type, label}]` |
| `get_schema` | `object_id` | JSON Schema of the object's style |
| `get_values` | `object_id` | `{"set": {...}, "resolved": {...}}` |
| `get_figure` | — | plotly figure JSON of the whole scene |
| `apply_edit` | `object_id, path, value` | `{"ok": bool, "error"?: str}` |
| `to_dict` | — | the scene document |
| `to_script` | — | equivalent magpylib Python code |

Try it:

```sh
printf '%s\n' \
  '{"id":1,"method":"list_objects"}' \
  '{"id":2,"method":"apply_edit","params":{"object_id":"cube","path":"color","value":"red"}}' \
  '{"id":3,"method":"to_script"}' \
| .venv/bin/python -m magpylib_studio
```

## Where this is going

The VS Code extension (TypeScript) is a thin shell around this engine:

1. spawns `python -m magpylib_studio` and supervises it;
2. a **webview** renders `get_figure()` with plotly.js (`uirevision` for the
   camera) and builds a schema-driven inspector from `get_schema()`;
3. edits (from controls or the model) call `apply_edit`, then re-render;
4. the LLM is a **Language Model Tool** (`vscode.lm`) whose `inputSchema` is the
   style schema — the native Copilot chat drives the scene, no API key;
5. the scene is persisted as a magpylib `.py` (`to_script`) → git is the history.

Not yet implemented here: `add_object` / `remove_object` / `move`, loading a doc
from a file, and the AST-free script→document parse (currently the document is
canonical and the script is generated from it).
