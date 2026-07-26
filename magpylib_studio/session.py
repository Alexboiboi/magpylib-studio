"""Headless magpylib editing engine — the framework-agnostic core of the studio.

A `MagpylibStudioSession` owns a scene built from a structured *document* and
exposes exactly the operations any frontend (VS Code webview, Solara, a CLI…)
needs. The document is the source of truth: every edit updates both the live
magpylib object and the document, so `to_dict()` / `to_script()` always reflect
the current state and can be versioned in git.

Protocol surface (all JSON-serializable in/out):
  list_objects()                       -> [{id, type, label}]
  get_schema(object_id)                -> JSON Schema of the object's style
  get_values(object_id)                -> {"set": {...}, "resolved": {...}}
  get_figure(animation?, template?)    -> plotly figure JSON (frames if animated)
  get_field(sensor_id?, points?, field?) -> {field, unit, points, values, magnitude}
  get_field_figure(output?, animation?, template?) -> 2D plotly JSON (magpylib-rendered)
  apply_edit(object_id, path, value)   -> {"ok": bool, "error"?: str}
  add_object(object_id, type, params?, style?, rotations?, parent?) -> {"ok": ...}
  remove_object(object_id)             -> {"ok": bool, ...} (subtree if Collection)
  move_object(object_id, parent?)      -> {"ok": bool, "error"?: str}
  set_param(object_id, name, value)    -> {"ok": bool, "error"?: str}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_script(path)                    -> {"ok": bool, "warnings"?: [...], ...}
  load_example()                       -> {"ok": bool, "error"?: str}
  clear_scene()                        -> {"ok": bool, "error"?: str}
  batch(operations)                    -> {"ok": bool, "results": [...]} (1 undo step)
  undo(steps?) / redo(steps?)          -> {"ok": bool, "error"?: str}
  get_history()                        -> {"undo": [labels], "redo": [labels]}
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json

import magpylib as magpy
import numpy as np
from magpylib._src.defaults.defaults_classes import default_settings
from magpylib._src.style import get_style


def example_scene():
    """The built-in showcase scene: a nested Halbach stack — two rings of 10
    rotated cuboids each (each cuboid orbited AND spun by the ring angle, the
    classic magpylib docs pattern), ring 2 staggered by a group rotation —
    plus a sensor path along the bore axis."""
    n = 10

    def ring(number, z):
        return {
            "id": f"ring{number}",
            "type": "Collection",
            "style": {"label": f"Ring {number}"},
            "children": [
                {
                    "id": f"r{number}m{i + 1:02d}",
                    "type": "magnet.Cuboid",
                    "params": {
                        "dimension": [1, 1, 1],
                        "polarization": [1, 0, 0],
                        "position": [2.3, 0, z],
                    },
                    "rotations": [
                        {"angle": 360 * i / n, "axis": "z", "anchor": 0},
                        {"angle": 360 * i / n, "axis": "z"},
                    ],
                    "style": {"label": f"Magnet {i + 1:02d}"},
                }
                for i in range(n)
            ],
        }

    ring2 = ring(2, 1.5)
    ring2["rotations"] = [{"angle": 18, "axis": "z", "anchor": 0}]  # stagger
    return {
        "objects": [
            {
                "id": "halbach",
                "type": "Collection",
                "style": {"label": "Halbach stack"},
                "children": [ring(1, 0.0), ring2],
            },
            {
                "id": "sensor",
                "type": "Sensor",
                "params": {
                    "position": [
                        [0, 0, round(-1.5 + 4.5 * i / 24, 3)] for i in range(25)
                    ]
                },
                "style": {"label": "Sensor"},
            },
        ]
    }


# Operations allowed inside batch() — mutating, per-object (plus clear).
_BATCHABLE = {
    "apply_edit",
    "add_object",
    "remove_object",
    "move_object",
    "set_param",
    "reset_style",
    "clear_scene",
}


def _resolve_type(type_str):
    """'magnet.Cuboid' -> magpylib.magnet.Cuboid."""
    obj = magpy
    for part in type_str.split("."):
        obj = getattr(obj, part)
    return obj


def _nest(flat):
    """Dotted-key dict -> nested dict, e.g. {'a.b': 1} -> {'a': {'b': 1}}."""
    root = {}
    for path, value in flat.items():
        node = root
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return root


class MagpylibStudioSession:
    """A live magpylib scene plus the document it was built from."""

    def __init__(self, scene: dict | None = None):
        self.doc = scene if scene is not None else {"objects": []}  # start empty
        self._objs: dict[str, object] = {}
        # In-session undo/redo (durable history stays in git via to_script):
        # each entry is {"label", "doc"} — the doc state BEFORE the change.
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._history_paused = False
        self._build()

    def _record_state(self, label, doc_before):
        """Push a pre-change doc state onto the undo stack (capped)."""
        if self._history_paused:
            return
        self._undo.append({"label": label, "doc": doc_before})
        del self._undo[:-100]
        self._redo.clear()

    def _build(self):
        self._objs = {}
        self.scene = magpy.Collection(
            *[self._build_spec(s) for s in self.doc["objects"]]
        )

    def _build_spec(self, spec):
        """Build one spec (recursing into Collection children) into a live object."""
        if spec["type"] == "Collection":
            children = [self._build_spec(c) for c in spec.get("children", [])]
            obj = magpy.Collection(*children, **dict(spec.get("params", {})))
        else:
            cls = _resolve_type(spec["type"])
            obj = cls(**dict(spec.get("params", {})))
        # optional post-construction rotations, applied in order:
        # {"angle": deg, "axis": "z", "anchor": 0 | [x,y,z]}; no anchor
        # rotates in place; on a Collection this rotates the whole group
        for rot in spec.get("rotations", []):
            obj.rotate_from_angax(rot["angle"], rot["axis"], anchor=rot.get("anchor"))
        for path, value in spec.get("style", {}).items():
            obj.style.set(path, value)  # dotted-path set (same as the GUI/LLM)
        if spec["id"] in self._objs:
            raise ValueError(f"duplicate object id {spec['id']!r}")
        self._objs[spec["id"]] = obj
        return obj

    def _mutate_doc(self, mutate, label="edit"):
        """Apply `mutate(doc)` and rebuild; on any failure restore the old doc.

        The doc stays the single source of truth: structural edits go through
        the same build path as startup, so a doc that builds once always
        rebuilds — bad mutations are rolled back and reported, never applied.
        Successful mutations push the prior state onto the undo stack.
        """
        snapshot = json.loads(json.dumps(self.doc))
        try:
            mutate(self.doc)
            self._build()
        except Exception as e:  # noqa: BLE001 - report every failure to the caller
            self.doc = snapshot
            self._build()
            return {"ok": False, "error": str(e)}
        self._record_state(label, snapshot)
        return {"ok": True}

    def _iter_specs(self, specs=None, parent=None):
        """Depth-first (spec, parent_spec) pairs over the whole document."""
        for spec in self.doc["objects"] if specs is None else specs:
            yield spec, parent
            yield from self._iter_specs(spec.get("children") or [], spec)

    def _spec(self, object_id):
        for spec, _ in self._iter_specs():
            if spec["id"] == object_id:
                return spec
        raise KeyError(f"unknown object id {object_id!r}")

    def _container_of(self, object_id):
        """The list (doc root or a Collection's children) holding this spec."""
        def search(lst):
            for s in lst:
                if s["id"] == object_id:
                    return lst
                found = search(s.get("children") or [])
                if found is not None:
                    return found
            return None

        found = search(self.doc["objects"])
        if found is None:
            raise KeyError(f"unknown object id {object_id!r}")
        return found

    # --- introspection -----------------------------------------------------
    def list_objects(self):
        return [
            {
                "id": s["id"],
                "type": s["type"],
                "label": self._objs[s["id"]].style.label or s["type"],
                "parent": p["id"] if p else None,
            }
            for s, p in self._iter_specs()
        ]

    def get_schema(self, object_id):
        return type(self._objs[object_id].style).schema()

    def get_values(self, object_id):
        obj = self._objs[object_id]
        resolved = get_style(obj, default_settings)
        return {
            "set": obj.style.set_values(),  # explicitly set (dotted keys)
            "resolved": resolved.as_dict(flatten=True),  # effective values
        }

    def get_figure(self, animation=False, template=None):
        """Figure JSON; animation=True animates paths (plotly frames + play
        button). magpylib falls back to a static plot if nothing has a path.
        template is a plotly template name ('plotly_dark', 'plotly_white', …) —
        resolved here because plotly.js has no named-template registry."""
        fig = magpy.show(
            self.scene, backend="plotly", animation=animation, return_fig=True
        )
        if template:
            fig.layout.template = template
        return json.loads(fig.to_json())  # to_json handles numpy/bdata

    # --- field evaluation --------------------------------------------------
    def _leaf_sources(self):
        """All field sources (excludes Sensors; Collections are just groups —
        using leaves avoids counting an object twice)."""
        return [
            obj
            for obj in self._objs.values()
            if not isinstance(obj, magpy.Collection | magpy.Sensor)
        ]

    def get_field(self, sensor_id=None, points=None, field="B"):
        """Total field of all sources, summed, at the given observers.

        Observers: explicit `points` [[x,y,z], ...] (m), else the sensor with
        `sensor_id`, else the first sensor in the scene (its whole path).
        Returns {"field", "unit", "points", "values", "magnitude"} in SI.
        """
        if field not in ("B", "H"):
            raise ValueError(f"field must be 'B' or 'H', got {field!r}")
        sources = self._leaf_sources()
        if not sources:
            raise ValueError("scene has no field sources")
        if points is not None:
            observer = pts = np.atleast_2d(np.array(points, dtype=float))
        else:
            sensor = None
            if sensor_id is not None:
                sensor = self._objs[sensor_id]
                if not isinstance(sensor, magpy.Sensor):
                    raise ValueError(f"{sensor_id!r} is not a Sensor")
            else:
                sensor = next(
                    (o for o in self._objs.values() if isinstance(o, magpy.Sensor)),
                    None,
                )
                if sensor is None:
                    raise ValueError("scene has no sensor; pass points instead")
            observer = sensor
            pts = np.atleast_2d(sensor.position)
        func = magpy.getB if field == "B" else magpy.getH
        values = np.atleast_2d(func(sources, observer, sumup=True))
        return {
            "field": field,
            "unit": "T" if field == "B" else "A/m",
            "points": pts.tolist(),
            "values": values.tolist(),
            "magnitude": np.linalg.norm(values, axis=-1).tolist(),
        }

    def get_field_figure(self, output="B", animation=False, template=None):
        """2D field plot rendered by magpylib itself (`show(output=...)`):
        field at the scene's sensors along their paths. `output` is e.g.
        "B", "Bx", "Bxy", "H", or a list of those (magpylib semantics);
        animation animates the path like the 3D view."""
        fig = magpy.show(
            self.scene,
            backend="plotly",
            output=output,
            animation=animation,
            return_fig=True,
        )
        if isinstance(output, str):  # magpylib leaves the axes untitled
            unit = "T" if output.startswith("B") else "A/m"
            fig.update_layout(
                xaxis_title="path index", yaxis_title=f"{output} ({unit})"
            )
        if template:
            fig.layout.template = template
        return json.loads(fig.to_json())

    # --- editing -----------------------------------------------------------
    def apply_edit(self, object_id, path, value):
        obj = self._objs[object_id]
        before = json.loads(json.dumps(self.doc))
        try:
            obj.style.set(path, value)
        except Exception as e:  # noqa: BLE001 - report validation errors, don't crash
            return {"ok": False, "error": str(e)}
        self._spec(object_id)["style"] = dict(obj.style.set_values())  # keep doc synced
        self._record_state(f"edit {object_id} {path}", before)
        return {"ok": True}

    # --- scene structure ---------------------------------------------------
    def add_object(self, object_id, type, params=None, style=None, rotations=None,
                   parent=None):
        if any(s["id"] == object_id for s, _ in self._iter_specs()):
            return {"ok": False, "error": f"object id {object_id!r} already exists"}
        if parent is not None and self._spec(parent)["type"] != "Collection":
            return {"ok": False, "error": f"parent {parent!r} is not a Collection"}

        def mutate(doc):
            spec = {
                "id": object_id,
                "type": type,
                "params": params or {},
                "style": style or {},
            }
            if type == "Collection":
                spec["children"] = []
            if rotations:
                spec["rotations"] = rotations
            target = (
                doc["objects"] if parent is None
                else self._spec(parent).setdefault("children", [])
            )
            target.append(spec)

        return self._mutate_doc(mutate, f"add {object_id}")

    def remove_object(self, object_id):
        """Remove an object; removing a Collection removes its whole subtree."""
        spec = self._spec(object_id)  # raise early on unknown id

        def mutate(doc):
            self._container_of(object_id).remove(spec)

        return self._mutate_doc(mutate, f"remove {object_id}")

    def move_object(self, object_id, parent=None):
        """Reparent an object: into a Collection, or to the root (parent=None)."""
        spec = self._spec(object_id)
        if parent is not None:
            subtree_ids = {s["id"] for s, _ in self._iter_specs([spec])}
            if parent in subtree_ids:
                return {"ok": False,
                        "error": f"cannot move {object_id!r} into its own subtree"}
            if self._spec(parent)["type"] != "Collection":
                return {"ok": False, "error": f"parent {parent!r} is not a Collection"}

        def mutate(doc):
            self._container_of(object_id).remove(spec)
            target = (
                doc["objects"] if parent is None
                else self._spec(parent).setdefault("children", [])
            )
            target.append(spec)

        return self._mutate_doc(mutate, f"move {object_id}")

    def set_param(self, object_id, name, value):
        """Set a constructor parameter (position, dimension, polarization, ...)."""
        spec = self._spec(object_id)

        def mutate(doc):
            spec.setdefault("params", {})[name] = value

        return self._mutate_doc(mutate, f"set {object_id}.{name}")

    def reset_style(self, object_id, path=None):
        """Reset one style path (or all styles) to defaults by dropping it
        from the doc and rebuilding — the property tree has no unset."""
        spec = self._spec(object_id)
        if path is not None and path not in spec.get("style", {}):
            return {"ok": False, "error": f"style path {path!r} is not set on {object_id!r}"}

        def mutate(doc):
            if path is None:
                spec["style"] = {}
            else:
                del spec["style"][path]

        return self._mutate_doc(mutate, f"reset {object_id} {path or 'style'}")

    def load_scene(self, scene):
        """Replace the whole document. `scene` is a document dict or a path to
        a JSON file containing one. (Script -> document is deferred by design.)"""
        if isinstance(scene, str):
            try:
                with open(scene, encoding="utf-8") as f:
                    scene = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                return {"ok": False, "error": str(e)}

        def mutate(doc):
            self.doc = json.loads(json.dumps(scene))

        return self._mutate_doc(mutate, "load scene")

    def load_script(self, path):
        """Import an existing magpylib script by EXECUTING it (same trust as
        the user running it) and introspecting the resulting objects into a
        document. Parametric structure flattens; see importer.py."""
        from magpylib_studio import importer

        try:
            namespace = importer.run_script(path)
            doc, warnings = importer.document_from_namespace(namespace)
        except Exception as e:  # noqa: BLE001 - report script errors, don't crash
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result = self.load_scene(doc)
        if result["ok"]:
            if not self._history_paused and self._undo:
                self._undo[-1]["label"] = "import script"
            if warnings:
                result["warnings"] = warnings
        return result

    def load_example(self):
        """Load the built-in example scene (Halbach ring, coil pair, sensor)."""
        result = self.load_scene(example_scene())
        if result["ok"] and not self._history_paused and self._undo:
            self._undo[-1]["label"] = "load example"
        return result

    def clear_scene(self):
        """Remove every object at once."""
        result = self.load_scene({"objects": []})
        if result["ok"] and not self._history_paused and self._undo:
            self._undo[-1]["label"] = "clear scene"
        return result

    def batch(self, operations):
        """Apply several mutating operations in one call, e.g.
        [{"method": "add_object", "params": {...}}, ...]. Continues past
        failures; per-operation results let the caller fix and retry.
        One undo step for the whole batch."""
        before = json.loads(json.dumps(self.doc))
        self._history_paused = True
        try:
            results = []
            for op in operations:
                method = op.get("method")
                params = op.get("params") or {}
                if method not in _BATCHABLE:
                    results.append(
                        {"ok": False, "error": f"method {method!r} not batchable"}
                    )
                    continue
                try:
                    results.append(getattr(self, method)(**params))
                except Exception as e:  # noqa: BLE001 - keep going, report per op
                    results.append({"ok": False, "error": str(e)})
        finally:
            self._history_paused = False
        if any(r["ok"] for r in results):  # something changed -> one undo step
            self._record_state(f"batch ({len(operations)} ops)", before)
        return {"ok": all(r["ok"] for r in results), "results": results}

    # --- undo / redo -------------------------------------------------------
    def undo(self, steps=1):
        """Step back through the in-session history (git stays the durable
        history; this is for quick reverts of slider drags / LLM edits)."""
        for _ in range(steps):
            if not self._undo:
                return {"ok": False, "error": "nothing to undo"}
            entry = self._undo.pop()
            self._redo.append(
                {"label": entry["label"], "doc": json.loads(json.dumps(self.doc))}
            )
            self.doc = entry["doc"]
            self._build()  # snapshots built before, so this cannot fail
        return {"ok": True}

    def redo(self, steps=1):
        for _ in range(steps):
            if not self._redo:
                return {"ok": False, "error": "nothing to redo"}
            entry = self._redo.pop()
            self._undo.append(
                {"label": entry["label"], "doc": json.loads(json.dumps(self.doc))}
            )
            self.doc = entry["doc"]
            self._build()
        return {"ok": True}

    def get_history(self):
        """Labels of what undo/redo would revert, most recent last."""
        return {
            "undo": [e["label"] for e in self._undo],
            "redo": [e["label"] for e in self._redo],
        }

    # --- serialization / round-trip ---------------------------------------
    def to_dict(self):
        return self.doc

    def to_script(self):
        lines = ["import magpylib as magpy", ""]

        def emit(spec):
            """Emit child definitions first, then this object; return its name."""
            name = spec["id"]
            parts = [emit(c) for c in spec.get("children") or []]
            parts += [
                f"{k}={tuple(v) if isinstance(v, list) else v!r}"
                for k, v in spec.get("params", {}).items()
            ]
            if spec.get("style"):
                parts.append(f"style={_nest(spec['style'])!r}")
            ctor = "Collection" if spec["type"] == "Collection" else spec["type"]
            lines.append(f"{name} = magpy.{ctor}({', '.join(parts)})")
            for rot in spec.get("rotations", []):
                args = f"{rot['angle']!r}, {rot['axis']!r}"
                anchor = rot.get("anchor")
                if anchor is not None:
                    args += f", anchor={tuple(anchor) if isinstance(anchor, list) else anchor!r}"
                lines.append(f"{name}.rotate_from_angax({args})")
            return name

        names = [emit(s) for s in self.doc["objects"]]
        lines += ["", f"scene = magpy.Collection({', '.join(names)})",
                  "scene.show(backend='plotly')"]
        return "\n".join(lines)
