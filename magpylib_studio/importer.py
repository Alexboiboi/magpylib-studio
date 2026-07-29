"""Import existing magpylib scripts by running them, not parsing them.

The script is executed in this process (same trust as the user running it),
`show()` patched to a no-op; the magpylib objects left in the namespace are
then introspected into a studio document: variable names become ids (nested
children included, whenever the script binds them to a name of their own),
Collections keep their nesting, orientation becomes a `rotations` entry.
The known cost: parametric structure flattens (a loop building 10 magnets
imports as 10 concrete objects).
"""

from __future__ import annotations

import ast
import keyword
import re

import magpylib as magpy
import numpy as np
from magpylib._src.display import display as _display_module

from magpylib_studio import expressions, style_compat

# Constructor kwargs worth introspecting, tried in order per object.
# magnetization is intentionally absent: it is derived from polarization.
_PARAM_ATTRS = (
    "polarization",
    "dimension",
    "diameter",
    "vertices",
    "faces",
    "current",
    "moment",
    "pixel",
)


def _dotted_type(obj):
    """Live object -> 'magnet.Cuboid' / 'Sensor' / ... or None if unsupported."""
    if isinstance(obj, magpy.Sensor):
        return "Sensor"
    name = type(obj).__name__
    for modname in ("magnet", "current", "misc"):
        if getattr(getattr(magpy, modname), name, None) is type(obj):
            return f"{modname}.{name}"
    return None


def _is_scene_object(obj):
    return isinstance(obj, magpy.Collection) or _dotted_type(obj) is not None


def _zeroed(array):
    """+0.0 turns IEEE negative zero back into plain zero. Without it a scene
    re-rendered as a script flip-flops between `0.0` and `-0.0` on every
    round trip — the tiny residue of a rotation, rounded, keeps its sign."""
    return array + 0.0 if array.dtype.kind == "f" else array


def _tolist(value):
    return _zeroed(value).tolist() if isinstance(value, np.ndarray) else value


def _unique_id(base, used):
    base = re.sub(r"\W|^(?=\d)", "_", str(base)) or "obj"
    if keyword.iskeyword(base):
        base += "_"
    candidate, n = base, 1
    while candidate in used:
        n += 1
        candidate = f"{base}_{n}"
    used.add(candidate)
    return candidate


def _spec_from(obj, object_id, used_ids, warnings, names):
    if isinstance(obj, magpy.Collection):
        spec = {"id": object_id, "type": "Collection", "children": [
            _spec_from(child,
                       _unique_id(names.get(id(child)) or child.style.label or "obj",
                                  used_ids),
                       used_ids, warnings, names)
            for child in obj.children
        ]}
    else:
        params = {}
        for attr in _PARAM_ATTRS:
            value = getattr(obj, attr, None)
            if value is not None:
                params[attr] = _tolist(value)
        position = np.array(obj.position)
        if np.any(position):
            params["position"] = position.tolist()
        spec = {"id": object_id, "type": _dotted_type(obj), "params": params}
        rotvec = np.atleast_2d(obj.orientation.as_rotvec(degrees=True))
        if len(rotvec) > 1:
            # orientation path: reproduced exactly, elementwise over the path
            if np.linalg.norm(rotvec) > 1e-9:
                spec["rotations"] = [
                    {"rotvec": _zeroed(rotvec.round(6)).tolist(), "start": 0}
                ]
        else:
            angle = float(np.linalg.norm(rotvec[0]))
            if angle > 1e-9:
                spec["rotations"] = [
                    {"angle": round(angle, 6),
                     "axis": _zeroed((rotvec[0] / angle).round(9)).tolist()}
                ]
    style = style_compat.set_values(obj)
    if style:
        spec["style"] = style
    return spec


def _document_from_named(named, names):
    """[(name, obj), ...] -> (document, warnings). `names` maps id(obj) -> the
    script's variable name, so nested children keep their script identity too
    (a Collection's children are not in `named`, only reachable through it)."""
    # Objects reachable inside a listed Collection are emitted there, not twice.
    contained = set()
    for _, obj in named:
        if isinstance(obj, magpy.Collection):
            for child in obj.children_all:
                contained.add(id(child))
    top = [(name, obj) for name, obj in named if id(obj) not in contained]
    # Same object under several names: keep the first name only.
    seen, unique_top = set(), []
    for name, obj in top:
        if id(obj) not in seen:
            seen.add(id(obj))
            unique_top.append((name, obj))
    if not unique_top:
        raise ValueError("script produced no magpylib objects")
    used_ids, warnings = set(), []
    objects = [
        _spec_from(obj, _unique_id(name, used_ids), used_ids, warnings, names)
        for name, obj in unique_top
    ]
    return {"objects": objects}, warnings


