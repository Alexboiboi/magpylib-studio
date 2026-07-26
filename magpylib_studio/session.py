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
  get_figure()                         -> plotly figure JSON of the whole scene
  apply_edit(object_id, path, value)   -> {"ok": bool, "error"?: str}
  add_object(object_id, type, params?, style?, rotations?) -> {"ok": bool, ...}
  remove_object(object_id)             -> {"ok": bool, "error"?: str}
  set_param(object_id, name, value)    -> {"ok": bool, "error"?: str}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_example()                       -> {"ok": bool, "error"?: str}
  clear_scene()                        -> {"ok": bool, "error"?: str}
  batch(operations)                    -> {"ok": bool, "results": [...]}
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json

import magpylib as magpy
from magpylib._src.defaults.defaults_classes import default_settings
from magpylib._src.style import get_style


def example_scene():
    """The built-in showcase scene: two stacked Halbach rings of 10 rotated
    cuboids each (each cuboid orbited AND spun by the ring angle, the classic
    magpylib docs pattern), plus a sensor path along the bore axis."""
    objects = []
    n = 10
    for ring, z in ((1, 0.0), (2, 1.5)):
        for i in range(n):
            angle = 360 * i / n
            objects.append(
                {
                    "id": f"r{ring}m{i + 1:02d}",
                    "type": "magnet.Cuboid",
                    "params": {
                        "dimension": [1, 1, 1],
                        "polarization": [1, 0, 0],
                        "position": [2.3, 0, z],
                    },
                    "rotations": [
                        {"angle": angle, "axis": "z", "anchor": 0},
                        {"angle": angle, "axis": "z"},
                    ],
                    "style": {"label": f"Ring{ring} {i + 1:02d}"},
                }
            )
    objects.append(
        {
            "id": "sensor",
            "type": "Sensor",
            "params": {
                "position": [[0, 0, round(-1.5 + 4.5 * i / 24, 3)] for i in range(25)]
            },
            "style": {"label": "Sensor"},
        }
    )
    return {"objects": objects}


# Operations allowed inside batch() — mutating, per-object (plus clear).
_BATCHABLE = {
    "apply_edit",
    "add_object",
    "remove_object",
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
        self._build()

    def _build(self):
        self._objs = {}
        objs = []
        for spec in self.doc["objects"]:
            cls = _resolve_type(spec["type"])
            obj = cls(**dict(spec.get("params", {})))
            # optional post-construction rotations, applied in order:
            # {"angle": deg, "axis": "z", "anchor": 0 | [x,y,z]}; no anchor
            # rotates in place (how Halbach-style patterns are expressed)
            for rot in spec.get("rotations", []):
                obj.rotate_from_angax(rot["angle"], rot["axis"], anchor=rot.get("anchor"))
            for path, value in spec.get("style", {}).items():
                obj.style.set(path, value)  # dotted-path set (same as the GUI/LLM)
            self._objs[spec["id"]] = obj
            objs.append(obj)
        self.scene = magpy.Collection(*objs)

    def _mutate_doc(self, mutate):
        """Apply `mutate(doc)` and rebuild; on any failure restore the old doc.

        The doc stays the single source of truth: structural edits go through
        the same build path as startup, so a doc that builds once always
        rebuilds — bad mutations are rolled back and reported, never applied.
        """
        snapshot = json.loads(json.dumps(self.doc))
        try:
            mutate(self.doc)
            self._build()
        except Exception as e:  # noqa: BLE001 - report every failure to the caller
            self.doc = snapshot
            self._build()
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def _spec(self, object_id):
        for spec in self.doc["objects"]:
            if spec["id"] == object_id:
                return spec
        raise KeyError(f"unknown object id {object_id!r}")

    # --- introspection -----------------------------------------------------
    def list_objects(self):
        return [
            {
                "id": s["id"],
                "type": s["type"],
                "label": self._objs[s["id"]].style.label or s["type"],
            }
            for s in self.doc["objects"]
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

    def get_figure(self):
        fig = magpy.show(self.scene, backend="plotly", return_fig=True)
        return json.loads(fig.to_json())  # to_json handles numpy/bdata

    # --- editing -----------------------------------------------------------
    def apply_edit(self, object_id, path, value):
        obj = self._objs[object_id]
        try:
            obj.style.set(path, value)
        except Exception as e:  # noqa: BLE001 - report validation errors, don't crash
            return {"ok": False, "error": str(e)}
        self._spec(object_id)["style"] = dict(obj.style.set_values())  # keep doc synced
        return {"ok": True}

    # --- scene structure ---------------------------------------------------
    def add_object(self, object_id, type, params=None, style=None, rotations=None):
        if any(s["id"] == object_id for s in self.doc["objects"]):
            return {"ok": False, "error": f"object id {object_id!r} already exists"}

        def mutate(doc):
            spec = {
                "id": object_id,
                "type": type,
                "params": params or {},
                "style": style or {},
            }
            if rotations:
                spec["rotations"] = rotations
            doc["objects"].append(spec)

        return self._mutate_doc(mutate)

    def remove_object(self, object_id):
        self._spec(object_id)  # raise early on unknown id

        def mutate(doc):
            doc["objects"] = [s for s in doc["objects"] if s["id"] != object_id]

        return self._mutate_doc(mutate)

    def set_param(self, object_id, name, value):
        """Set a constructor parameter (position, dimension, polarization, ...)."""
        spec = self._spec(object_id)

        def mutate(doc):
            spec.setdefault("params", {})[name] = value

        return self._mutate_doc(mutate)

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

        return self._mutate_doc(mutate)

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

        return self._mutate_doc(mutate)

    def load_example(self):
        """Load the built-in example scene (Halbach ring, coil pair, sensor)."""
        return self.load_scene(example_scene())

    def clear_scene(self):
        """Remove every object at once."""
        return self.load_scene({"objects": []})

    def batch(self, operations):
        """Apply several mutating operations in one call, e.g.
        [{"method": "add_object", "params": {...}}, ...]. Continues past
        failures; per-operation results let the caller fix and retry."""
        results = []
        for op in operations:
            method = op.get("method")
            params = op.get("params") or {}
            if method not in _BATCHABLE:
                results.append({"ok": False, "error": f"method {method!r} not batchable"})
                continue
            try:
                results.append(getattr(self, method)(**params))
            except Exception as e:  # noqa: BLE001 - keep going, report per op
                results.append({"ok": False, "error": str(e)})
        return {"ok": all(r["ok"] for r in results), "results": results}

    # --- serialization / round-trip ---------------------------------------
    def to_dict(self):
        return self.doc

    def to_script(self):
        lines = ["import magpylib as magpy", ""]
        names = []
        for s in self.doc["objects"]:
            name = s["id"]
            names.append(name)
            parts = [
                f"{k}={tuple(v) if isinstance(v, list) else v!r}"
                for k, v in s.get("params", {}).items()
            ]
            if s.get("style"):
                parts.append(f"style={_nest(s['style'])!r}")
            lines.append(f"{name} = magpy.{s['type']}({', '.join(parts)})")
            for rot in s.get("rotations", []):
                args = f"{rot['angle']!r}, {rot['axis']!r}"
                anchor = rot.get("anchor")
                if anchor is not None:
                    args += f", anchor={tuple(anchor) if isinstance(anchor, list) else anchor!r}"
                lines.append(f"{name}.rotate_from_angax({args})")
        lines += ["", f"scene = magpy.Collection({', '.join(names)})",
                  "scene.show(backend='plotly')"]
        return "\n".join(lines)
