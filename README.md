# magpylib-studio

Headless **magpylib editing engine** plus a **VS Code extension** built on it —
a GUI *and* LLM studio for magnetic scenes. The engine owns a magpylib scene and
exposes everything a frontend needs over a tiny JSON-RPC protocol on stdio; the
presentation layer (VS Code webview, Solara, a CLI…) is a thin client.

The VS Code side is in [`vscode-extension/`](vscode-extension/) — scene tree
with the construction history in it, schema-driven inspector, variables with
sliders, 3D view, field maps and sweeps, an editable script tab, and Copilot
chat tools.

## Try it out

```sh
# 1. the engine (Python >= 3.11)
python3 -m venv ~/magpylib-studio-venv
~/magpylib-studio-venv/bin/pip install \
  "magpylib-studio @ git+https://github.com/magpylib/magpylib-studio.git"

# 2. the extension
git clone https://github.com/magpylib/magpylib-studio.git
cd magpylib-studio/vscode-extension && npm install && cd ..
```

Open **the repo root** in VS Code and press `F5` — not the `vscode-extension/`
folder: the launch config is at the root so the engine stays in the workspace
you can edit, and F5 compiles before it launches. A second window opens (the
Extension Development Host, on `sandbox/`). In it, set
`magpylib-studio.pythonPath` to `~/magpylib-studio-venv/bin/python`, click the
magpylib icon in the activity bar and press **Load Example Scene**.

