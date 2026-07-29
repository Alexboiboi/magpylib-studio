"""Headless magpylib editing engine — the framework-agnostic core of the studio.

A `MagpylibStudioSession` owns a scene built from a structured *document* and
exposes exactly the operations any frontend (VS Code webview, Solara, a CLI…)
needs. The document is the source of truth: every edit updates both the live
magpylib object and the document, so `to_dict()` / `to_script()` always reflect
the current state and can be versioned in git.

Protocol surface (all JSON-serializable in/out):
  list_objects()                       -> [{id, type, label}]
  get_schema(object_id)                -> JSON Schema of the object's style
  get_values(object_id)                -> {"set": {...}, "resolved": {...}} (style)
  get_params(object_id)                -> [{name, value, kind, doc}] (physics)
  get_figure(animation?, template?)    -> plotly figure JSON (frames if animated)
  get_field(sensor_id?, points?, field?) -> {field, unit, points, values, magnitude}
  get_field_figure(output?, animation?, template?) -> 2D plotly JSON (magpylib-rendered)
  get_field_map(plane?, offset?, component?, log?, sensor_id?, …) -> heatmap JSON
  set_pixel_grid(object_id, plane?, size?, resolution?, offset?) -> {"ok": bool}
  apply_edit(object_id, path, value)   -> {"ok": bool, "error"?: str}
  add_object(object_id, type, params?, style?, rotations?, parent?) -> {"ok": ...}
  remove_object(object_id)             -> {"ok": bool, ...} (subtree if Collection)
  move_object(object_id, parent?)      -> {"ok": bool, "error"?: str}
  set_param(object_id, name, value)    -> {"ok": bool, "error"?: str}
  move(object_id, displacement, start?)          -> {"ok": bool, ...} (list = path)
  rotate(object_id, angle, axis?, anchor?, start?) -> {"ok": bool, ...} (list = path)
  set_transform(object_id, position?, orientation?) -> {"ok": bool, ...} (absolute)
  clear_path(object_id, index?)        -> {"ok": bool, "error"?: str}
  duplicate_around(object_id, count, axis?, anchor?, spin?) -> {"ok": bool, ...}
  get_transform(object_id)             -> {position, orientation, path_length, ...}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_script(path, scene?)            -> {"ok", "scene", "scenes": [labels], ...}
  load_captured(scene)                 -> same (switch between captured scenes)
  apply_script(path)                   -> {"ok", "warnings"?} (edited to_script back in)
  load_example()                       -> {"ok": bool, "error"?: str}
  clear_scene()                        -> {"ok": bool, "error"?: str}
  batch(operations)                    -> {"ok": bool, "results": [...]} (1 undo step)
  undo(steps?) / redo(steps?)          -> {"ok": bool, "error"?: str}
  get_history()                        -> {"entries": [...], "current": int, ...}
  goto_history(index)                  -> {"ok": bool, "error"?: str}
  get_variables()                      -> {"variables": [{name, expression, value}]}
  unknown_variables(values)            -> {"unknown": [names not defined yet]}
  set_variable(name, value)            -> {"ok": bool, "error"?: str}
  set_variable_bounds(name, min?, max?, soft_min?, soft_max?) -> {"ok": bool, ...}
  remove_variable(name)                -> {"ok": bool, "error"?: str}
  sweep(variable, values, sensor_id?, points?, field?) -> {"ok", "steps": [...]}
  get_sweep_figure(variable, values, …) -> plotly line-plot JSON
  get_events()                         -> {"events": [{index, id, target, source}]}
  edit_event(event_id, changes)        -> {"ok": bool, "error"?: str}
  remove_event(event_id)               -> {"ok": bool, "error"?: str}
  move_event(event_id, index)          -> {"ok": bool, "error"?: str}
  to_dict()                            -> the scene document
  to_script()                          -> equivalent magpylib Python code
"""

from __future__ import annotations

import json
import re

import magpylib as magpy
import numpy as np
import plotly.graph_objects as go
from scipy.spatial.transform import Rotation as R

from magpylib_studio import expressions, style_compat


def example_scene():
    """The built-in showcase scene: a nested Halbach stack — two rings of 10
    rotated cuboids each (each cuboid orbited AND spun by the ring angle, the
    classic magpylib docs pattern), ring 2 staggered by a group rotation —
    plus a sensor path along the bore axis.

    Both rings are written in terms of `radius`, and the upper one sits at
    `gap`, so the scene arrives parametric: dragging either in the Variables
    panel moves twenty magnets at once, which is the thing worth discovering
    first. Their soft bounds are the range worth exploring — below a radius
    of about 1.6 the ten unit cubes would have to overlap (2πr < 10) — while
    the hard bounds only rule out the physically impossible.
    """
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
                        "position": ["=radius", 0, z],
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

    ring2 = ring(2, "=gap")
    ring2["rotations"] = [{"angle": 18, "axis": "z", "anchor": 0}]  # stagger
    return {
        "variables": {"radius": 2.3, "gap": 1.5},
        "variable_bounds": {
            "radius": {"min": 0.5, "max": 8, "soft_min": 1.6, "soft_max": 4},
            "gap": {"min": 0, "max": 6, "soft_min": 1, "soft_max": 3},
        },
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


# Editable constructor parameters, introspected off the live object.
# `magnetization` is absent on purpose: it is derived from polarization.
_PARAM_ATTRS = (
    "polarization",
    "magnetization",
    "dimension",
    "diameter",
    "vertices",
    "faces",
    "current",
    "moment",
    "pixel",
)

_PARAM_DOCS = {
    "polarization": "magnetic polarization J (T), in object coordinates",
    "magnetization": "magnetization M (A/m) — derived from polarization",
    "dimension": "size; Cuboid (a,b,c) m · Cylinder (d,h) m · "
                 "CylinderSegment (r1,r2,h,phi1,phi2) m/deg",
    "diameter": "diameter (m)",
    "vertices": "corner/path points (m)",
    "faces": "triangle indices into vertices",
    "current": "electrical current (A)",
    "moment": "magnetic moment (A·m²)",
    "pixel": "sensor pixel positions in local coordinates (m)",
}


# Style switches that hide an object without removing it from the figure —
# magpylib still assigns it a colour, so the others keep theirs.
_HIDE_STYLE = {"model3d.showdefault": False, "path.show": False}


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
    # a parametric scene is built in one go: variables, then the objects
    # written in terms of them, then the arrangements
    "set_variable",
    "remove_variable",
    "duplicate_around",
}