def _name_map(namespace):
    """id(obj) -> first variable name bound to it in the script."""
    mapping = {}
    for name, obj in namespace.items():
        if not name.startswith("_") and _is_scene_object(obj):
            mapping.setdefault(id(obj), name)
    return mapping


def document_from_namespace(namespace):
    """Every magpylib object the script left behind, as one document."""
    named = [
        (name, obj)
        for name, obj in namespace.items()
        if not name.startswith("_") and _is_scene_object(obj)
    ]
    return _document_from_named(named, _name_map(namespace))


def document_from_objects(objects, namespace):
    """The objects of one captured show() call, named from the namespace
    where possible (falling back to style labels / generated ids)."""
    names = _name_map(namespace)
    named = [
        (names.get(id(obj)) or obj.style.label or "obj", obj) for obj in objects
    ]
    return _document_from_named(named, names)


# --- reading a script back by parsing it ---------------------------------
#
# Executing a script tells you what it built; parsing tells you how it was
# written. Only the latter can recover a variable or the order of a transform
# sequence, because both are gone by the time the objects exist. So the shape
# `to_script` emits — assignments and calls, no control flow — is parsed
# instead, and anything outside that shape falls back to running it.

_METHOD_OPS = {
    "move": ("displacement",),
    "rotate_from_angax": ("angle", "axis"),
    "rotate_from_rotvec": ("rotvec",),
}


