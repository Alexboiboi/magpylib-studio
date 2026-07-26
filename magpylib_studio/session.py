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
  move(object_id, displacement, start?)          -> {"ok": bool, ...} (list = path)
  rotate(object_id, angle, axis?, anchor?, start?) -> {"ok": bool, ...} (list = path)
  set_transform(object_id, position?, orientation?) -> {"ok": bool, ...} (absolute)
  clear_path(object_id, index?)        -> {"ok": bool, "error"?: str}
  get_transform(object_id)             -> {position, orientation, path_length, ...}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_script(path, scene?)            -> {"ok", "scene", "scenes": [labels], ...}
  load_captured(scene)                 -> same (switch between captured scenes)
  load_example()                       -> {"ok": bool, "error"?: str}
  clear_scene()                        -> {"ok": bool, "error"?: str}
  batch(operations)                    -> {"ok": bool, "results": [...]} (1 undo step)
  undo(steps?) / redo(steps?)          -> {"ok": bool, "error"?: str}
  get_history()                        -> {"entries": [...], "current": int, ...}
  goto_history(index)                  -> {"ok": bool, "error"?: str}
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json

import magpylib as magpy
import numpy as np
from magpylib._src.defaults.defaults_classes import default_settings
from magpylib._src.style import get_style
from scipy.spatial.transform import Rotation as R


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
    "move",
    "rotate",
    "set_transform",
    "clear_path",
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
        self._captured_scenes: list[dict] = []  # from the last load_script
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
        # optional post-construction rotations, applied in order. Two forms:
        # {"angle": deg|[deg,...], "axis": "z"|[x,y,z], "anchor"?, "start"?}
        #   -> rotate_from_angax (no anchor rotates in place; on a Collection
        #      rotates the whole group; an angle list builds a path)
        # {"rotvec": [[x,y,z],...], "anchor"?, "start"?}
        #   -> rotate_from_rotvec, elementwise over the path (how imported
        #      orientation paths are reproduced exactly)
        for rot in spec.get("rotations", []):
            kwargs = {"anchor": rot.get("anchor")}
            if "start" in rot:
                kwargs["start"] = rot["start"]
            if "rotvec" in rot:
                obj.rotate_from_rotvec(rot["rotvec"], degrees=True, **kwargs)
            else:
                obj.rotate_from_angax(rot["angle"], rot["axis"], **kwargs)
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

    def get_transform(self, object_id):
        """World pose of an object, for the inspector's transform widgets."""
        obj = self._objs[object_id]
        position = np.atleast_2d(np.array(obj.position, dtype=float))
        rotvec = np.atleast_2d(obj.orientation.as_rotvec(degrees=True))
        euler = np.atleast_2d(obj.orientation.as_euler("xyz", degrees=True))
        return {
            "position": position[-1].round(9).tolist(),
            "orientation": rotvec[-1].round(9).tolist(),
            "euler": euler[-1].round(9).tolist(),
            "path_length": len(position),
            "path": position.round(9).tolist() if len(position) > 1 else None,
        }

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

    def _rebase(self, spec, world_pos, world_rot):
        """Rewrite a spec's position/rotations so that, after its (new)
        ancestors' build-time transforms, the object lands back on the given
        world pose. Reparenting must not teleport an object — a Collection's
        group rotation would otherwise be applied on top of coordinates that
        already included the old parent's."""
        params = spec.setdefault("params", {})
        # Probe: build the object at the origin with identity orientation, so
        # its resulting pose IS the transform its ancestors apply.
        params["position"] = [0.0, 0.0, 0.0]
        spec.pop("rotations", None)
        self._build()
        probe = self._objs[spec["id"]]
        frame_pos = np.array(probe.position, dtype=float)
        frame_rot = probe.orientation

        local_pos = frame_rot.inv().apply(world_pos - frame_pos)
        local_rot = frame_rot.inv() * world_rot
        params["position"] = np.round(local_pos, 9).tolist()
        rotvec = np.round(local_rot.as_rotvec(degrees=True), 9)
        if np.linalg.norm(rotvec) > 1e-9:
            entry = {"rotvec": rotvec.tolist()}
            if rotvec.ndim > 1:
                entry["start"] = 0
            spec["rotations"] = [entry]

    # --- transforms --------------------------------------------------------
    def _transform(self, object_id, apply, label):
        """Run a magpylib transform on the live object, then rebase its spec
        to the resulting pose — magpylib's own semantics (paths, anchors,
        `start`) for free, with the document staying canonical."""
        obj = self._objs[object_id]
        spec = self._spec(object_id)

        def mutate(doc):
            apply(obj)
            self._rebase(
                spec, np.array(obj.position, dtype=float), obj.orientation
            )

        return self._mutate_doc(mutate, label)

    def move(self, object_id, displacement, start="auto"):
        """Move by `displacement` (relative). A list of displacements
        [[dx,dy,dz], ...] creates/extends a position path."""
        return self._transform(
            object_id,
            lambda o: o.move(displacement, start=start),
            f"move {object_id}",
        )

    def rotate(self, object_id, angle, axis="z", anchor=None, start="auto"):
        """Rotate by `angle` degrees about `axis` (relative). `anchor` orbits
        that point (0 = origin); a list of angles creates/extends a path."""
        return self._transform(
            object_id,
            lambda o: o.rotate_from_angax(angle, axis, anchor=anchor, start=start),
            f"rotate {object_id}",
        )

    def set_transform(self, object_id, position=None, orientation=None):
        """Set the absolute pose in world coordinates. `position` is [x,y,z]
        or a path [[x,y,z], ...]; `orientation` is a rotation vector in
        degrees [rx,ry,rz] (or a list of them)."""
        def apply(o):
            if position is not None:
                o.position = position
            if orientation is not None:
                o.orientation = R.from_rotvec(orientation, degrees=True)

        return self._transform(object_id, apply, f"set transform {object_id}")

    def clear_path(self, object_id, index=-1):
        """Reduce a path to a single step (default: its last)."""
        def apply(o):
            o.position = np.atleast_2d(o.position)[index]
            rot = o.orientation
            o.orientation = rot[index] if len(np.atleast_2d(rot.as_rotvec())) > 1 else rot

        return self._transform(object_id, apply, f"clear path {object_id}")

    def move_object(self, object_id, parent=None):
        """Reparent an object: into a Collection, or to the root
        (parent=None). Position and orientation in world coordinates are
        preserved across the move."""
        spec = self._spec(object_id)
        if parent is not None:
            subtree_ids = {s["id"] for s, _ in self._iter_specs([spec])}
            if parent in subtree_ids:
                return {"ok": False,
                        "error": f"cannot move {object_id!r} into its own subtree"}
            if self._spec(parent)["type"] != "Collection":
                return {"ok": False, "error": f"parent {parent!r} is not a Collection"}
        obj = self._objs[object_id]
        world_pos = np.array(obj.position, dtype=float)
        world_rot = obj.orientation

        def mutate(doc):
            self._container_of(object_id).remove(spec)
            target = (
                doc["objects"] if parent is None
                else self._spec(parent).setdefault("children", [])
            )
            target.append(spec)
            self._rebase(spec, world_pos, world_rot)

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

    def load_script(self, path, scene=0):
        """Import an existing magpylib script by EXECUTING it (same trust as
        the user running it). Every show() call the script makes is captured
        as a scene candidate (that is what its author considered "the
        scene"), plus an "all script objects" fallback when it differs.
        Loads candidate `scene` (default: the first show() call); the rest
        stay cached for load_captured(). Parametric structure flattens."""
        from magpylib_studio import importer

        candidates = []
        try:
            namespace, captured = importer.run_script(path)
            for i, objects in enumerate(captured):
                try:
                    doc, warnings = importer.document_from_objects(objects, namespace)
                except ValueError:
                    continue
                candidates.append({
                    "label": f"show() call {i + 1} ({len(doc['objects'])} top-level)",
                    "doc": doc,
                    "warnings": warnings,
                })
            try:
                doc, warnings = importer.document_from_namespace(namespace)
                if all(c["doc"] != doc for c in candidates):
                    candidates.append({
                        "label": f"all script objects ({len(doc['objects'])} top-level)",
                        "doc": doc,
                        "warnings": warnings,
                    })
            except ValueError:
                pass
        except Exception as e:  # noqa: BLE001 - report script errors, don't crash
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not candidates:
            return {"ok": False, "error": "script produced no magpylib objects"}
        self._captured_scenes = candidates
        return self.load_captured(scene)

    def load_captured(self, scene=0):
        """Load one of the scene candidates cached by the last load_script."""
        if not self._captured_scenes:
            return {"ok": False, "error": "no imported scenes; run load_script first"}
        if not 0 <= scene < len(self._captured_scenes):
            return {"ok": False,
                    "error": f"scene must be 0..{len(self._captured_scenes) - 1}"}
        entry = self._captured_scenes[scene]
        result = self.load_scene(json.loads(json.dumps(entry["doc"])))
        if result["ok"]:
            if not self._history_paused and self._undo:
                self._undo[-1]["label"] = f"import {entry['label']}"
            result["scene"] = scene
            result["scenes"] = [c["label"] for c in self._captured_scenes]
            if entry["warnings"]:
                result["warnings"] = entry["warnings"]
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
        """The session timeline: entry 0 is the initial state, entry i the
        state after the i-th change; `current` is where the scene sits now
        (entries after it are redoable)."""
        labels = [e["label"] for e in self._undo]
        labels += [e["label"] for e in reversed(self._redo)]
        return {
            "entries": [{"index": 0, "label": "Initial state"}]
            + [{"index": i + 1, "label": label} for i, label in enumerate(labels)],
            "current": len(self._undo),
            "undo": [e["label"] for e in self._undo],
            "redo": [e["label"] for e in self._redo],
        }

    def goto_history(self, index):
        """Jump to any point on the timeline (undoing or redoing as needed)."""
        total = len(self._undo) + len(self._redo)
        if not 0 <= index <= total:
            return {"ok": False, "error": f"index must be 0..{total}"}
        current = len(self._undo)
        if index < current:
            return self.undo(current - index)
        if index > current:
            return self.redo(index - current)
        return {"ok": True}

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
                if "rotvec" in rot:
                    method = "rotate_from_rotvec"
                    args = f"{rot['rotvec']!r}, degrees=True"
                else:
                    method = "rotate_from_angax"
                    args = f"{rot['angle']!r}, {rot['axis']!r}"
                anchor = rot.get("anchor")
                if anchor is not None:
                    args += f", anchor={tuple(anchor) if isinstance(anchor, list) else anchor!r}"
                if "start" in rot:
                    args += f", start={rot['start']!r}"
                lines.append(f"{name}.{method}({args})")
            return name

        names = [emit(s) for s in self.doc["objects"]]
        lines += ["", f"scene = magpy.Collection({', '.join(names)})",
                  "scene.show(backend='plotly')"]
        return "\n".join(lines)
