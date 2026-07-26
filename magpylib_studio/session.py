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
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json

import magpylib as magpy
from magpylib._src.defaults.defaults_classes import default_settings
from magpylib._src.style import get_style

DEFAULT_SCENE = {
    "objects": [
        {
            "id": "cube",
            "type": "magnet.Cuboid",
            "params": {
                "polarization": [0, 0, 1],
                "dimension": [1, 1, 1],
                "position": [0, 0, 0],
            },
            "style": {"label": "Cube"},
        },
        {
            "id": "cyl",
            "type": "magnet.Cylinder",
            "params": {
                "polarization": [1, 0, 0],
                "dimension": [1, 1],
                "position": [2.5, 0, 0],
            },
            "style": {"label": "Cyl"},
        },
    ]
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
        self.doc = scene if scene is not None else json.loads(json.dumps(DEFAULT_SCENE))
        self._objs: dict[str, object] = {}
        self._build()

    def _build(self):
        objs = []
        for spec in self.doc["objects"]:
            cls = _resolve_type(spec["type"])
            obj = cls(**dict(spec.get("params", {})))
            for path, value in spec.get("style", {}).items():
                obj.style.set(path, value)  # dotted-path set (same as the GUI/LLM)
            self._objs[spec["id"]] = obj
            objs.append(obj)
        self.scene = magpy.Collection(*objs)

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