def _plain(value):
    """numpy scalars/arrays -> plain Python, so the document stays JSON-safe
    (and generated scripts contain literals, not np.float64 reprs)."""
    if isinstance(value, np.generic | np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _spec_ops(spec):
    """Transform ops to replay on a freshly built object: the spec's
    `transforms` list, plus legacy `rotations` entries (same semantics)."""
    ops = []
    for rot in spec.get("rotations", []):
        kind = "rotate_from_rotvec" if "rotvec" in rot else "rotate_from_angax"
        ops.append({"op": kind, **rot})
    return ops + list(spec.get("transforms", []))


def _walk_specs(specs):
    """Depth-first over a plain document's specs (no session needed)."""
    for spec in specs:
        yield spec
        yield from _walk_specs(spec.get("children") or [])


_DOC_KEYS = ("variables", "variable_bounds", "objects", "events")


def _canonical(doc):
    """One spelling per value, whichever way the document was built.

    Empty `params`/`style`/`variables` are dropped, and expressions are put in
    canonical spacing — otherwise the same scene compares unequal to itself
    depending on whether it was built up through the API or read back from its
    script, and the script tab churns on the first save.
    """
    for spec in _walk_specs(doc.get("objects") or []):
        for key in ("params", "style"):
            if spec.get(key) == {}:
                del spec[key]
        if "params" in spec:
            spec["params"] = expressions.normalized(spec["params"])
    if doc.get("variables") == {}:
        del doc["variables"]
    elif "variables" in doc:
        doc["variables"] = expressions.normalized(doc["variables"])
    # limits belong to a variable and go when it does
    if "variable_bounds" in doc:
        defined = doc.get("variables") or {}
        doc["variable_bounds"] = {
            name: limits
            for name, limits in doc["variable_bounds"].items()
            if name in defined
        }
        if not doc["variable_bounds"]:
            del doc["variable_bounds"]
    if doc.get("events"):
        doc["events"] = expressions.normalized(doc["events"])
    # A fixed key order as well, so "the same document" is the same text
    # however it was assembled — read back from a script, built up through
    # the API, or written by hand.
    doc.setdefault("objects", [])  # a slot for the projection _build writes
    ordered = {key: doc[key] for key in _DOC_KEYS if key in doc}
    ordered.update({k: v for k, v in doc.items() if k not in _DOC_KEYS})
    doc.clear()
    doc.update(ordered)
    return doc


def _next_event_id(events):
    used = {e.get("id") for e in events}
    n = len(events) + 1
    while f"e{n}" in used:
        n += 1
    return f"e{n}"


def _migrate_events(doc):
    """Fold per-object `transforms`/`rotations` into the document's single
    ordered event log, in the order the old per-object build replayed them:
    depth-first, a Collection's children before the Collection itself, so a
    group transform still lands after everything it moves.

    The log is what makes an event editable — an op buried in one object's
    list has no position relative to the rest of the scene, so there is no
    "and then re-apply the later ones" to speak of.
    """
    events = list(doc.get("events") or [])
    described = {e["target"] for e in events if e.get("op") == "create"}
    creates, transforms = [], []

    def walk(specs, parent):
        for spec in specs:
            if spec["id"] not in described:
                creates.append({
                    "id": None, "op": "create", "target": spec["id"],
                    "type": spec["type"],
                    **({"params": spec["params"]} if spec.get("params") else {}),
                    **({"style": spec["style"]} if spec.get("style") else {}),
                    **({"parent": parent} if parent else {}),
                    **({"visible": False} if spec.get("visible") is False else {}),
                })
            # A parent has to exist before its children can join it, so creates
            # go depth-first from the root; the transforms keep the order the
            # per-object build replayed them in, children before parents.
            walk(spec.get("children") or [], spec["id"])
            for op in _spec_ops(spec):
                transforms.append({"id": None, "target": spec["id"], **op})
            spec.pop("transforms", None)
            spec.pop("rotations", None)

    walk(doc.get("objects") or [], None)
    events = creates + transforms + events
    used, n, numbered = {e.get("id") for e in events}, 0, []
    for event in events:
        if event.get("id") is not None:
            numbered.append(event)
            continue
        while f"e{(n := n + 1)}" in used:
            pass
        used.add(f"e{n}")
        # rebuilt rather than assigned into, so the id reads first wherever
        # the event came from — a document should not depend on that
        numbered.append({"id": f"e{n}",
                         **{k: v for k, v in event.items() if k != "id"}})
    doc["events"] = numbered
    return doc


def _id_list(ids, limit=5):
    head = ", ".join(ids[:limit])
    return head if len(ids) <= limit else f"{head} (+{len(ids) - limit} more)"


def _round_trip_warnings(before, after):
    """What re-importing a script changed beyond the edit the user made.

    Only the deterministic losses are reported: a diff of ids or parameters
    would just be describing the user's own edit back at them. A script states
    each object's final pose, so recorded transform sequences come back as the
    single equivalent transform, and a group transform comes back distributed
    over the children it moved.
    """
    old = {s["id"]: s for s in _walk_specs(before["objects"])}
    new = {s["id"]: s for s in _walk_specs(after["objects"])}

    def counts(doc):
        tally = {}
        for event in doc.get("events") or []:
            tally[event["target"]] = tally.get(event["target"], 0) + 1
        return tally

    was, now = counts(before), counts(after)
    collapsed, ungrouped = [], []
    for oid, spec in old.items():
        if oid not in new or was.get(oid, 0) <= now.get(oid, 0):
            continue
        bucket = ungrouped if spec.get("type") == "Collection" else collapsed
        bucket.append(oid)
    warnings = []
    if collapsed:
        warnings.append("transform steps collapsed into one equivalent "
                        f"transform: {_id_list(collapsed)}")
    if ungrouped:
        warnings.append("group transforms are now baked into the children they "
                        f"moved: {_id_list(ungrouped)}")
    return warnings


def _replay(obj, ops):
    """Replay recorded magpylib transform calls on a live object.

    Transforms are stored as the magpylib calls themselves rather than as a
    derived pose — magpylib owns the semantics (paths, anchors, `start`, and
    Collections transforming their children), we only record and replay.
    """
    for op in ops:
        kind = op.get("op", "rotate_from_angax")
        kwargs = {"start": op["start"]} if "start" in op else {}
        if kind == "move":
            obj.move(op["displacement"], **kwargs)
        elif kind == "rotate_from_angax":
            obj.rotate_from_angax(
                op["angle"], op["axis"], anchor=op.get("anchor"), **kwargs
            )
        elif kind == "rotate_from_rotvec":
            obj.rotate_from_rotvec(
                op["rotvec"], degrees=True, anchor=op.get("anchor"), **kwargs
            )
        elif kind == "position":
            obj.position = op["value"]
        elif kind == "orientation":
            obj.orientation = R.from_rotvec(op["rotvec"], degrees=True)
        else:
            raise ValueError(f"unknown transform op {kind!r}")


def _lit(value):
    """Document value -> Python source. Expressions lose their `=` and go in
    unquoted, so the generated script is parametric in the same variables the
    document is; everything else is a literal."""
    if expressions.is_expression(value):
        return expressions.source_of(value)
    if isinstance(value, list):
        inner = ", ".join(_lit(v) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    return repr(value)


def _op_source(op):
    """One recorded transform op -> the magpylib call that produced it."""
    kind = op.get("op", "rotate_from_angax")
    if kind == "position":
        return f"position = {_lit(op['value'])}"
    if kind == "orientation":
        return f"orientation = R.from_rotvec({_lit(op['rotvec'])}, degrees=True)"
    if kind == "move":
        args = _lit(op["displacement"])
    elif kind == "rotate_from_angax":
        args = f"{_lit(op['angle'])}, {_lit(op['axis'])}"
    else:  # rotate_from_rotvec
        args = f"{_lit(op['rotvec'])}, degrees=True"
    anchor = op.get("anchor")
    if anchor is not None:
        args += f", anchor={_lit(anchor)}"
    if "start" in op:
        args += f", start={_lit(op['start'])}"
    return f"{kind}({args})"


def _event_source(event):
    """One event as the line it stands for, for a history list."""
    op = event.get("op", "rotate_from_angax")
    target = event["target"]
    if op == "create":
        args = [f"{k}={_lit(v)}" for k, v in (event.get("params") or {}).items()]
        if event.get("parent"):
            args.append(f"parent={event['parent']!r}")
        ctor = "Collection" if event["type"] == "Collection" else event["type"]
        return f"{target} = magpy.{ctor}({', '.join(args)})"
    if op == "remove":
        return f"remove {target}"
    if op == "reparent":
        return f"{target} joins {event.get('parent') or 'the scene root'}"
    if op == "duplicate_around":
        return (f"{target} × {_lit(event.get('count', 1))} about "
                f"{_lit(event.get('axis', 'z'))}")
    return f"{target}.{_op_source(event)}"


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
        # start empty; older documents carry their ops per object, not as a log
        self.doc = _canonical(
            _migrate_events(scene if scene is not None else {"objects": []})
        )
        self._objs: dict[str, object] = {}
        self._vars: dict[str, float] = {}  # resolved at each build
        self._derived: dict[str, list[str]] = {}  # source id -> generated copies
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
        """Resolve the variables, construct every object, then fold the event
        log over them in order.

        Objects first is safe because a Collection's constructor does not move
        the children handed to it — only its position/orientation *setters*
        do, and those are events like any other, so they keep their place in
        the log relative to the children they carry.
        """
        self._vars = expressions.resolve_variables(self.doc.get("variables") or {})
        # Hard bounds are checked here rather than where a value is typed, so
        # they hold however the variable arrived at its value — including
        # through another variable's expression.
        for name, limits in (self.doc.get("variable_bounds") or {}).items():
            value = self._vars.get(name)
            if value is None:
                continue
            if limits.get("min") is not None and value < limits["min"]:
                raise ValueError(
                    f"{name} = {value:g} is below its minimum {limits['min']:g}"
                )
            if limits.get("max") is not None and value > limits["max"]:
                raise ValueError(
                    f"{name} = {value:g} is above its maximum {limits['max']:g}"
                )
        self._objs = {}
        self._derived = {}
        self._specs = {}  # id -> the spec its create event describes
        self._parents = {}  # id -> parent id or None
        self.scene = magpy.Collection()
        for event in self.doc.get("events") or []:
            op = event.get("op", "rotate_from_angax")
            if op == "create":
                self._create(event)
                continue
            if op == "remove":
                self._remove(event["target"])
                continue
            target = self._objs.get(event["target"])
            if target is None:
                raise ValueError(
                    f"event {event.get('id')} targets unknown object "
                    f"{event['target']!r}"
                )
            if op == "reparent":
                self._reparent(event["target"], event.get("parent"))
                continue
            resolved = self._resolve(event)
            if op == "duplicate_around":
                self._duplicate_around(event["target"], resolved)
            else:
                _replay(target, [resolved])
        # The object tree is a projection of the log, rebuilt here rather than
        # stored: two representations of the same structure would drift.
        self.doc["objects"] = self._project()

    def _create(self, event):
        """A create event -> a live magpylib object in its place in the tree."""
        object_id = event["target"]
        if object_id in self._objs:
            raise ValueError(f"duplicate object id {object_id!r}")
        spec = {
            "id": object_id,
            "type": event["type"],
            **({"params": event["params"]} if event.get("params") else {}),
            **({"style": event["style"]} if event.get("style") else {}),
            **({"visible": False} if event.get("visible") is False else {}),
        }
        params = self._resolve(dict(event.get("params") or {}))
        if event["type"] == "Collection":
            # Positional children, the form a script uses: they exist already.
            adopted = [self._objs[c] for c in event.get("children") or []]
            obj = magpy.Collection(*adopted, **params)
            for child in event.get("children") or []:
                self._parents[child] = object_id
        else:
            obj = _resolve_type(event["type"])(**params)
        for path, value in (event.get("style") or {}).items():
            style_compat.set_style(obj, path, value)  # same call the GUI/LLM makes
        self._objs[object_id] = obj
        self._specs[object_id] = spec
        parent = event.get("parent")
        self._parents[object_id] = parent
        (self._objs[parent] if parent else self.scene).add(obj)

    def _remove(self, object_id):
        """A remove event: the object and everything under it stop existing
        from here on. Events recorded before it still happened."""
        if object_id not in self._objs:
            raise ValueError(f"cannot remove unknown object {object_id!r}")
        gone = [object_id, *self._descendants(object_id)]
        parent = self._parents.get(object_id)
        (self._objs[parent] if parent else self.scene).remove(
            self._objs[object_id], recursive=False
        )
        for dead in gone:
            self._objs.pop(dead, None)
            self._specs.pop(dead, None)
            self._parents.pop(dead, None)
            self._derived.pop(dead, None)

    def _reparent(self, object_id, parent):
        """A reparent event: from here on the object belongs to another group,
        so later group transforms carry it and earlier ones do not."""
        if parent is not None and parent not in self._objs:
            raise ValueError(f"cannot reparent into unknown object {parent!r}")
        if parent in [object_id, *self._descendants(object_id)]:
            raise ValueError(f"cannot move {object_id!r} into its own subtree")
        old = self._parents.get(object_id)
        (self._objs[old] if old else self.scene).remove(
            self._objs[object_id], recursive=False
        )
        (self._objs[parent] if parent else self.scene).add(self._objs[object_id])
        self._parents[object_id] = parent

    def _descendants(self, object_id):
        below = [c for c, p in self._parents.items() if p == object_id]
        return [*below, *[d for c in below for d in self._descendants(c)]]

    def _project(self):
        """The object tree the log describes, in creation order.

        Read straight from the create events rather than from anything cached
        at build time, so an edit to one shows up without a rebuild — which is
        what makes `_spec()` and everything reading it still true.
        """
        creates = {
            e["target"]: e
            for e in self.doc.get("events") or []
            if e.get("op") == "create"
        }

        def spec_of(object_id):
            event = creates[object_id]
            spec = {"id": object_id, "type": event["type"]}
            for key in ("params", "style", "hidden_style"):
                if event.get(key):
                    spec[key] = event[key]
            if event.get("visible") is False:
                spec["visible"] = False
            if event["type"] == "Collection":
                spec["children"] = [
                    spec_of(child)
                    for child, parent in self._parents.items()
                    if parent == object_id
                ]
            return spec

        # _parents holds exactly the objects still alive, in creation order
        return [spec_of(oid) for oid, parent in self._parents.items() if parent is None]

    def _duplicate_around(self, object_id, event):
        """Replay a duplicate event: `count` copies evenly spaced about an
        axis, optionally spun in place as they go (which is all a Halbach ring
        is). The copies are generated, not declared — they exist only as long
        as the event does, which is what makes the count a single number to
        edit instead of twenty objects to keep in step."""
        count = int(event.get("count", 1))
        if count < 1:
            raise ValueError(f"duplicate count must be at least 1, got {count}")
        axis = event.get("axis", "z")
        anchor = event.get("anchor", 0)
        spin = float(event.get("spin", 0))
        source = self._objs[object_id]
        container = self._objs.get(self._parent_id(object_id)) or self.scene
        made = []
        for i in range(1, count):
            copy = source.copy()
            copy.rotate_from_angax(i * 360 / count, axis, anchor=anchor)
            if spin:
                copy.rotate_from_angax(i * spin, axis, anchor=None)
            copy_id = f"{object_id}#{i}"
            self._objs[copy_id] = copy
            container.add(copy)
            made.append(copy_id)
        self._derived[object_id] = made

    def _parent_id(self, object_id):
        for spec, parent in self._iter_specs():
            if spec["id"] == object_id:
                return parent["id"] if parent else None
        return None

    def _resolve(self, value):
        """Document value -> plain numbers, substituting the variables."""
        def lookup(name):
            if name not in self._vars:
                raise ValueError(f"unknown variable {name!r}")
            return self._vars[name]

        return expressions.resolve(value, lookup)

    def _build_spec(self, spec):
        """Build one spec (recursing into Collection children) into a live object."""
        params = self._resolve(dict(spec.get("params", {})))
        if spec["type"] == "Collection":
            children = [self._build_spec(c) for c in spec.get("children", [])]
            obj = magpy.Collection(*children, **params)
        else:
            cls = _resolve_type(spec["type"])
            obj = cls(**params)
        for path, value in spec.get("style", {}).items():
            style_compat.set_style(obj, path, value)  # same call the GUI/LLM makes
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
            _canonical(self.doc)
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
        objects = []
        for spec, parent in self._iter_specs():
            objects.append({
                "id": spec["id"],
                "type": spec["type"],
                "label": self._objs[spec["id"]].style.label or spec["type"],
                "parent": parent["id"] if parent else None,
                "visible": spec.get("visible", True),
            })
            # copies made by a duplicate event: real objects in the field and
            # the 3D view, but generated, so they have no spec to edit
            for copy_id in self._derived.get(spec["id"], []):
                objects.append({
                    "id": copy_id,
                    "type": spec["type"],
                    "label": self._objs[copy_id].style.label or spec["type"],
                    "parent": parent["id"] if parent else None,
                    "visible": spec.get("visible", True),
                    "derived": spec["id"],
                })
        return objects

    def get_schema(self, object_id):
        return style_compat.schema(self._objs[object_id])

    def get_params(self, object_id):
        """The object's physics parameters (polarization, dimension, current,
        …) with their current values and shape, for inspector widgets.
        Position/orientation are excluded: those are transform-managed."""
        obj = self._objs[object_id]
        try:
            written = self._spec(object_id).get("params", {})
        except KeyError:
            written = {}  # a generated copy has no spec to have written it
        out = []
        for name in _PARAM_ATTRS:
            value = getattr(obj, name, None)
            if value is None:
                continue
            plain = _plain(value)
            if isinstance(plain, list):
                kind = "matrix" if plain and isinstance(plain[0], list) else "vector"
            else:
                kind = "scalar"
            entry = {
                "name": name,
                "value": plain,
                "kind": kind,
                "doc": _PARAM_DOCS.get(name, ""),
            }
            # `value` is what magpylib holds; when the document says it in
            # terms of a variable, the editor needs the expression as well —
            # otherwise editing the field would silently replace it.
            if expressions.contains_expression(written.get(name)):
                entry["written"] = written[name]
            out.append(entry)
        return out

    def get_transform(self, object_id):
        """World pose of an object, for the inspector's transform widgets."""
        obj = self._objs[object_id]
        position = np.atleast_2d(np.array(obj.position, dtype=float))
        rotvec = np.atleast_2d(obj.orientation.as_rotvec(degrees=True))
        euler = np.atleast_2d(obj.orientation.as_euler("xyz", degrees=True))
        out = {
            "position": position[-1].round(9).tolist(),
            "orientation": rotvec[-1].round(9).tolist(),
            "euler": euler[-1].round(9).tolist(),
            "path_length": len(position),
            "path": position.round(9).tolist() if len(position) > 1 else None,
        }
        # If the pose was written in terms of a variable, say so: an editor
        # showing only the resolved number would replace the expression the
        # moment the user touched a neighbouring axis.
        for op, key in (("position", "value"), ("orientation", "rotvec")):
            written = self._last_written(object_id, op, key)
            if written is not None:
                out[f"written_{op}"] = written
        return out

    def _last_written(self, object_id, op, key):
        """The last pose event of this kind on this object, if it holds an
        expression — the form to edit, as opposed to what it came out to."""
        for event in reversed(self.doc.get("events") or []):
            if event["target"] == object_id and event.get("op") == op:
                value = event.get(key)
                return value if expressions.contains_expression(value) else None
        if op == "position":
            # no event pinned it, so the constructor param is what wrote it
            try:
                value = self._spec(object_id).get("params", {}).get("position")
            except KeyError:
                return None
            return value if expressions.contains_expression(value) else None
        return None

    def get_values(self, object_id):
        obj = self._objs[object_id]
        return {
            "set": style_compat.set_values(obj),  # explicitly set (dotted keys)
            "resolved": style_compat.resolved_values(obj),  # effective values
        }

    def get_figure(self, animation=False, template=None):
        """Figure JSON; animation=True animates paths (plotly frames + play
        button). magpylib falls back to a static plot if nothing has a path.
        template is a plotly template name ('plotly_dark', 'plotly_white', …) —
        resolved here because plotly.js has no named-template registry.
        The whole scene is always drawn: objects hidden via set_visible carry
        magpylib's own hide switches, keeping every colour assignment stable."""
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

    def _scene_extent(self):
        """A square in-plane extent covering the sources, with margin."""
        points = [
            np.atleast_2d(np.array(obj.position, dtype=float))
            for obj in self._objs.values()
        ]
        if not points:
            return 1.0, np.zeros(3)
        stacked = np.vstack(points)
        centre = (stacked.max(axis=0) + stacked.min(axis=0)) / 2
        span = float(np.max(stacked.max(axis=0) - stacked.min(axis=0)))
        return max(span, 1.0) * 1.2, centre

    def set_pixel_grid(self, object_id, plane="xy", size=2.0, resolution=20,
                       offset=0.0):
        """Give a Sensor a regular grid of pixels — magpylib's own way to map a
        field. The grid is in the sensor's LOCAL frame, so moving or rotating
        the sensor carries the measurement plane with it (any orientation, not
        just the axis planes), and it is drawn in the 3D view."""
        obj = self._objs[object_id]
        if not isinstance(obj, magpy.Sensor):
            return {"ok": False, "error": f"{object_id!r} is not a Sensor"}
        axes = {"xy": (0, 1, 2), "xz": (0, 2, 1), "yz": (1, 2, 0)}
        if plane not in axes:
            return {"ok": False, "error": f"plane must be one of {sorted(axes)}"}
        iu, iv, inormal = axes[plane]
        n = max(2, int(resolution))
        span = np.linspace(-size / 2, size / 2, n)
        grid_u, grid_v = np.meshgrid(span, span)
        pixel = np.zeros((n, n, 3))
        pixel[:, :, iu] = grid_u
        pixel[:, :, iv] = grid_v
        pixel[:, :, inormal] = offset
        return self.set_param(object_id, "pixel", pixel.round(9).tolist())

    def get_field_map(self, plane="xy", offset=0.0, extent=None, resolution=40,
                      field="B", component="magnitude", log=False,
                      sensor_id=None, template=None):
        """Field on a plane as a plotly heatmap — the 2D map complementing the
        sensor-path plot. `plane` is 'xy' | 'xz' | 'yz' (offset is along the
        remaining axis), `component` is 'magnitude' | 'x' | 'y' | 'z'.
        `extent` is [umin, umax, vmin, vmax]; omitted it covers the scene.
        `log` plots log10 of the magnitude — near a magnet the field spans
        orders of magnitude and a linear scale flattens everything else.
        With `sensor_id`, the map is read off that Sensor's pixel grid instead
        (see set_pixel_grid) — the plane then follows the sensor's own pose."""
        if sensor_id is not None:
            return self._sensor_field_map(
                sensor_id, field=field, component=component, log=log,
                template=template,
            )
        axes = {"xy": (0, 1, 2), "xz": (0, 2, 1), "yz": (1, 2, 0)}
        if plane not in axes:
            raise ValueError(f"plane must be one of {sorted(axes)}, got {plane!r}")
        if component not in ("magnitude", "x", "y", "z"):
            raise ValueError(f"unknown component {component!r}")
        iu, iv, inormal = axes[plane]

        if extent is None:
            size, centre = self._scene_extent()
            extent = [
                centre[iu] - size, centre[iu] + size,
                centre[iv] - size, centre[iv] + size,
            ]
        u = np.linspace(extent[0], extent[1], int(resolution))
        v = np.linspace(extent[2], extent[3], int(resolution))
        grid_u, grid_v = np.meshgrid(u, v)
        points = np.zeros((grid_u.size, 3))
        points[:, iu] = grid_u.ravel()
        points[:, iv] = grid_v.ravel()
        points[:, inormal] = offset

        data = self.get_field(points=points.tolist(), field=field)
        values = np.array(data["values"]).reshape(len(v), len(u), 3)
        return self._heatmap(
            u, v, values, data["unit"], field, component, log, template,
            labels=(f"{plane[0]} (m)", f"{plane[1]} (m)"),
            subtitle=f"on {plane} at {'xyz'[inormal]} = {offset:g} m",
        )

    def _sensor_field_map(self, sensor_id, field="B", component="magnitude",
                          log=False, template=None):
        """Field over a Sensor's pixel grid — magpylib computes it directly on
        the sensor, so the plane follows the sensor's position/orientation."""
        sensor = self._objs[sensor_id]
        if not isinstance(sensor, magpy.Sensor):
            # ValueError like the rest of the surface: RPC reports the type name
            raise ValueError(f"{sensor_id!r} is not a Sensor")  # noqa: TRY004
        pixel = np.array(sensor.pixel, dtype=float) if sensor.pixel is not None else None
        if pixel is None or pixel.ndim != 3:
            raise ValueError(
                f"sensor {sensor_id!r} has no pixel grid — use set_pixel_grid first"
            )
        sources = self._leaf_sources()
        if not sources:
            raise ValueError("scene has no field sources")
        func = magpy.getB if field == "B" else magpy.getH
        values = np.array(func(sources, sensor, sumup=True), dtype=float)
        path_note = ""
        if values.ndim == 4:  # the sensor also has a path: map its last step
            path_note = f", path step {len(values) - 1}"
            values = values[-1]
        # local grid coordinates: the two axes the pixels actually vary along
        spread = np.ptp(pixel.reshape(-1, 3), axis=0)
        iu, iv = np.argsort(spread)[::-1][:2]
        iu, iv = sorted((int(iu), int(iv)))
        u = pixel[0, :, iu]
        v = pixel[:, 0, iv]
        return self._heatmap(
            u, v, values, "T" if field == "B" else "A/m", field, component, log,
            template,
            labels=(f"sensor {'xyz'[iu]} (m)", f"sensor {'xyz'[iv]} (m)"),
            subtitle=f"over {sensor.style.label or sensor_id} "
                     f"({pixel.shape[0]}×{pixel.shape[1]} pixels{path_note})",
        )

    def _heatmap(self, u, v, values, unit, field, component, log, template,
                 labels, subtitle):
        """Shared heatmap builder for both field-map sources."""
        if component == "magnitude":
            z = np.linalg.norm(values, axis=-1)
            # sequential: one hue light -> dark, lightest reads as "near zero"
            colorscale = [
                [0.0, "#cde2fb"], [0.25, "#86b6ef"], [0.5, "#3987e5"],
                [0.75, "#1c5cab"], [1.0, "#0d366b"],
            ]
            zmid = None
            title = f"|{field}| ({unit})"
            if log:
                z = np.log10(np.maximum(z, np.finfo(float).tiny))
                title = f"log₁₀ |{field}| ({unit})"
        else:
            z = values[:, :, "xyz".index(component)]
            # diverging: two poles with a neutral midpoint anchored at zero
            colorscale = [
                [0.0, "#0d366b"], [0.25, "#3987e5"], [0.5, "#f0efec"],
                [0.75, "#d03b3b"], [1.0, "#6b1111"],
            ]
            zmid = 0.0
            title = f"{field}{component} ({unit})"

        heatmap = {
            "type": "heatmap",
            "x": np.asarray(u).tolist(),
            "y": np.asarray(v).tolist(),
            "z": z.tolist(),
            "colorscale": colorscale,
            "colorbar": {"title": {"text": title}},
            "hovertemplate": (
                f"{labels[0].split(' ')[-2] if ' ' in labels[0] else 'x'}"
                "=%{x:.3g}<br>y=%{y:.3g}<br>"
                f"{title.split(' ')[0]}=%{{z:.4g}} {unit}<extra></extra>"
            ),
        }
        if zmid is not None:
            heatmap["zmid"] = zmid
        fig = go.Figure(data=[heatmap])
        fig.update_layout(
            xaxis_title=labels[0],
            yaxis_title=labels[1],
            yaxis={"scaleanchor": "x", "scaleratio": 1},  # undistorted geometry
            title={"text": f"{title} {subtitle}"},
        )
        if template:
            fig.layout.template = template
        return json.loads(fig.to_json())

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
            style_compat.set_style(obj, path, value)
        except Exception as e:  # noqa: BLE001 - report validation errors, don't crash
            return {"ok": False, "error": str(e)}
        self._create_event(object_id)["style"] = style_compat.set_values(obj)
        self.doc["objects"] = self._project()  # keep the projection in step
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
            self._append({
                "op": "create", "target": object_id, "type": type,
                **({"params": params} if params else {}),
                **({"style": style} if style else {}),
                **({"parent": parent} if parent else {}),
            })
            if rotations:
                # Recorded after the create, i.e. after whatever has already
                # happened to the parent — the same thing the equivalent
                # script would do.
                self._log(object_id, _spec_ops({"rotations": rotations}))

        return self._mutate_doc(mutate, f"add {object_id}")

    def remove_object(self, object_id):
        """Remove an object; removing a Collection removes its whole subtree.

        Recorded rather than erased: the events that ran while the object
        existed still happened, and rewriting them would make the log a
        different story from the one the scene actually went through.
        """
        self._spec(object_id)  # raise early on unknown id

        def mutate(doc):
            self._append({"op": "remove", "target": object_id})

        return self._mutate_doc(mutate, f"remove {object_id}")

    def _set_world_pose(self, object_id, world_pos, world_rot):
        """Pin an object (and, for a Collection, its subtree) to a WORLD pose.

        magpylib positions are world coordinates, and the log ends here, so
        the assignment needs no parent-frame correction: nothing runs after it
        to move the object again. Before the log existed this had to measure
        the frame its ancestors would re-apply, by building a probe scene.
        """
        self._log(object_id, [
            {"op": "position", "value": np.round(world_pos, 9).tolist()},
            {"op": "orientation",
             "rotvec": np.round(world_rot.as_rotvec(degrees=True), 9).tolist()},
        ])

    # --- editing the log ---------------------------------------------------
    def _append(self, event):
        """Add one event to the end of the log, under a fresh id."""
        events = self.doc.setdefault("events", [])
        events.append({
            "id": _next_event_id(events),
            **{k: v for k, v in event.items() if k != "id"},
        })
        return events[-1]

    def _create_event(self, object_id):
        """The event that brought an object into being.

        What an object *is* — its type, parameters and style — is not a
        sequence of things that happened to it, so editing those edits this
        event in place rather than appending another. Same reason a CAD
        history lets you change the box you made instead of recording that you
        changed it. Only what happened *to* it afterwards is appended.
        """
        for event in self.doc.get("events") or []:
            if event.get("op") == "create" and event["target"] == object_id:
                return event
        raise KeyError(f"unknown object id {object_id!r}")

    # --- transforms --------------------------------------------------------
    def _log(self, object_id, ops):
        """Append transform ops to the end of the event log."""
        events = self.doc.setdefault("events", [])
        for op in expressions.normalized(_plain(ops)):
            events.append({"id": _next_event_id(events),
                           "target": object_id, **op})

    def _append_ops(self, object_id, ops, label):
        """Record magpylib transform calls in the event log and rebuild."""
        self._spec(object_id)  # raise early on unknown id

        def mutate(doc):
            self._log(object_id, ops)

        return self._mutate_doc(mutate, label)

    def move(self, object_id, displacement, start="auto"):
        """Move by `displacement` (relative), magpylib semantics: a list of
        displacements creates/extends a path, and a Collection carries its
        children along."""
        op = {"op": "move", "displacement": displacement}
        if start != "auto":
            op["start"] = start
        return self._append_ops(object_id, [op], f"move {object_id}")

    def rotate(self, object_id, angle, axis="z", anchor=None, start="auto"):
        """Rotate by `angle` degrees about `axis` (relative). `anchor` orbits
        that point (0 = origin); a list of angles creates/extends a path; on a
        Collection the whole group rotates."""
        op = {"op": "rotate_from_angax", "angle": angle, "axis": axis}
        if anchor is not None:
            op["anchor"] = anchor
        if start != "auto":
            op["start"] = start
        return self._append_ops(object_id, [op], f"rotate {object_id}")

    def set_transform(self, object_id, position=None, orientation=None):
        """Set the absolute pose in WORLD coordinates: `position` [x,y,z] and/
        or `orientation` as a rotation vector in degrees. Recorded at the end
        of the event log, so the pose is world-absolute even inside a rotated
        Collection — nothing replays after it."""
        if position is None and orientation is None:
            return {"ok": False, "error": "nothing to set"}
        obj = self._objs[object_id]
        if expressions.contains_expression([position, orientation]):
            # Recorded as written, not as the pose it currently comes to:
            # resolving here would freeze the variable out of the scene.
            def mutate_symbolic(doc):
                ops = []
                if position is not None:
                    ops.append({"op": "position", "value": position})
                if orientation is not None:
                    ops.append({"op": "orientation", "rotvec": orientation})
                self._log(object_id, ops)

            return self._mutate_doc(mutate_symbolic, f"set transform {object_id}")
        target_pos = np.array(
            obj.position if position is None else position, dtype=float
        )
        target_rot = (
            obj.orientation
            if orientation is None
            else R.from_rotvec(orientation, degrees=True)
        )
        is_path = target_pos.ndim > 1 or len(np.atleast_2d(target_rot.as_rotvec())) > 1

        def mutate(doc):
            if is_path:
                ops = []
                if position is not None:
                    ops.append({"op": "position", "value": position})
                if orientation is not None:
                    ops.append({"op": "orientation", "rotvec": orientation})
                self._log(object_id, ops)
            else:
                self._set_world_pose(object_id, target_pos, target_rot)

        return self._mutate_doc(mutate, f"set transform {object_id}")

    def duplicate_around(self, object_id, count, axis="z", anchor=0, spin=0):
        """Record a duplicate event: `count` copies of an object spaced evenly
        about `axis` through `anchor`, each additionally spun by `spin` degrees
        times its index (a Halbach ring is spin = 360/count). `count` and
        `spin` may be expressions, so the arrangement stays parametric.

        The object must sit inside a Collection: that is where the copies go,
        and it is what lets the arrangement export as plain runnable magpylib.
        """
        self._spec(object_id)  # raise early on unknown id
        if self._parent_id(object_id) is None:
            return {"ok": False,
                    "error": f"{object_id!r} must be inside a Collection to "
                             f"duplicate it — the copies need a group to join"}
        return self._append_ops(
            object_id,
            [{"op": "duplicate_around", "count": count, "axis": axis,
              "anchor": anchor, "spin": spin}],
            f"duplicate {object_id}",
        )

    def clear_path(self, object_id, index=-1):
        """Reduce a path to a single step (default: its last)."""
        obj = self._objs[object_id]
        position = np.atleast_2d(np.array(obj.position, dtype=float))[index]
        rotvec = np.atleast_2d(obj.orientation.as_rotvec(degrees=True))[index]
        return self._append_ops(
            object_id,
            [
                {"op": "position", "value": position.round(9).tolist()},
                {"op": "orientation", "rotvec": rotvec.round(9).tolist()},
            ],
            f"clear path {object_id}",
        )

    def _unique_id(self, base):
        used = {s["id"] for s, _ in self._iter_specs()}
        stem = re.sub(r"_\d+$", "", base) or "obj"
        n = 1
        while f"{stem}_{n}" in used:
            n += 1
        return f"{stem}_{n}"

    def _next_label(self, label):
        """magpylib's copy convention: 'Cube' -> 'Cube_01' -> 'Cube_02'."""
        match = re.match(r"^(.*)_(\d+)$", label or "")
        stem, n = (match.group(1), int(match.group(2))) if match else (label or "obj", 0)
        used = {o["label"] for o in self.list_objects()}
        while True:
            n += 1
            candidate = f"{stem}_{n:02d}"
            if candidate not in used:
                return candidate

    def copy_object(self, object_id, parent=None):
        """Duplicate an object (a Collection copies its whole subtree). The
        copy's label gets magpylib's iteration suffix."""
        src = self._spec(object_id)
        if parent is not None and self._spec(parent)["type"] != "Collection":
            return {"ok": False, "error": f"parent {parent!r} is not a Collection"}
        new_id = self._unique_id(object_id)
        label = self._next_label(self._objs[object_id].style.label or src["type"])

        # source id -> copy's id, decided up front so the copied events can be
        # redirected onto the new objects as they are replayed
        renamed = {object_id: new_id}
        for spec in _walk_specs(src.get("children") or []):
            renamed[spec["id"]] = self._unique_id(spec["id"])

        def mutate(doc):
            source_events = list(doc.get("events") or [])
            for spec, spec_parent in self._iter_specs([src]):
                create = json.loads(json.dumps(self._create_event(spec["id"])))
                create["target"] = renamed[spec["id"]]
                if spec is src:
                    create.setdefault("style", {})["label"] = label
                    if parent is not None:
                        create["parent"] = parent
                    else:
                        create.pop("parent", None)
                else:
                    create["parent"] = renamed[spec_parent["id"]]
                self._append(create)
            # A copy is not a copy without its history: the source's other
            # events replay onto the new ids, in the order they first ran.
            for event in source_events:
                if event.get("op") != "create" and event["target"] in renamed:
                    self._append({**event,
                                  "target": renamed[event["target"]]})

        result = self._mutate_doc(mutate, f"copy {object_id}")
        if result["ok"]:
            result["id"] = new_id
        return result

    def set_visible(self, object_id, visible=True):
        """Show/hide an object in the 3D view. Implemented with magpylib's own
        style switches (`model3d.showdefault`, `path.show`) rather than by
        leaving the object out of the figure: the object still takes its slot
        in magpylib's colour sequence, so hiding one thing cannot recolour the
        others. Display only — hidden sources still contribute to the field.
        Hiding a Collection hides every leaf beneath it."""
        spec = self._spec(object_id)
        leaves = [
            s
            for s, _ in self._iter_specs([spec])
            if s["type"] != "Collection"
        ]

        def mutate(doc):
            create = self._create_event(object_id)
            if visible:
                create.pop("visible", None)
            else:
                create["visible"] = False
            for leaf in leaves:
                event = self._create_event(leaf["id"])
                style = event.setdefault("style", {})
                if visible:
                    restore = event.pop("hidden_style", {})
                    for path in _HIDE_STYLE:
                        if path in restore:
                            style[path] = restore[path]
                        else:
                            style.pop(path, None)
                else:
                    if "hidden_style" not in event:
                        event["hidden_style"] = {
                            p: style[p] for p in _HIDE_STYLE if p in style
                        }
                    style.update(_HIDE_STYLE)

        state = "show" if visible else "hide"
        return self._mutate_doc(mutate, f"{state} {object_id}")

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
            # Appended, not a rewrite of where the object was created: which
            # group transforms carried it depends on when it joined, and that
            # is exactly what the position in the log records.
            self._append({"op": "reparent", "target": object_id, "parent": parent})
            self._set_world_pose(object_id, world_pos, world_rot)

        return self._mutate_doc(mutate, f"reparent {object_id}")

    def set_param(self, object_id, name, value):
        """Set a constructor parameter (position, dimension, polarization, …).
        A value may be an expression over the document's variables, on its own
        or inside a vector: `[0, 0, "=gap"]`."""
        self._spec(object_id)  # raise early on unknown id

        def mutate(doc):
            # What an object *is* lives on its create event, so this edits
            # that rather than appending — see _create_event.
            create = self._create_event(object_id)
            create.setdefault("params", {})[name] = expressions.normalized(value)

        return self._mutate_doc(mutate, f"set {object_id}.{name}")

    def reset_style(self, object_id, path=None):
        """Reset one style path (or all styles) to defaults by dropping it
        from the doc and rebuilding — the property tree has no unset."""
        spec = self._spec(object_id)
        if path is not None and path not in spec.get("style", {}):
            return {"ok": False, "error": f"style path {path!r} is not set on {object_id!r}"}

        def mutate(doc):
            create = self._create_event(object_id)
            if path is None:
                create.pop("style", None)
            else:
                del create["style"][path]

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
        # A document says what it holds: since both keys are optional and an
        # empty scene is legal, something with neither is not an empty scene,
        # it is not a scene — and loading it as one would quietly wipe this.
        if not isinstance(scene, dict) or not {"objects", "events"} & set(scene):
            return {"ok": False,
                    "error": "not a scene document: expected 'objects' or 'events'"}

        def mutate(doc):
            self.doc = _canonical(_migrate_events(json.loads(json.dumps(scene))))

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

    def apply_script(self, path):
        """Replace the document with the scene an edited `to_script()` output
        describes, by EXECUTING it (same trust as load_script).

        Two ways in, and the result says which one ran ("mode"):

        - "parsed": the file is still in the shape to_script emits, so it is
          read as source. Variables, the order of a transform sequence and
          group transforms all survive, because nothing was executed and
          nothing had to be inferred from final poses.
        - "executed": anything else (a loop, a helper, numpy) is run, and the
          objects it leaves behind are introspected — the load_script route.
          That cannot see how the scene was written, so it reports in
          "warnings" what it had to flatten: transform sequences come back as
          the single equivalent transform, group transforms baked into the
          children they moved. Geometry survives; the writing of it does not.
        """
        from magpylib_studio import importer

        before = json.loads(json.dumps(self.doc))
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
        except OSError as e:
            return {"ok": False, "error": str(e)}

        doc, why_not = importer.parse_script(source)
        warnings = []
        if doc is None:
            try:
                namespace, _ = importer.run_script(path)
                doc, warnings = importer.document_from_namespace(namespace)
            except Exception as e:  # noqa: BLE001 - report errors, don't crash
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # Bounds are editor metadata: a script has nowhere to put them, so
        # they are carried across for the variables that survived the edit
        # rather than being silently dropped on every save.
        carried = {
            name: limits
            for name, limits in (before.get("variable_bounds") or {}).items()
            if name in (doc.get("variables") or {})
        }
        if carried:
            doc["variable_bounds"] = {**carried, **(doc.get("variable_bounds") or {})}

        result = self.load_scene(doc)
        if not result["ok"]:
            return result
        if not self._history_paused and self._undo:
            self._undo[-1]["label"] = "edit script"
        result["mode"] = "executed" if why_not else "parsed"
        if why_not:
            warnings = warnings + _round_trip_warnings(before, self.doc)
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

    # --- variables ---------------------------------------------------------
    def get_variables(self):
        """The document's variables, as written and as resolved."""
        variables = self.doc.get("variables") or {}
        bounds = self.doc.get("variable_bounds") or {}
        return {
            "variables": [
                {"name": name, "expression": value, "value": self._vars.get(name),
                 **({"bounds": bounds[name]} if name in bounds else {})}
                for name, value in variables.items()
            ]
        }

    def unknown_variables(self, values):
        """Names the given values refer to that this document does not define.

        A UI asks this before storing what someone typed: writing `a*2` into a
        field is a perfectly clear way to say "and let me set `a`", but the
        document cannot build until `a` exists, so it has to be asked for
        first rather than reported as an error afterwards.
        """
        defined = self.doc.get("variables") or {}
        return {
            "unknown": [
                name
                for name in expressions.referenced_names(values)
                if name not in defined
            ]
        }

    def set_variable_bounds(self, name, min=None, max=None,  # noqa: A002
                            soft_min=None, soft_max=None):
        """Limit a variable, so a UI can offer a slider and a typo cannot put
        the scene somewhere meaningless.

        Hard bounds (`min`/`max`) are enforced: a value outside them is
        rejected, including one a variable arrives at through an expression.
        Soft bounds (`soft_min`/`soft_max`) are only the range worth sweeping
        or dragging through — values outside stay legal, which is the point of
        the distinction. Passing nothing clears the limits.
        """
        if name not in (self.doc.get("variables") or {}):
            return {"ok": False, "error": f"unknown variable {name!r}"}
        limits = {"min": min, "max": max,
                  "soft_min": soft_min, "soft_max": soft_max}
        for key, value in limits.items():
            if value is not None and (
                not isinstance(value, int | float) or isinstance(value, bool)
            ):
                return {"ok": False, "error": f"{key} must be a number"}
        limits = {k: v for k, v in limits.items() if v is not None}
        for lo, hi in (("min", "max"), ("soft_min", "soft_max")):
            if lo in limits and hi in limits and limits[lo] > limits[hi]:
                return {"ok": False, "error": f"{lo} must not exceed {hi}"}
        if ("min" in limits and "soft_min" in limits
                and limits["soft_min"] < limits["min"]):
            return {"ok": False, "error": "soft_min is outside min"}
        if ("max" in limits and "soft_max" in limits
                and limits["soft_max"] > limits["max"]):
            return {"ok": False, "error": "soft_max is outside max"}

        def mutate(doc):
            bounds = doc.setdefault("variable_bounds", {})
            if limits:
                bounds[name] = limits
            else:
                bounds.pop(name, None)

        return self._mutate_doc(mutate, f"bound {name}")

    def set_variable(self, name, value):
        """Define or redefine a variable. `value` is a number, or an
        expression over the other variables ("=gap*2"). Everything that
        references it is rebuilt; a definition that cannot resolve (a typo, a
        cycle, a value some object rejects) is reported and rolled back."""
        if not isinstance(name, str) or not name.isidentifier():
            return {"ok": False, "error": f"{name!r} is not a valid variable name"}
        if name in expressions._CONSTANTS or name in expressions._FUNCTIONS:
            return {"ok": False, "error": f"{name!r} is a built-in expression name"}
        if not isinstance(value, int | float | str) or isinstance(value, bool):
            return {"ok": False, "error": "a variable is a number or an expression"}

        def mutate(doc):
            doc.setdefault("variables", {})[name] = expressions.normalized(value)

        return self._mutate_doc(mutate, f"set {name}")

    def remove_variable(self, name):
        """Drop a variable. Fails if anything still refers to it."""
        if name not in (self.doc.get("variables") or {}):
            return {"ok": False, "error": f"unknown variable {name!r}"}

        def mutate(doc):
            del doc["variables"][name]

        result = self._mutate_doc(mutate, f"remove {name}")
        if not result["ok"] and f"unknown variable {name!r}" in result["error"]:
            # the rollback's own message, which reads like the variable never
            # existed — say what actually stopped it
            result["error"] = (
                f"{name!r} is still used by the scene; change what refers to "
                f"it first"
            )
        return result

    def sweep(self, variable, values, sensor_id=None, points=None, field="B"):
        """Rebuild the scene once per value of a variable and read the field.

        This is what variables are *for*: a parameter study. It costs a full
        re-fold of the document per step, which is milliseconds — the scene is
        rebuilt from the log on every ordinary edit anyway. Nothing is
        recorded in the history: the document ends on the value it started on.
        """
        variables = self.doc.get("variables") or {}
        if variable not in variables:
            return {"ok": False, "error": f"unknown variable {variable!r}"}
        if not isinstance(values, list | tuple) or not values:
            return {"ok": False, "error": "values must be a non-empty list"}
        original = variables[variable]
        steps = []
        try:
            for value in values:
                self.doc["variables"][variable] = value
                self._build()
                data = self.get_field(sensor_id=sensor_id, points=points, field=field)
                steps.append({
                    "value": value,
                    "values": data["values"],
                    "magnitude": data["magnitude"],
                })
        except Exception as e:  # noqa: BLE001 - a bad value is a result, not a crash
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "variable": variable, "steps": steps}
        finally:
            self.doc["variables"][variable] = original
            self._build()
        return {"ok": True, "variable": variable, "field": field,
                "unit": "T" if field == "B" else "A/m", "steps": steps}

    def get_sweep_figure(self, variable, values, sensor_id=None, points=None,
                         field="B", component="magnitude", template=None):
        """A sweep as a plotly line plot: the field against the variable, one
        trace per observation point (a sensor path gives one per step)."""
        result = self.sweep(variable, values, sensor_id, points, field)
        if not result["ok"]:
            raise ValueError(result["error"])
        xs = [step["value"] for step in result["steps"]]
        per_step = [
            np.atleast_2d(np.array(step["values"], dtype=float).reshape(-1, 3))
            for step in result["steps"]
        ]
        n_points = min(len(a) for a in per_step)
        # one hue, light -> dark over the observation points: they are the same
        # quantity at different places, not unrelated series
        shades = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
        traces = []
        for i in range(n_points):
            column = np.array([a[i] for a in per_step])
            y = (np.linalg.norm(column, axis=-1) if component == "magnitude"
                 else column[:, "xyz".index(component)])
            shade = shades[i * len(shades) // n_points] if n_points > 1 else shades[3]
            traces.append(go.Scatter(
                x=xs, y=y, mode="lines+markers", line={"color": shade},
                marker={"size": 5},
                name=f"point {i}" if n_points > 1 else f"|{field}|",
                showlegend=n_points > 1,
            ))
        label = f"|{field}|" if component == "magnitude" else f"{field}{component}"
        fig = go.Figure(traces)
        fig.update_layout(
            title={"text": f"{label} against {variable}"},
            xaxis={"title": {"text": variable}},
            yaxis={"title": {"text": f"{label} ({result['unit']})"}},
            margin={"l": 60, "r": 20, "t": 50, "b": 50},
        )
        if template:
            fig.layout.template = template
        return json.loads(fig.to_json())

    # --- the event log -----------------------------------------------------
    def get_events(self):
        """The ordered transform log, as the scene's editable construction
        history: each entry is the magpylib call that produced it."""
        return {
            "events": [
                {"index": i, "id": e["id"], "target": e["target"],
                 "op": e.get("op", "rotate_from_angax"),
                 "source": _event_source(e)}
                for i, e in enumerate(self.doc.get("events") or [])
            ]
        }

    def _event_index(self, event_id):
        for i, event in enumerate(self.doc.get("events") or []):
            if event["id"] == event_id:
                return i
        raise KeyError(f"unknown event id {event_id!r}")

    def edit_event(self, event_id, changes):
        """Change a past event in place; everything recorded after it is
        re-applied on top, because the scene is rebuilt by folding the whole
        log. An edit that cannot replay (a bad axis, a target that no longer
        exists) is reported and rolled back, leaving the log as it was."""
        index = self._event_index(event_id)
        if not isinstance(changes, dict) or not changes:
            return {"ok": False, "error": "changes must be a non-empty object"}
        if "id" in changes:
            return {"ok": False, "error": "an event's id is not editable"}

        def mutate(doc):
            doc["events"][index] = {**doc["events"][index], **_plain(changes)}

        return self._mutate_doc(mutate, f"edit event {event_id}")

    def remove_event(self, event_id):
        """Drop one event and re-fold the log without it."""
        index = self._event_index(event_id)

        def mutate(doc):
            del doc["events"][index]

        return self._mutate_doc(mutate, f"remove event {event_id}")

    def move_event(self, event_id, index):
        """Reorder the log. Transforms do not commute, so this is a real edit:
        rotating then moving lands somewhere else than moving then rotating."""
        current = self._event_index(event_id)
        events = self.doc["events"]
        if not 0 <= index < len(events):
            return {"ok": False, "error": f"index must be 0..{len(events) - 1}"}

        def mutate(doc):
            doc["events"].insert(index, doc["events"].pop(current))

        return self._mutate_doc(mutate, f"move event {event_id}")

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

    def _duplicate_source(self, event):
        """A duplicate event as plain runnable magpylib: there is no library
        primitive for "N of these around an axis", so it exports as the loop
        it means. importer.parse_script reads exactly this shape back, which
        is what keeps the arrangement parametric across a round trip."""
        target = event["target"]
        count, spin = _lit(event.get("count", 1)), _lit(event.get("spin", 0))
        axis, anchor = _lit(event.get("axis", "z")), _lit(event.get("anchor", 0))
        body = [
            f"for i in range(1, {count}):",
            f"    _copy = {target}.copy()",
            f"    _copy.rotate_from_angax(i * 360 / ({count}), {axis}, "
            f"anchor={anchor})",
        ]
        if event.get("spin"):
            body.append(
                f"    _copy.rotate_from_angax(i * ({spin}), {axis}, anchor=None)"
            )
        # the copies go in the source's own group, which is why a duplicate
        # needs one: a bare list would have to be threaded into show()
        body.append(f"    {self._parent_id(target)}.add(_copy)")
        return body

    def to_script(self):
        # Definitions come from the object tree, so the structural events are
        # already expressed by it; what is left to write out is what happened
        # to the objects afterwards.
        events = [
            e for e in self.doc.get("events") or []
            if e.get("op") not in ("create", "remove", "reparent")
        ]
        needs_scipy = any(e.get("op") == "orientation" for e in events)
        lines = ["import magpylib as magpy"]
        if needs_scipy:
            lines.append("from scipy.spatial.transform import Rotation as R")
        lines.append("")
        variables = self.doc.get("variables") or {}
        if variables:
            # Real Python variables: the script stays parametric, and reading
            # it back recovers them (see importer.parse_script).
            lines += [f"{name} = {_lit(value)}" for name, value in variables.items()]
            lines.append("")

        def emit(spec):
            """Emit child definitions first, then this object; return its name."""
            name = spec["id"]
            parts = [emit(c) for c in spec.get("children") or []]
            parts += [
                f"{k}={_lit(v)}" for k, v in spec.get("params", {}).items()
            ]
            if spec.get("style"):
                parts.append(f"style={_nest(spec['style'])!r}")
            ctor = "Collection" if spec["type"] == "Collection" else spec["type"]
            lines.append(f"{name} = magpy.{ctor}({', '.join(parts)})")
            return name

        names = [emit(s) for s in self.doc["objects"]]
        # Definitions, then the event log in order — the script is the log's
        # own notation, which is why editing a line of it edits an event.
        if events:
            lines.append("")
            for event in events:
                if event.get("op") == "duplicate_around":
                    lines += self._duplicate_source(event)
                else:
                    lines.append(f"{event['target']}.{_op_source(event)}")
        # Shown as loose objects, not wrapped in a Collection: the script must
        # bind exactly the objects this document holds, so importing it back
        # reproduces the same scene. A wrapper would come back as one nested
        # group and take every id inside it with it.
        lines += ["", f"magpy.show({', '.join(names)}, backend='plotly')"
                  if names else "# empty scene"]
        return "\n".join(lines)