Full walkthrough, including building an installable `.vsix`, in the
[extension README](vscode-extension/README.md#try-it-out).

## Install the engine on its own

```sh
pip install "magpylib-studio @ git+https://github.com/magpylib/magpylib-studio.git"
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
# the engine
uv venv --python 3.13 .venv
VIRTUAL_ENV=$PWD/.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q

# the extension (from vscode-extension/)
npm install
npm run compile     # tsc + eslint + webview and contribution checks
npm test            # four tests in a real Extension Development Host
```

Then open the repo root in VS Code and press `F5`.

## Design decisions

- **The document is a log, and the object tree is a projection of it.**
  `doc["events"]` holds everything that built the scene — `create`, `remove`,
  `reparent`, the transforms and the patterns — and every build folds it from
  the start, so `doc["objects"]` is regenerated rather than stored. Strip it
  from a document and the log reconstructs the same scene, ids and field
  included. Editing an early event therefore re-applies everything after it
  for free; what it breaks is reported rather than blocking the edit.
- **What a thing *is* is edited; what happened *to* it is appended.** An
  object's type, parameters and style live on its `create` event and are
  changed in place — dragging a slider must not write history — while moves,
  rotations, removals and reparents go on the end. That one distinction is
  what keeps the log finite *and* meaningful.
- **Transforms are recorded magpylib calls, not derived poses.** The log holds
  `move`, `rotate_from_angax`, … as they were made, so magpylib owns every
  semantic: paths, anchors, `start`, and group transforms carrying a subtree.
- **Scenes are parametric.** Any numeric value may be an expression over the
  document's variables (`"=360/n"`), evaluated from its AST against an
  allow-list — never `eval`, because a document is something you open from
  someone else. A variable is not always a quantity: one bounded by `options`
  holds a name (`"z"` for an axis), which gets a dropdown for the same reason
  min/max gets a slider, and is enforced the same way. `sweep()` re-folds the
  scene once per value of a variable, which is affordable because a rebuild is
  milliseconds.
- **One schema contract.** The same JSON Schema drives the inspector widgets
  *and* the LLM tool inputs.
- **Validation is shared.** Every edit goes through magpylib, and a bad edit is
  *reported* (`{"ok": false, "error": …}`), not raised — so a GUI shows an error
  and an LLM self-corrects. There is no second validation layer.
- **The saved file is the document, and it is versioned.** A scene saves as
  `.magpy.json` — exactly what `to_dict()` returns, so the format the engine
  works in is the format on disk, with no serializer in between to disagree
  with it. It carries a `version`, because a file outlives the program that
  wrote it: an older one is migrated, and a *newer* one is refused rather than
  read half-way and saved back with the parts we did not understand missing.
  Fields the engine does not recognise are carried through, which is the only
  form of forward compatibility a document can actually have. A script is an
  export, not a save: it loses slider bounds and hidden flags, and nothing
  else — measured, not assumed.
- **Document canonical, script generated — and read back two ways.**
  `to_script()` emits runnable magpylib code, patterns included (as the loops
  they mean), **folding the log in order** rather than declaring everything up
  front: where an object is created relative to the steps around it is part of
  the scene, and an object added to an already-patterned group must not end up
  inside every copy. `apply_script()` **parses** it when it is still in that
  shape, so variables, event order and arrangements survive and the whole
  document round-trips byte-identically; anything else — a loop of your own, a
  helper, numpy — is *executed* with `show()` intercepted, as `load_script()`
  always did, and what that flattens is reported.

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
| patterns | `duplicate_around` (circular) · `duplicate_along` (linear; twice = a grid) · `mirror` |
| variables | `get_variables` · `set_variable` · `set_variable_bounds` · `remove_variable` · `unknown_variables` · `expression_help` · `check_expression` |
| history | `get_events` · `edit_event` · `move_event` · `remove_event` · `set_rollback` |
| view | `get_figure` (3D) · `get_field_figure` (along a sensor path) · `get_field_map` (plane heatmap) · `get_sweep_figure` |
| field | `get_field` — summed B/H at points or along a sensor · `sweep` — the field against a variable |
| undo | `undo` · `redo` · `goto_history` |
| I/O | `load_scene` · `load_script` · `apply_script` · `load_captured` · `list_examples` · `load_example` · `clear_scene` · `to_dict` · `to_script` |
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

The engine is covered by 104 tests against both magpylib versions
(`.venv/bin/python -m pytest -q`).

The extension is checked at three levels, all wired into `npm run compile` so
they run before every F5 and before packaging: type-checking and ESLint over
both the host code and the webview scripts; two contribution checks (every
declared command registered, every menu clause matching a context value the
tree can set, every palette entry safe to invoke with no argument); and a DOM
harness that runs a panel's real script against a real engine
(`npm run inspect -- halbach`). On top of that, `npm test` runs eleven
integration tests **inside a real Extension Development Host** — activation,
the engine subprocess answering through the virtual `scene.json`, a removal
taking a pattern's copies with it, the script tab applying an edit on save,
and the whole save/open path: a file that opens back as the same scene, a
save that goes to the file it came from without asking, a document from a
newer version being refused without disturbing the open one, and the crash
backup being written and restorable.

Both suites and the packaging run in CI on every push, the engine against
**both magpylib versions** — the claim above used to be checked by hand.
Pushing a `v*` tag builds the `.vsix` and attaches it to a GitHub release.

It is **not on the Marketplace**, and the reason is not polish: the engine is
not on PyPI, so installing the extension there would be followed by "now go
and pip install this git URL" — a failure at first contact, before anyone sees
a feature. Publishing the engine is what unblocks that, and lets the extension
offer to install it into the interpreter you point at instead of telling you
to. Until then, a release asset is the honest channel. (`publisher` is
`magpylib`, which this repo's org now makes right — but a Marketplace
publisher is registered through Azure DevOps and is a separate thing from the
GitHub org, so it still has to be created before a first `vsce publish`.)

See [CONTINUE.md](CONTINUE.md) for the current state and what is next.

## License

BSD-3-Clause — the same as the other packages built on [magpylib][mplib]
([magpylib-force][force], [magpylib-material-response][matresp]). The core
library itself is BSD-2-Clause; its satellites are all 3-clause, and this is
one of those. See [LICENSE](LICENSE).

[mplib]: https://github.com/magpylib/magpylib
[force]: https://github.com/magpylib/magpylib-force
[matresp]: https://github.com/magpylib/magpylib-material-response
