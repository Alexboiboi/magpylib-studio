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
  add_object(object_id, type, params?, style?) -> {"ok": bool, "error"?: str}
  remove_object(object_id)             -> {"ok": bool, "error"?: str}
  set_param(object_id, name, value)    -> {"ok": bool, "error"?: str}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_example()                       -> {"ok": bool, "error"?: str}
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json
import math

import magpylib as magpy
from magpylib._src.defaults.defaults_classes import default_settings
from magpylib._src.style import get_style


def example_scene():
    """The built-in showcase scene: a Halbach-style ring of 12 cuboids
    (polarization rotating at twice the ring angle, after the magpylib docs
    Halbach examples), a coil pair, and a central sensor."""
    objects = []
    for i in range(12):
        a = 2 * math.pi * i / 12
        objects.append(
            {
                "id": f"mag{i + 1:02d}",
                "type": "magnet.Cuboid",
                "params": {
                    "polarization": [
                        round(math.cos(2 * a), 4),
                        round(math.sin(2 * a), 4),
                        0,
                    ],
                    "dimension": [1, 1, 1],
                    "position": [
                        round(3 * math.cos(a), 4),
                        round(3 * math.sin(a), 4),
                        0,
                    ],
                },
                "style": {"label": f"Halbach {i + 1:02d}"},
            }
        )
    for sign, name in ((1, "top"), (-1, "bottom")):
        objects.append(
            {
                "id": f"coil_{name}",
                "type": "current.Circle",
                "params": {"current": 200, "diameter": 9, "position": [0, 0, 2 * sign]},
                "style": {"label": f"Coil {name}"},
            }
        )
    objects.append(
        {
            "id": "sensor",
            "type": "Sensor",
            "params": {"position": [0, 0, 0]},
            "style": {"label": "Sensor"},
        }
    )
    return {"objects": objects}


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
    def add_object(self, object_id, type, params=None, style=None):
        if any(s["id"] == object_id for s in self.doc["objects"]):
            return {"ok": False, "error": f"object id {object_id!r} already exists"}

        def mutate(doc):
            doc["objects"].append(
                {
                    "id": object_id,
                    "type": type,
                    "params": params or {},
                    "style": style or {},
                }
            )

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
        lines += ["", f"scene = magpy.Collection({', '.join(names)})",
                  "scene.show(backend='plotly')"]
        return "\n".join(lines)
