"""Import existing magpylib scripts by running them, not parsing them.

The script is executed in this process (same trust as the user running it),
`show()` patched to a no-op; the magpylib objects left in the namespace are
then introspected into a studio document: variable names become ids,
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


def _tolist(value):
    return value.tolist() if isinstance(value, np.ndarray) else value


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


def _spec_from(obj, object_id, used_ids, warnings):
    if isinstance(obj, magpy.Collection):
        spec = {"id": object_id, "type": "Collection", "children": [
            _spec_from(child, _unique_id(child.style.label or "obj", used_ids),
                       used_ids, warnings)
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
            warnings.append(
                f"{object_id}: orientation path not supported, kept last orientation"
            )
        angle = float(np.linalg.norm(rotvec[-1]))
        if angle > 1e-9:
            spec["rotations"] = [
                {"angle": round(angle, 6),
                 "axis": (rotvec[-1] / angle).round(9).tolist()}
            ]
    style = dict(obj.style.set_values())
    if style:
        spec["style"] = style
    return spec


def document_from_namespace(namespace):
    """Introspect executed-script namespace into a studio document."""
    named = [
        (name, obj)
        for name, obj in namespace.items()
        if not name.startswith("_") and _is_scene_object(obj)
    ]
    # Objects reachable inside a named Collection are emitted there, not twice.
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
        _spec_from(obj, _unique_id(name, used_ids), used_ids, warnings)
        for name, obj in unique_top
    ]
    return {"objects": objects}, warnings


def _show_patch_targets():
    """Everywhere a script can reach show(): the magpy/module functions plus
    the base classes whose `show` attribute obj.show() binds."""
    targets = [(magpy, "show"), (_display_module, "show")]
    for cls in (magpy.magnet.Cuboid, magpy.Collection, magpy.Sensor):
        owner = next(k for k in cls.__mro__ if "show" in vars(k))
        if all(o is not owner for o, _ in targets):
            targets.append((owner, "show"))
    return targets


def run_script(path):
    """Execute a magpylib script (show() suppressed) and return its namespace."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    namespace = {"__name__": "__main__", "__file__": str(path)}

    def _noop_show(*args, **kwargs):
        return None

    targets = _show_patch_targets()
    originals = [getattr(owner, name) for owner, name in targets]
    for owner, name in targets:
        setattr(owner, name, _noop_show)
    try:
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102 - the point
    finally:
        for (owner, name), original in zip(targets, originals):
            setattr(owner, name, original)
    return namespace