def _flatten_style(nested, prefix=""):
    """{'magnetization': {'mode': 'arrow'}} -> {'magnetization.mode': 'arrow'},
    the dotted form the document stores (inverse of session._nest)."""
    flat = {}
    for key, value in nested.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten_style(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _listify(value):
    if isinstance(value, tuple | list):
        return [_listify(v) for v in value]
    return value


def _parsed_value(node, variables):
    """A literal becomes itself; anything mentioning a variable becomes the
    document's `=expression` form, element-wise inside a tuple or list."""
    if isinstance(node, ast.Tuple | ast.List):
        return [_parsed_value(e, variables) for e in node.elts]
    if {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & variables:
        return expressions.PREFIX + ast.unparse(node)
    return _listify(ast.literal_eval(node))  # ValueError if not a literal


def _dotted_from_call(node):
    """magpy.magnet.Cuboid(...) -> 'magnet.Cuboid'; magpy.Sensor(...) -> 'Sensor'."""
    parts = []
    attr = node.func
    while isinstance(attr, ast.Attribute):
        parts.append(attr.attr)
        attr = attr.value
    if not isinstance(attr, ast.Name) or attr.id != "magpy":
        return None
    return ".".join(reversed(parts))


class _Unparseable(Exception):
    """The script is not in the shape to_script emits; run it instead."""


def _event_from_call(node, target, variables):
    method = node.func.attr
    if method not in _METHOD_OPS:
        raise _Unparseable(method)
    op = {"op": method, "target": target}
    names = _METHOD_OPS[method]
    if len(node.args) < len(names):
        raise _Unparseable(method)
    for name, arg in zip(names, node.args):
        op[name] = _parsed_value(arg, variables)
    for kw in node.keywords:
        if kw.arg == "degrees":  # emitted with rotate_from_rotvec, implied
            continue
        if kw.arg not in ("anchor", "start"):
            raise _Unparseable(kw.arg)
        op[kw.arg] = _parsed_value(kw.value, variables)
    return op


_PLANE_NORMALS = {(0, 0, 1): "xy", (0, 1, 0): "xz", (1, 0, 0): "yz"}


def _mirror_from_call(node, objects, variables):
    """`group.add(_mirror(obj, normal, anchor))` back into a mirror event, or
    None when the call is something else entirely."""
    if node.func.attr != "add" or len(node.args) != 1:
        return None
    inner = node.args[0]
    if not (isinstance(inner, ast.Call)
            and getattr(inner.func, "id", None) == "_mirror"):
        return None
    if not inner.args or getattr(inner.args[0], "id", None) not in objects:
        raise _Unparseable("_mirror")
    event = {"op": "mirror", "target": inner.args[0].id}
    normal = _parsed_value(inner.args[1], variables) if len(inner.args) > 1 else [0, 0, 1]
    named = _PLANE_NORMALS.get(tuple(normal)) if isinstance(normal, list) else None
    # a named plane reads better and is what the studio recorded
    event.update({"plane": named} if named else {"normal": normal})
    event["anchor"] = (
        _parsed_value(inner.args[2], variables) if len(inner.args) > 2 else 0
    )
    return event


def _duplicate_from_loop(node, objects, variables):
    """The one loop shape the studio emits — `for i in range(1, n): copy,
    rotate, add` — back into a duplicate event. Any other loop raises, and
    the script goes to the execute path where it flattens into real copies."""
    if (not isinstance(node.target, ast.Name) or node.target.id != "i"
            or node.orelse):
        raise _Unparseable("loop")
    call = node.iter
    if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "range"
            and len(call.args) == 2):
        raise _Unparseable("loop range")
    count = _parsed_value(call.args[1], variables)

    source_name, spin, parent, rotations, shift = None, 0, None, [], None
    for stmt in node.body:
        if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                and getattr(stmt.value.func, "attr", None) == "copy"):
            source_name = stmt.value.func.value.id
            continue
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            raise _Unparseable("loop body")
        inner = stmt.value
        method = getattr(inner.func, "attr", None)
        if method == "rotate_from_angax":
            rotations.append(inner)
        elif method == "move":
            shift = inner
        elif method == "add":
            parent = inner.func.value.id
        else:
            raise _Unparseable(method or "loop body")
    if source_name is None or parent is None or not (rotations or shift):
        raise _Unparseable("loop body")
    if source_name not in objects:
        raise _Unparseable(source_name)

    if shift is not None:  # a linear pattern: each copy `i * step` further on
        offsets = shift.args[0]
        if not isinstance(offsets, ast.Tuple | ast.List):
            raise _Unparseable("step")
        step = []
        for component in offsets.elts:
            if not isinstance(component, ast.BinOp):
                raise _Unparseable("step")
            step.append(_parsed_value(component.right, variables))
        return {"op": "duplicate_along", "target": source_name,
                "count": count, "step": step}

    # first rotation is the orbit (i * 360 / count about the anchor), an
    # optional second is the per-copy spin (i * spin, no anchor)
    orbit = rotations[0]
    axis = _parsed_value(orbit.args[1], variables)
    anchor = 0
    for kw in orbit.keywords:
        if kw.arg == "anchor":
            anchor = _parsed_value(kw.value, variables)
    if len(rotations) > 1:
        spin_arg = rotations[1].args[0]  # i * (<spin>)
        if not isinstance(spin_arg, ast.BinOp):
            raise _Unparseable("spin")
        spin = _parsed_value(spin_arg.right, variables)
    return {"op": "duplicate_around", "target": source_name, "count": count,
            "axis": axis, "anchor": anchor, "spin": spin}


