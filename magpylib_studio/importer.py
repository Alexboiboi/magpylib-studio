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

import keyword
import re

import magpylib as magpy
import numpy as np
from magpylib._src.display import display as _display_module

from magpylib_studio import style_compat

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