def parse_script(source):
    """A script in the shape `to_script` emits -> (document, None), or
    (None, reason) when it is anything else and has to be executed."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return None, f"{type(e).__name__}: {e.msg}"

    variables, objects, events = {}, {}, []
    nested = set()  # object names that became a Collection's children

    def value(node):
        return _parsed_value(node, set(variables))

    try:
        for stmt in tree.body:
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                continue
            if isinstance(stmt, ast.For):
                events.append(_duplicate_from_loop(stmt, objects, set(variables)))
                continue
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "_mirror":
                continue  # the helper the studio emits, re-emitted on the way out
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Attribute):
                    if isinstance(call.func.value, ast.Name):
                        owner = call.func.value.id
                        if owner == "magpy":  # the trailing show()
                            continue
                        if owner not in objects:
                            raise _Unparseable(owner)
                        mirrored = _mirror_from_call(call, objects, set(variables))
                        events.append(
                            mirrored
                            or _event_from_call(call, owner, set(variables))
                        )
                        continue
                raise _Unparseable(ast.unparse(stmt))
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                raise _Unparseable(ast.unparse(stmt))
            target = stmt.targets[0]

            # obj.position = ... / obj.orientation = R.from_rotvec(...)
            if isinstance(target, ast.Attribute):
                if not isinstance(target.value, ast.Name):
                    raise _Unparseable(ast.unparse(stmt))
                owner = target.value.id
                if (owner not in objects
                        or target.attr not in ("position", "orientation")):
                    raise _Unparseable(ast.unparse(stmt))
                if target.attr == "position":
                    events.append({"op": "position", "target": owner,
                                   "value": value(stmt.value)})
                else:
                    call = stmt.value
                    if not (isinstance(call, ast.Call)
                            and getattr(call.func, "attr", None) == "from_rotvec"):
                        raise _Unparseable(ast.unparse(stmt))
                    events.append({"op": "orientation", "target": owner,
                                   "rotvec": value(call.args[0])})
                continue

            if not isinstance(target, ast.Name):
                raise _Unparseable(ast.unparse(stmt))
            name = target.id

            # name = magpy.Type(...) — an object; otherwise a variable
            if isinstance(stmt.value, ast.Call) and _dotted_from_call(stmt.value):
                dotted = _dotted_from_call(stmt.value)
                spec = {"id": name, "type": dotted, "params": {}, "style": {}}
                for arg in stmt.value.args:  # positional args are children
                    if not isinstance(arg, ast.Name) or arg.id not in objects:
                        raise _Unparseable(ast.unparse(stmt))
                    spec.setdefault("children", []).append(objects[arg.id])
                    nested.add(arg.id)
                for kw in stmt.value.keywords:
                    if kw.arg == "style":
                        spec["style"] = _flatten_style(ast.literal_eval(kw.value))
                    else:
                        spec["params"][kw.arg] = value(kw.value)
                if dotted == "Collection":
                    spec.setdefault("children", [])
                # keep documents minimal, so a parsed scene is byte-identical
                # to the one that rendered the script
                objects[name] = {k: v for k, v in spec.items() if v != {}}
            else:
                variables[name] = value(stmt.value)
    except (_Unparseable, ValueError, AttributeError, IndexError) as e:
        return None, f"not in the studio's own script shape ({e})"

    # No ids assigned here: the document numbers its whole log in one go when
    # the objects become create events, and two numbering passes would give
    # the same scene different ids depending on where it came from.
    log = []
    for event in events:
        event = dict(event)
        target = event.pop("target")
        log.append({"target": target, **event})
    doc = {
        "objects": [s for n, s in objects.items() if n not in nested],
        "events": log,
    }
    if variables:
        doc["variables"] = variables
    if not doc["objects"]:
        return None, "no magpylib objects"
    return doc, None


def _show_patch_targets():
    """Everywhere a script can reach show(): the magpy/module functions plus
    the base classes whose `show` attribute obj.show() binds."""
    targets = [(magpy, "show"), (_display_module, "show")]
    for cls in (magpy.magnet.Cuboid, magpy.Collection, magpy.Sensor):
        owner = next(k for k in cls.__mro__ if "show" in vars(k))
        if all(o is not owner for o, _ in targets):
            targets.append((owner, "show"))
    return targets


def _flatten_show_args(args):
    objects = []
    for arg in args:
        if isinstance(arg, list | tuple):
            objects.extend(a for a in arg if _is_scene_object(a))
        elif _is_scene_object(arg):
            objects.append(arg)
    return objects


def run_script(path):
    """Execute a magpylib script with show() intercepted.

    Returns (namespace, captured) where captured holds the objects of each
    show() call — every call the script makes is a scene candidate. Note:
    docs are built AFTER execution, so objects shown mid-script import with
    their final state.
    """
    with open(path, encoding="utf-8") as f:
        source = f.read()
    namespace = {"__name__": "__main__", "__file__": str(path)}
    captured: list[list] = []

    def _capture_show(*args, **kwargs):
        objects = _flatten_show_args(args)
        if objects:
            captured.append(objects)

    targets = _show_patch_targets()
    originals = [getattr(owner, name) for owner, name in targets]
    for owner, name in targets:
        setattr(owner, name, _capture_show)
    try:
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102 - the point
    finally:
        for (owner, name), original in zip(targets, originals):
            setattr(owner, name, original)
    return namespace, captured
