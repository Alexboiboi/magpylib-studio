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
  duplicate_along(object_id, count, step)  -> {"ok": bool, ...} (linear pattern)
  mirror(object_id, plane?, normal?, anchor?) -> {"ok": bool, ...} (one reflection)
  get_transform(object_id)             -> {position, orientation, path_length, ...}
  reset_style(object_id, path?)        -> {"ok": bool, "error"?: str}
  load_scene(scene | path)             -> {"ok": bool, "error"?: str}
  load_script(path, scene?)            -> {"ok", "scene", "scenes": [labels], ...}
  load_captured(scene)                 -> same (switch between captured scenes)
  apply_script(path)                   -> {"ok", "warnings"?} (edited to_script back in)
  list_examples()                      -> {"examples": [{name, label, description}]}
  load_example(name?)                  -> {"ok": bool, "error"?: str}
  clear_scene()                        -> {"ok": bool, "error"?: str}
  batch(operations)                    -> {"ok": bool, "results": [...]} (1 undo step)
  undo(steps?) / redo(steps?)          -> {"ok": bool, "error"?: str}
  get_history()                        -> {"entries": [...], "current": int, ...}
  goto_history(index)                  -> {"ok": bool, "error"?: str}
  get_variables()                      -> {"variables": [{name, expression, value}]}
  unknown_variables(values)            -> {"unknown": [names not defined yet]}
  expression_help()                    -> {operators, functions, constants, ...}
  check_expression(text)               -> {"ok": bool, "error"?: str}
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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

import magpylib as magpy
import numpy as np
import plotly.graph_objects as go
from scipy.spatial.transform import Rotation as R

from magpylib_studio import expressions, style_compat


def example_scene():
    """The built-in showcase scene: a nested Halbach stack — two rings of
    cuboids (each orbited AND spun by the ring angle, the classic magpylib
    docs pattern), the upper one staggered by half a step — plus a sensor
    path along the bore axis.

    Written the way the studio is meant to be used, because it is the first
    thing anyone opens: each ring is ONE magnet and one pattern step, not ten
    declared magnets, so the whole scene is nine steps and four variables.
    Changing `n` rebuilds both rings, and `stagger` follows it without being
    touched, being half a magnet step by definition.

    Soft bounds mark the range worth exploring — below a radius of about 1.6
    the ten unit cubes of the default ring would have to overlap (2πr < n) —
    while the hard bounds only rule out the physically impossible.
    """
    def ring(number, z):
        return {
            "id": f"ring{number}",
            "type": "Collection",
            "style": {"label": f"Ring {number}"},
            "children": [{
                "id": f"r{number}",
                "type": "magnet.Cuboid",
                "params": {
                    "dimension": [1, 1, 1],
                    "polarization": [1, 0, 0],
                    "position": ["=radius", 0, z],
                },
                "style": {"label": f"Magnet {number}"},
            }],
        }

    def ring_pattern(number):
        return {
            "target": f"r{number}", "op": "duplicate_around", "count": "=n",
            "axis": "z", "anchor": [0, 0, 0], "spin": "=360 / n",
        }

    return {
        "variables": {
            "n": 10, "radius": 2.3, "gap": 1.5, "stagger": "=360 / (2 * n)",
            "tilt": 0.0, "tilt_axis": "z",
        },
        "variable_bounds": {
            "n": {"min": 2, "max": 60, "soft_min": 4, "soft_max": 20,
                  "integer": True},
            "radius": {"min": 0.5, "max": 8, "soft_min": 1.6, "soft_max": 4},
            "gap": {"min": 0, "max": 6, "soft_min": 1, "soft_max": 3},
            "tilt": {"min": -180, "max": 180, "soft_min": -90, "soft_max": 90},
            # Not every variable is a quantity. An axis is a name, so a range
            # says nothing about it and a slider cannot offer one — options
            # are to a dropdown what bounds are to a slider.
            "tilt_axis": {"options": ["x", "y", "z"]},
        },
        "events": [
            ring_pattern(1),
            ring_pattern(2),
            # after ring 2's copies exist, so the group carries them
            {"target": "ring2", "op": "rotate_from_angax",
             "angle": "=stagger", "axis": "z", "anchor": 0},
            # Last, so it carries the whole assembled stack: tilting the
            # collection turns everything in it, copies included. Zero by
            # default — the scene looks the same until you drag `tilt`, and
            # then `tilt_axis` decides which way it tips.
            {"target": "halbach", "op": "rotate_from_angax",
             "angle": "=tilt", "axis": "=tilt_axis", "anchor": 0},
        ],
        "objects": [
            {
                "id": "halbach",
                "type": "Collection",
                "style": {"label": "Halbach stack"},
                "children": [ring(1, 0.0), ring(2, "=gap")],
            },
            _bore_sensor(-1.5, 3.0),
        ]
    }


def _bore_sensor(start, stop, steps=25, label="Sensor"):
    """A sensor walking a straight line, the usual way to read a scene."""
    return {
        "id": "sensor",
        "type": "Sensor",
        "params": {
            "position": [
                [0, 0, round(start + (stop - start) * i / (steps - 1), 3)]
                for i in range(steps)
            ]
        },
        "style": {"label": label},
    }


def coil_scene():
    """A solenoid: one current loop and a linear pattern, rather than a stack
    of declared turns. `turns` and `pitch` reshape the whole coil."""
    return {
        "variables": {
            "turns": 12, "coil_radius": 1.0, "pitch": 0.25, "amps": 100,
            "height": "=pitch * (turns - 1)",
        },
        "variable_bounds": {
            "turns": {"min": 1, "max": 200, "soft_min": 4, "soft_max": 40,
                      "integer": True},
            "coil_radius": {"min": 0.05, "max": 10, "soft_min": 0.3, "soft_max": 3},
            "pitch": {"min": 0.01, "max": 2, "soft_min": 0.1, "soft_max": 0.6},
            "amps": {"min": -10000, "max": 10000, "soft_min": 0, "soft_max": 500},
        },
        "events": [{
            "target": "turn", "op": "duplicate_along", "count": "=turns",
            "step": [0, 0, "=pitch"],
        }],
        "objects": [
            {
                "id": "coil", "type": "Collection",
                "style": {"label": "Solenoid"},
                "children": [{
                    "id": "turn", "type": "current.Circle",
                    "params": {
                        "current": "=amps",
                        "diameter": "=2 * coil_radius",
                        "position": [0, 0, "=-height / 2"],
                    },
                    "style": {"label": "Turn"},
                }],
            },
            _bore_sensor(-2.5, 2.5, label="On axis"),
        ],
    }


def pair_scene():
    """Two magnets facing across a gap, the second a mirror of the first —
    so it stays a mirror image while the first one is edited."""
    return {
        "variables": {"gap": 2.0, "size": 1.0},
        "variable_bounds": {
            "gap": {"min": 0.1, "max": 20, "soft_min": 0.5, "soft_max": 6},
            "size": {"min": 0.05, "max": 5, "soft_min": 0.5, "soft_max": 2},
        },
        "events": [
            {"target": "upper", "op": "mirror", "plane": "xy", "anchor": 0},
        ],
        "objects": [
            {
                "id": "pair", "type": "Collection",
                "style": {"label": "Facing pair"},
                "children": [{
                    "id": "upper", "type": "magnet.Cuboid",
                    "params": {
                        "dimension": ["=size", "=size", "=size"],
                        "polarization": [0, 0, -1],
                        "position": [0, 0, "=gap / 2"],
                    },
                    "style": {"label": "Upper"},
                }],
            },
            _bore_sensor(-3.0, 3.0, label="Through the gap"),
        ],
    }


def array_scene():
    """A magnet array: one magnet patterned into a row, the row patterned
    into a grid — two linear steps, both counts editable."""
    return {
        "variables": {"nx": 4, "ny": 3, "pitch": 1.5, "lift": 2.0},
        "variable_bounds": {
            "nx": {"min": 1, "max": 40, "soft_min": 2, "soft_max": 10,
                   "integer": True},
            "ny": {"min": 1, "max": 40, "soft_min": 2, "soft_max": 10,
                   "integer": True},
            "pitch": {"min": 0.2, "max": 10, "soft_min": 1, "soft_max": 4},
            "lift": {"min": 0.1, "max": 10, "soft_min": 0.5, "soft_max": 4},
        },
        "events": [
            {"target": "tile", "op": "duplicate_along", "count": "=nx",
             "step": ["=pitch", 0, 0]},
            {"target": "row", "op": "duplicate_along", "count": "=ny",
             "step": [0, "=pitch", 0]},
        ],
        "objects": [
            {
                "id": "array", "type": "Collection",
                "style": {"label": "Magnet array"},
                "children": [{
                    "id": "row", "type": "Collection",
                    "style": {"label": "Row"},
                    "children": [{
                        "id": "tile", "type": "magnet.Cuboid",
                        "params": {
                            "dimension": [1, 1, 1],
                            "polarization": [0, 0, 1],
                            "position": [0, 0, 0],
                        },
                        "style": {"label": "Tile"},
                    }],
                }],
            },
            {
                "id": "sensor", "type": "Sensor",
                "params": {
                    "position": [
                        [round(-1 + 6 * i / 24, 3), 1.5, "=lift"] for i in range(25)
                    ]
                },
                "style": {"label": "Above the array"},
            },
        ],
    }


def pixel_field_scene(resolution=7):
    """A magnet under a measuring plane: a Sensor whose pixel grid is written
    in terms of `span`, so the patch being measured resizes with it.

    magpylib's own field-on-a-plane examples build a meshgrid of observer
    points; here that grid belongs to a Sensor, so it is a real scene object
    — drawn in the 3D view, carried by the sensor's pose, and exported. A
    pixel grid is a table of numbers, and this one shows that even a table
    can be parametric: resolution cannot be (an expression yields a number,
    not an array of a different length), but every coordinate in it can.
    """
    steps = [
        (i / (resolution - 1)) - 0.5 for i in range(resolution)
    ]  # -0.5 … +0.5, scaled by `span` at build time

    def coordinate(fraction):
        return 0 if fraction == 0 else f"=span * {fraction:.6g}"

    return {
        "variables": {"span": 4.0, "lift": 1.5, "mag": 1.0},
        "variable_bounds": {
            "span": {"min": 0.1, "max": 40, "soft_min": 1, "soft_max": 10},
            "lift": {"min": 0.05, "max": 20, "soft_min": 0.5, "soft_max": 5},
            "mag": {"min": 0.05, "max": 10, "soft_min": 0.5, "soft_max": 3},
        },
        "objects": [
            {
                "id": "magnet", "type": "magnet.Cuboid",
                "params": {
                    "dimension": ["=mag", "=mag", "=mag"],
                    "polarization": [0, 0, 1],
                },
                "style": {"label": "Magnet"},
            },
            {
                "id": "probe", "type": "Sensor",
                "params": {
                    "position": [0, 0, "=lift"],
                    "pixel": [
                        [[coordinate(u), coordinate(v), 0] for u in steps]
                        for v in steps
                    ],
                },
                "style": {"label": "Measuring plane"},
            },
        ],
    }


def quiver_scene(pixels=12, steps=51):
    """magpylib's animated-quiver example as a scene: a magnet turning
    through a full revolution under a grid of field arrows, each coloured and
    scaled by what it measures.

    The two things it shows that nothing else here does — an object whose
    pose is a *path*, so the whole scene animates, and a sensor styled to
    draw its own reading — are both magpylib's, not the studio's. What the
    studio adds is that the grid rides on the sensor's position, so `lift`
    moves all 144 arrows at once.
    """
    edge = 2.0
    coordinates = [
        round(-edge + 2 * edge * i / (pixels - 1), 6) for i in range(pixels)
    ]
    return {
        "variables": {"lift": 1.0, "width": 3.0},
        "variable_bounds": {
            "lift": {"min": 0.1, "max": 10, "soft_min": 0.5, "soft_max": 3},
            "width": {"min": 0.1, "max": 10, "soft_min": 1, "soft_max": 5},
        },
        "events": [{
            "target": "magnet", "op": "rotate_from_angax",
            "angle": [round(360 * i / (steps - 1), 6) for i in range(steps)],
            "axis": "y", "start": 0,
        }],
        "objects": [
            {
                "id": "magnet", "type": "magnet.Cuboid",
                "params": {
                    "polarization": [0, 0, 1],
                    "dimension": [1, "=width", 1],
                },
                "style": {"label": "Turning magnet"},
            },
            {
                "id": "field", "type": "Sensor",
                "params": {
                    "position": [0, 0, "=lift"],
                    "pixel": [
                        [[u, v, 0] for u in coordinates] for v in coordinates
                    ],
                },
                "style": {
                    "label": "Field arrows",
                    "pixel.field.source": "B",
                    "pixel.field.symbol": "arrow3d",
                    "pixel.field.colormap": "Viridis",
                },
            },
        ],
    }


# The built-in scenes, each written the way the studio is meant to be used
# and each leaning on a different feature — which is the point of having
# more than one: an example is the shortest documentation there is.
EXAMPLES = {
    "halbach": ("Halbach stack",
                "Two rings of magnets, each one magnet and a circular "
                "pattern; the upper ring staggered by half a step",
                example_scene),
    "coil": ("Solenoid coil",
             "One current loop patterned along its axis — turns and pitch "
             "reshape the whole winding",
             coil_scene),
    "pair": ("Facing magnet pair",
             "A magnet and its mirror image across a gap, which stays a "
             "mirror image as the first is edited",
             pair_scene),
    "pixels": ("Field on a plane",
               "A magnet under a sensor whose pixel grid resizes with a "
               "variable — open the Field view and read it off the sensor",
               pixel_field_scene),
    "quiver": ("Turning magnet, field arrows",
               "A magnet rotating through a revolution under a grid of "
               "arrows coloured by what they read — press Animate paths",
               quiver_scene),
    "array": ("Magnet array",
              "A magnet patterned into a row, the row into a grid — two "
              "linear steps, both counts editable",
              array_scene),
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

# The unit each parameter is in, said outright rather than left inside the
# prose of _PARAM_DOCS for a UI to dig back out with a regex.
_PARAM_UNITS = {
    "polarization": "T",
    "magnetization": "A/m",
    "dimension": "m",
    "diameter": "m",
    "vertices": "m",
    "current": "A",
    "moment": "A·m²",
    "pixel": "m",
}

# What the components of a vector parameter are called, where they have
# names. A dimension's do not: they depend on the shape, and its doc says so.
_PARAM_COMPONENTS = {
    "polarization": ("x", "y", "z"),
    "magnetization": ("x", "y", "z"),
    "moment": ("x", "y", "z"),
}

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


# A mirror borrows the body's own z-flip symmetry, so only shapes that have
# one can be reflected; the rest would need their vertices mirrored, which is
# a different object rather than the same one placed differently.
_MIRRORABLE = ("Cuboid", "Cylinder", "CylinderSegment", "Sphere", "Dipole",
               "Sensor")

_MIRROR_NORMALS = {"xy": [0, 0, 1], "xz": [0, 1, 0], "yz": [1, 0, 0]}

# "no parent was given", which is not the same as "the scene root": a copy
# with no destination belongs beside what it was copied from. JSON cannot
# express this, so an RPC caller either omits the argument or names a place.
_BESIDE = object()

# Emitted into a script that contains a mirror, since magpylib has none. Kept
# in one piece so what runs and what parse_script reads back cannot drift.
_MIRROR_HELPER = [
    "def _mirror(obj, normal, anchor=(0, 0, 0)):",
    '    """A reflected copy. Polarization is an axial vector: its component',
    "    along the normal survives and the tangential ones reverse, which is",
    '    the opposite of what the position does."""',
    "    n = np.array(normal, dtype=float)",
    "    S = np.eye(3) - 2 * np.outer(n, n) / (n @ n)",
    "    T = np.diag([1.0, 1.0, -1.0])",
    "    a = np.array(anchor, dtype=float)",
    "    copy = obj.copy()",
    "    if obj.style.label:",
    "        copy.style.label = obj.style.label + ' #1'  # copy() renames",
    "    leaves = (list(copy.children_all)",
    "              if isinstance(copy, magpy.Collection) else [copy])",
    "    for leaf in leaves:",
    "        if isinstance(leaf, magpy.Collection):",
    "            continue",
    "        leaf.position = a + (np.array(leaf.position, dtype=float) - a) @ S.T",
    "        leaf.orientation = R.from_matrix(S @ leaf.orientation.as_matrix() @ T)",
    "        if getattr(leaf, 'polarization', None) is not None:",
    "            leaf.polarization = -(np.array(leaf.polarization, dtype=float) @ T.T)",
    "    return copy",
]


# The fields magpylib can evaluate, with the unit each comes out in. B and H
# are what a scene is usually read for; J and M are zero outside a magnet and
# constant inside one, which makes them the quick way to see what a shape
# actually covers.
_FIELDS = {
    "B": ("getB", "T"),
    "H": ("getH", "A/m"),
    "J": ("getJ", "T"),
    "M": ("getM", "A/m"),
}


def _field_sources():
    """Everything magpylib accepts as a pixel field source: a field letter,
    on its own or followed by the axes to combine — 'B', 'Bz', 'Bxy'."""
    return [
        f"{field}{axes}"
        for field in _FIELDS
        for axes in ("", "x", "y", "z", "xy", "xz", "yz", "xyz")
    ]


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
    "duplicate_along",
    "mirror",
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


def _whole(value, what):
    """A count of 7.3 is not a coarse 7.3, it is meaningless — and rounding it
    quietly is how a scene ends up with a magnet fewer than it says."""
    number = float(value)
    if number != int(number):
        raise ValueError(f"{what} has to be a whole number, got {number:g}")
    if int(number) < 1:
        raise ValueError(f"{what} must be at least 1, got {int(number)}")
    return int(number)


def _walk_specs(specs):
    """Depth-first over a plain document's specs (no session needed)."""
    for spec in specs:
        yield spec
        yield from _walk_specs(spec.get("children") or [])


#: The document format this engine reads and writes.
#:
#: Bump it when a document written here can no longer be understood by the
#: engine before it. A document with a *lower* version (or none at all — every
#: document written before this field existed) is migrated on load; one with a
#: *higher* version is refused, because reading it half-way and saving it back
#: would drop whatever we did not understand, which is worse than not opening
#: it. That refusal is the only reason to write the number down.
DOC_VERSION = 1

try:
    __version__ = _package_version("magpylib-studio")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

_DOC_KEYS = ("version", "generator", "variables", "variable_bounds",
             "objects", "events")

# What the engine itself puts on a create event and on the spec projected from
# it. Anything else on either belongs to something we do not know about — a
# newer format, a hand-written file, another tool — and is carried rather than
# dropped, so opening a document here does not quietly strip it.
_CREATE_KEYS = ("id", "op", "target", "type", "params", "style",
                "hidden_style", "parent", "visible")
_SPEC_KEYS = ("id", "type", "params", "style", "hidden_style", "visible",
              "children", "transforms", "rotations")


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
    # Every document that reaches a session comes through here, so this is
    # where it gets stamped: whatever it was written by, what we hand back is
    # ours and says so.
    doc["version"] = DOC_VERSION
    doc["generator"] = f"magpylib-studio {__version__}"
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
    described = {e["target"]: e for e in events if e.get("op") == "create"}
    creates, transforms = [], []

    def walk(specs, parent):
        for spec in specs:
            # `objects` is a projection: at the next build it is regenerated
            # from the create events, so anything on it we do not recognise
            # would be dropped. Its home is the create event, which is kept
            # verbatim — whether that event is already there or made here.
            unknown = {k: v for k, v in spec.items() if k not in _SPEC_KEYS}
            if spec["id"] in described:
                for key, value in unknown.items():
                    described[spec["id"]].setdefault(key, value)
            else:
                creates.append({
                    "id": None, "op": "create", "target": spec["id"],
                    "type": spec["type"],
                    **({"params": spec["params"]} if spec.get("params") else {}),
                    **({"style": spec["style"]} if spec.get("style") else {}),
                    **({"parent": parent} if parent else {}),
                    **({"visible": False} if spec.get("visible") is False else {}),
                    **unknown,
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


def _vec(value, unit=""):
    """A vector as something to read, not as an argument list."""
    if not isinstance(value, list):
        return f"{_lit(value)}{unit}"
    if value and isinstance(value[0], list):
        return f"{len(value)} steps"
    inner = ", ".join(_lit(v) for v in value)
    return f"({inner}){unit}"


def _axis_label(value):
    """An axis to read: `z`, not `'z'` — a label is not source."""
    if expressions.is_expression(value):
        return expressions.source_of(value)
    return value if isinstance(value, str) else _vec(value)


def _event_label(event):
    """What an event did, named for the doing of it.

    The tree shows these, so they read as steps a person took — "orbit 36°
    about z" — rather than as the call that carried it out. The call is what
    `source` is for, and what the script tab shows.
    """
    op = event.get("op", "rotate_from_angax")
    if op == "create":
        return "created"
    if op == "remove":
        return "removed"
    if op == "reparent":
        parent = event.get("parent")
        return f"moved into {parent}" if parent else "moved to the scene root"
    if op == "move":
        return f"moved by {_vec(event.get('displacement'), ' m')}"
    if op == "position":
        return f"placed at {_vec(event.get('value'), ' m')}"
    if op == "orientation":
        return f"oriented {_vec(event.get('rotvec'), '°')}"
    if op == "duplicate_around":
        return (f"{_lit(event.get('count', 1))} copies about "
                f"{_axis_label(event.get('axis', 'z'))}")
    if op == "duplicate_along":
        return (f"{_lit(event.get('count', 1))} copies every "
                f"{_vec(event.get('step'), ' m')}")
    if op == "mirror":
        plane = event.get("plane") or _vec(event.get("normal"))
        return f"mirrored in {plane}"
    if op == "rotate_from_rotvec":
        return f"turned {_vec(event.get('rotvec'), '°')}"
    # rotate_from_angax: the anchor is what makes it an orbit rather than a spin
    kind = "orbit" if event.get("anchor") is not None else "spin"
    angle = event.get("angle")
    axis = _axis_label(event.get("axis", "z"))
    if isinstance(angle, list):
        # a path: the row says what it does, not every angle it passes through
        return f"{kind} through {len(angle)} steps about {axis}, to {_lit(angle[-1])}°"
    return f"{kind} {_lit(angle)}° about {axis}"


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
    if op == "duplicate_along":
        return (f"{target} × {_lit(event.get('count', 1))} every "
                f"{_lit(event.get('step'))}")
    if op == "mirror":
        return f"{target} mirrored in {event.get('plane') or event.get('normal')}"
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
        # Copying a Collection copies everything under it, and those copied
        # descendants are magpylib objects with no id of their own — nothing
        # in the document names them. This is how they are found again:
        # source id -> the live copies of that object sitting inside some
        # generated group. Without it, removing an object patterned through
        # its parent leaves its copies standing, invisible and still in the
        # field, which is the same failure _remove already guards against one
        # level up.
        self._inherited: dict[str, list] = {}
        self._broken: list[dict] = []  # events the last fold could not apply
        self._rollback: int | None = None  # view only: fold up to here
        self._objects_view: list = []  # the tree that is actually built
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
            choices = limits.get("options")
            if choices is not None and value not in choices:
                raise ValueError(
                    f"{name} = {value!r} is not one of its options "
                    f"({', '.join(str(c) for c in choices)})"
                )
            if isinstance(value, str):
                # A named value — an axis, a plane. The limits below are all
                # numeric, and comparing a name against one used to surface as
                # a raw TypeError from deep inside the build.
                if choices is None:
                    raise ValueError(
                        f"{name} = {value!r} is a name, but {name} is limited "
                        f"as a number; give it options rather than a range"
                    )
                continue
            if limits.get("min") is not None and value < limits["min"]:
                raise ValueError(
                    f"{name} = {value:g} is below its minimum {limits['min']:g}"
                )
            if limits.get("max") is not None and value > limits["max"]:
                raise ValueError(
                    f"{name} = {value:g} is above its maximum {limits['max']:g}"
                )
            if limits.get("integer") and float(value) != int(value):
                raise ValueError(f"{name} = {value:g} counts things, so it "
                                 f"has to be a whole number")
        self._objs = {}
        self._derived = {}
        self._inherited = {}
        self._specs = {}  # id -> the spec its create event describes
        self._parents = {}  # id -> parent id or None
        self._broken = []  # events the fold could not apply, in order
        self.scene = magpy.Collection()
        for event in self._folded_events():
            try:
                self._apply(event)
            except Exception as e:  # noqa: BLE001 - one bad event is not a
                # broken document: the rest of the log still describes a
                # scene, and refusing to build it would leave nothing to
                # look at while fixing the event that went wrong.
                self._broken.append({
                    "id": event.get("id"),
                    "target": event.get("target"),
                    "source": _event_source(event),
                    "error": f"{type(e).__name__}: {e}",
                })
        # The object tree is a projection of the log, rebuilt here rather than
        # stored: two representations of the same structure would drift.
        self._objects_view = self._project()
        if self._rollback is None:
            self.doc["objects"] = self._objects_view
        # ...but a rolled-back build is a preview, so the document keeps the
        # tree of the whole log. Otherwise saving while stepping through the
        # history would write out a scene missing everything after the step.

    def _folded_events(self):
        """The events this build takes in, which is all of them unless the
        history is rolled back to an earlier step."""
        events = self.doc.get("events") or []
        if self._rollback is None:
            return events
        # undo can restore a shorter log than the step we were looking at
        self._rollback = min(self._rollback, len(events))
        return events[: self._rollback]

    def set_rollback(self, index=None):
        """Show the scene as it stood after the first `index` events, or the
        whole of it again with no argument.

        Borrowed from the rollback bar of a CAD feature tree: a history you
        can only read is far less use than one you can step through and watch
        build. It costs a rebuild, which is milliseconds, and it is a view of
        the document rather than a change to it — so nothing is saved and the
        next edit returns to the end.
        """
        total = len(self.doc.get("events") or [])
        if index is not None and not 0 <= index <= total:
            return {"ok": False, "error": f"index must be 0..{total}"}
        previous = self._rollback
        self._rollback = index
        try:
            self._build()
        except Exception as e:  # noqa: BLE001 - restore the view that worked
            self._rollback = previous
            self._build()
            return {"ok": False, "error": str(e)}
        return {"ok": True, "rollback": index, "events": total}

    def _apply(self, event):
        """Fold one event into the scene being built."""
        op = event.get("op", "rotate_from_angax")
        if op == "create":
            self._create(event)
            return
        if op == "remove":
            self._remove(event["target"])
            return
        target = self._objs.get(event["target"])
        if target is None:
            raise ValueError(f"targets unknown object {event['target']!r}")
        if op == "reparent":
            self._reparent(event["target"], event.get("parent"))
            return
        resolved = self._resolve(event)
        if op == "duplicate_around":
            self._duplicate_around(event["target"], resolved)
        elif op == "duplicate_along":
            self._duplicate_along(event["target"], resolved)
        elif op == "mirror":
            self._mirror(event["target"], resolved)
        else:
            _replay(target, [resolved])

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
        parent = self._parents.get(object_id)
        (self._objs[parent] if parent else self.scene).remove(
            self._objs[object_id], recursive=False
        )
        # Everything that only existed because this object did — which is not
        # a list but a closure, because a copy can have copies of its own. In
        # a grid (pattern the magnet, then pattern the row) the magnet's three
        # copies are each copied again into every other row, so following one
        # step finds six of the twelve and stops.
        pending = [object_id, *self._descendants(object_id)]
        seen = set()
        while pending:
            dead = pending.pop()
            if dead in seen:
                continue
            seen.add(dead)
            # A pattern's copies are part of the object they came from, so
            # they go with it. Left behind they would be invisible — nothing
            # lists them once their source is gone — while still standing in
            # the scene, contributing to every field it computes.
            for copy_id in self._derived.pop(dead, []):
                copy = self._objs.pop(copy_id, None)
                if copy is not None:
                    # wherever it ended up: the copies of a group are groups
                    self.scene.remove(copy, recursive=True, errors="ignore")
                pending.append(copy_id)  # and whatever was copied from it
            # The same argument one level up. Patterning a *group* copies
            # everything inside it, so an object can have copies it never made
            # itself, sitting in groups it was never added to. Those are the
            # worst kind to leave behind: no id, no entry in the tree, no line
            # in the exported script, and still summed into every field.
            for copy in self._inherited.pop(dead, []):
                self.scene.remove(copy, recursive=True, errors="ignore")
            self._objs.pop(dead, None)
            self._specs.pop(dead, None)
            self._parents.pop(dead, None)

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
            # Whatever the create event carries that we do not recognise shows
            # up on the projection too, so a reader of `objects` alone sees
            # everything the document holds about the object (see _SPEC_KEYS).
            spec.update({k: v for k, v in event.items() if k not in _CREATE_KEYS})
            if event["type"] == "Collection":
                spec["children"] = [
                    spec_of(child)
                    for child, parent in self._parents.items()
                    if parent == object_id
                ]
            return spec

        # _parents holds exactly the objects still alive, in creation order
        return [spec_of(oid) for oid, parent in self._parents.items() if parent is None]

    def _track_inherited(self, source, copy):
        """Remember which document object each copied descendant came from.

        Copying a Collection copies its whole subtree in one go, so the
        objects inside the copy are not created by any event and carry no id.
        magpylib copies children in order, so walking the two trees together
        pairs each new object with the one it was copied from — which is the
        only handle anything has on them afterwards.
        """
        if not isinstance(source, magpy.Collection):
            return
        by_object = {id(obj): oid for oid, obj in self._objs.items()}
        for original, made in zip(source.children_all, copy.children_all):
            source_id = by_object.get(id(original))
            if source_id is not None:
                self._inherited.setdefault(source_id, []).append(made)

    def _duplicate_around(self, object_id, event):
        """Replay a duplicate event: `count` copies evenly spaced about an
        axis, optionally spun in place as they go (which is all a Halbach ring
        is). The copies are generated, not declared — they exist only as long
        as the event does, which is what makes the count a single number to
        edit instead of twenty objects to keep in step."""
        count = _whole(event.get("count", 1), "duplicate count")
        axis = event.get("axis", "z")
        anchor = event.get("anchor", 0)
        spin = float(event.get("spin", 0))
        source = self._objs[object_id]
        container = self._container_for_copies(object_id)
        made = []
        for i in range(1, count):
            copy = source.copy()
            copy.rotate_from_angax(i * 360 / count, axis, anchor=anchor)
            if spin:
                copy.rotate_from_angax(i * spin, axis, anchor=None)
            self._name_copy(copy, source, i)
            self._track_inherited(source, copy)
            copy_id = f"{object_id}#{i}"
            self._objs[copy_id] = copy
            container.add(copy)
            made.append(copy_id)
        self._derived[object_id] = made

    def _duplicate_along(self, object_id, event):
        """The linear pattern to `duplicate_around`'s circular one: `count`
        copies, each one `step` further along than the last.

        A rectangular grid is this applied twice — pattern the object, then
        pattern the Collection holding it — which is why there is no separate
        grid op: composing the log already expresses it.
        """
        count = _whole(event.get("count", 1), "duplicate count")
        step = event.get("step", [1, 0, 0])
        source = self._objs[object_id]
        container = self._container_for_copies(object_id)
        made = []
        for i in range(1, count):
            copy = source.copy()
            copy.move([i * float(component) for component in step])
            self._name_copy(copy, source, i)
            self._track_inherited(source, copy)
            copy_id = f"{object_id}#{i}"
            self._objs[copy_id] = copy
            container.add(copy)
            made.append(copy_id)
        self._derived[object_id] = made

    def _mirror(self, object_id, event):
        """One reflected copy.

        Two things stop this being a matter of flipping a sign. A reflection
        has determinant -1, and an orientation is a *proper* rotation, so the
        mirrored frame cannot be stored as one. And polarization is an axial
        vector: under a mirror its normal component survives and its
        tangential components reverse — the opposite of what position does,
        which is why "the polarization is in the local frame, so nothing
        changes" gives the wrong magnet.

        Both are solved at once by borrowing the body's own improper symmetry
        T (a z-flip; every shape here is symmetric under it). Then

            orientation' = S · R · T     — proper again, det(-1)(+1)(-1)
            polarization' = -T · J       — the axial rule, in local terms

        which reproduces the field a mirror image would have: B is axial too,
        and B'(S·p) comes out as 2(B·n)n - B. There is a test.
        """
        normal = event.get("normal") or _MIRROR_NORMALS[event.get("plane", "xy")]
        normal = np.array(self._resolve(normal), dtype=float)
        length = np.linalg.norm(normal)
        if length < 1e-12:
            raise ValueError("a mirror plane needs a non-zero normal")
        normal = normal / length
        anchor = event.get("anchor", 0)
        anchor = np.array([0.0, 0.0, 0.0] if anchor in (0, None) else anchor,
                          dtype=float)
        reflect = np.eye(3) - 2 * np.outer(normal, normal)
        flip = np.diag([1.0, 1.0, -1.0])

        source = self._objs[object_id]
        container = self._container_for_copies(object_id)
        copy = source.copy()
        leaves = (list(copy.children_all)
                  if isinstance(copy, magpy.Collection) else [copy])
        for leaf in leaves:
            kind = type(leaf).__name__
            if kind not in _MIRRORABLE:
                raise ValueError(
                    f"{kind} cannot be mirrored: its shape has no mirror "
                    f"symmetry to borrow, so the reflection would have to "
                    f"flip its vertices"
                )
        for leaf in leaves:
            if isinstance(leaf, magpy.Collection):
                continue  # its pose setter would move the children again
            position = np.array(leaf.position, dtype=float)
            leaf.position = anchor + (position - anchor) @ reflect.T
            leaf.orientation = R.from_matrix(
                reflect @ leaf.orientation.as_matrix() @ flip
            )
            polarization = getattr(leaf, "polarization", None)
            if polarization is not None:
                leaf.polarization = -(np.array(polarization, dtype=float) @ flip.T)
        self._name_copy(copy, source, 1)
        self._track_inherited(source, copy)
        copy_id = f"{object_id}#1"
        self._objs[copy_id] = copy
        container.add(copy)
        self._derived[object_id] = [copy_id]

    @staticmethod
    def _name_copy(copy, source, index):
        """Name a generated copy after the object it came from, numbered like
        its id (`r1#3` -> "Magnet 1 #3").

        magpylib's own `copy()` renames as it goes: it increments a trailing
        number, so a copy of "Magnet 1" comes back as "Magnet 2". Every copy
        in a pattern is made from the same source, so a ring of ten read as
        one "Magnet 1" and nine identical "Magnet 2"s — nine rows claiming to
        be an object that already exists elsewhere in the scene. That is
        sensible behaviour for copying one object by hand and the wrong
        answer for a pattern, where the copies are instances of a source, not
        new objects in their own right.
        """
        label = source.style.label
        copy.style.label = f"{label} #{index}" if label else None

    def _container_for_copies(self, object_id):
        """Where a pattern's copies go: the group the source is in.

        Checked here, at the fold, and not only when the step was recorded —
        the object can be moved out of its group afterwards, and copies with
        nowhere to belong would be invisible to the exported script, which
        names the top level explicitly.
        """
        parent = self._parents.get(object_id)
        if parent is None:
            raise ValueError(
                f"{object_id!r} is not inside a Collection, so its copies "
                f"have no group to join"
            )
        return self._objs[parent]

    def _parent_at(self, event_id, object_id):
        """Which group an object was in when a given event ran.

        A pattern's copies joined the group the source was in *then*, and the
        source may have been moved since — so the exported loop has to name
        that group, not whichever one the object ended up in.
        """
        parent = None
        for event in self.doc.get("events") or []:
            if event.get("id") == event_id:
                break
            if event.get("op") == "create":
                if event["target"] == object_id:
                    parent = event.get("parent")
                elif object_id in (event.get("children") or []):
                    parent = event["target"]
            elif event.get("op") == "reparent" and event["target"] == object_id:
                parent = event.get("parent")
        return parent

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

    def _mutate_doc(self, mutate, label="edit", tolerant=False):
        """Apply `mutate(doc)` and rebuild; on any failure restore the old doc.

        The doc stays the single source of truth: structural edits go through
        the same build path as startup, so a doc that builds once always
        rebuilds — bad mutations are rolled back and reported, never applied.
        Successful mutations push the prior state onto the undo stack.

        `tolerant` is for edits to the log itself. Changing something that
        happened early can leave a later event with nothing to act on, and
        refusing the edit for that reason would make history uneditable — so
        those calls apply, and report what they broke instead.
        """
        snapshot = json.loads(json.dumps(self.doc))
        broken_before = {b["id"] for b in self._broken}
        rollback_before = self._rollback
        before = list(self.doc.get("events") or [])
        try:
            mutate(self.doc)
            inserted = self._reposition_for_rollback(before)
            _canonical(self.doc)
            self._build()
            new_breakage = [b for b in self._broken if b["id"] not in broken_before]
            if new_breakage and not tolerant:
                raise ValueError(new_breakage[0]["error"])
        except Exception as e:  # noqa: BLE001 - report every failure to the caller
            self.doc = snapshot
            self._rollback = rollback_before
            self._build()
            return {"ok": False, "error": str(e)}
        self._record_state(label, snapshot)
        result = {"ok": True}
        if new_breakage:
            result["broken"] = new_breakage
        if inserted:
            result["inserted_at"] = inserted
        return result

    def _reposition_for_rollback(self, before):
        """While the history is rolled back, new events go in *at* that step
        rather than at the end — the other half of the CAD rollback gesture.

        This is well defined precisely because a rolled-back scene only holds
        the objects that existed then: whatever you can act on is already
        there, so an inserted event cannot refer to something created later.
        The step advances past what was inserted, so several edits in a row
        stack up in the order they were made.

        Anything that did not simply append — loading a document, editing the
        log itself — returns to the end instead.
        """
        if self._rollback is None:
            return None
        events = self.doc.get("events") or []
        appended = len(events) > len(before) and events[: len(before)] == before
        if not appended:
            if events != before:
                self._rollback = None  # not an append: the preview is stale
            return None
        added = events[len(before):]
        del events[len(before):]
        events[self._rollback : self._rollback] = added
        at = self._rollback
        self._rollback += len(added)
        return at

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
        creates = {e["target"]: e for e in self.doc.get("events") or []
                   if e.get("op") == "create"}
        # what is built, which is the whole document unless it is rolled back
        for spec, parent in self._iter_specs(self._objects_view):
            objects.append({
                "id": spec["id"],
                "type": spec["type"],
                "label": self._objs[spec["id"]].style.label or spec["type"],
                "parent": parent["id"] if parent else None,
                "visible": spec.get("visible", True),
                # How the object is *written*, expressions and all — not the
                # resolved numbers. A caller that only gets ids and labels
                # cannot see the scene's scale or that it is parametric at
                # all, and fills that gap with whatever it assumes: an LLM
                # asked to extend this Halbach stack added a 15 mm magnet at
                # r = 55 mm to a scene whose magnets are 1 and whose radius
                # is "=radius". One line per object is what stops that.
                **({"source": _event_source(creates[spec["id"]])}
                   if spec["id"] in creates else {}),
                # a sensor carrying a measuring grid is a field source a UI
                # can offer to read off, so say so where it is listed
                **self._pixel_shape(self._objs[spec["id"]]),
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

    @staticmethod
    def _pixel_shape(obj):
        """{"pixels": [rows, cols]} for a Sensor with a grid, else nothing."""
        if not isinstance(obj, magpy.Sensor) or obj.pixel is None:
            return {}
        pixel = np.array(obj.pixel, dtype=float)
        return {"pixels": list(pixel.shape[:2])} if pixel.ndim == 3 else {}

    def get_schema(self, object_id):
        schema = style_compat.schema(self._objs[object_id])
        # magpylib's schema does not say what a pixel field source may be —
        # only that it is one. We know: it is the set the engine evaluates.
        # Without this the inspector has nothing to build a widget from and
        # the property silently does not appear.
        try:
            source = (schema["properties"]["pixel"]["properties"]
                      ["field"]["properties"]["source"])
        except (KeyError, TypeError):
            return schema
        source.setdefault("type", ["string", "null"])
        source.setdefault("enum", [None, *_field_sources()])
        return schema

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
                "unit": _PARAM_UNITS.get(name, ""),
                **({"components": list(_PARAM_COMPONENTS[name])}
                   if name in _PARAM_COMPONENTS else {}),
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
        using leaves avoids counting an object twice).

        Read off the scene graph rather than the id table: patterning a
        Collection copies its children too, and those copies are real magnets
        that nothing registered an id for. Asking magpylib what the scene
        contains is the only answer that stays true as the log grows ways to
        generate objects.
        """
        return list(self.scene.sources_all)

    def get_field(self, sensor_id=None, points=None, field="B"):
        """Total field of all sources, summed, at the given observers.

        Observers: explicit `points` [[x,y,z], ...] (m), else the sensor with
        `sensor_id`, else the first sensor in the scene (its whole path).
        Returns {"field", "unit", "points", "values", "magnitude"} in SI.
        """
        if field not in _FIELDS:
            raise ValueError(
                f"field must be one of {sorted(_FIELDS)}, got {field!r}"
            )
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
        func = getattr(magpy, _FIELDS[field][0])
        values = np.atleast_2d(func(sources, observer, sumup=True))
        return {
            "field": field,
            "unit": _FIELDS[field][1],
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
        func = getattr(magpy, _FIELDS[field][0])
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
            u, v, values, _FIELDS[field][1], field, component, log,
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
        ops = [
            {"op": "position", "value": np.round(world_pos, 9).tolist()},
            {"op": "orientation",
             "rotvec": np.round(world_rot.as_rotvec(degrees=True), 9).tolist()},
        ]
        # A pin supersedes the pin it directly follows. Nudging a position
        # field is one act of placing an object, not a dozen — and a log that
        # grew by two entries per nudge would be unreadable, which is the
        # thing it most needs not to be. Only at the very end of the log:
        # once anything else has happened, order matters and this must append.
        events = self.doc.setdefault("events", [])
        tail = events[-2:]
        if (len(tail) == 2
                and all(e.get("target") == object_id for e in tail)
                and [e.get("op") for e in tail] == ["position", "orientation"]):
            events[-2] = {**tail[0], **_plain(ops[0])}
            events[-1] = {**tail[1], **_plain(ops[1])}
        else:
            self._log(object_id, ops)

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

    def duplicate_along(self, object_id, count, step):
        """Record a linear pattern: `count` copies of an object (counting the
        original), each `step` further along than the last. `count` and the
        components of `step` may be expressions.

        For a rectangular grid, pattern the object and then pattern the
        Collection holding it: two linear steps compose into one, which is
        what a CAD rectangular pattern is doing behind its two-direction
        dialog. Like `duplicate_around`, the object must sit in a Collection —
        that is where the copies go.
        """
        self._spec(object_id)  # raise early on unknown id
        if self._parent_id(object_id) is None:
            return {"ok": False,
                    "error": f"{object_id!r} must be inside a Collection to "
                             f"duplicate it — the copies need a group to join"}
        return self._append_ops(
            object_id,
            [{"op": "duplicate_along", "count": count, "step": step}],
            f"duplicate {object_id}",
        )

    def mirror(self, object_id, plane="xy", normal=None, anchor=0):
        """Record a mirror: one reflected copy, in `plane` ('xy', 'xz', 'yz')
        or about an explicit `normal`, through `anchor`.

        Only shapes with a mirror symmetry of their own can be reflected —
        see `_mirror`, which explains why, and why the polarization does not
        simply come along unchanged.
        """
        self._spec(object_id)  # raise early on unknown id
        if self._parent_id(object_id) is None:
            return {"ok": False,
                    "error": f"{object_id!r} must be inside a Collection to "
                             f"mirror it — the copy needs a group to join"}
        if normal is None and plane not in _MIRROR_NORMALS:
            return {"ok": False,
                    "error": f"plane must be one of {sorted(_MIRROR_NORMALS)}"}
        op = {"op": "mirror",
              **({"normal": normal} if normal is not None else {"plane": plane}),
              "anchor": anchor}
        return self._append_ops(object_id, [op], f"mirror {object_id}")

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

    def copy_object(self, object_id, parent=_BESIDE):
        """Duplicate an object (a Collection copies its whole subtree). The
        copy's label gets magpylib's iteration suffix.

        With no `parent` the copy lands beside its source, which is where a
        copy belongs and — for an object inside a group — the only place its
        own pattern step can put *its* copies: a pattern needs a group to add
        to, so copying a patterned magnet to the scene root used to fail
        outright. Passing `parent` explicitly still says where it goes, and
        `None` still means the root, so a paste can put it anywhere.
        """
        src = self._spec(object_id)
        if parent is not _BESIDE and parent is not None and (
            self._spec(parent)["type"] != "Collection"
        ):
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
                    if parent is _BESIDE:
                        pass  # keep the source's own parent
                    elif parent is not None:
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
        a JSON file containing one. (Script -> document is deferred by design.)

        A host with its own filesystem access should pass the dict: reading
        the file here only works where this process can open() it, which is
        not everywhere a document can live.

        Versions: older documents (including every one written before the
        field existed) are migrated; a newer one is refused, because reading
        it with this engine's vocabulary and saving it back would drop
        whatever it added. See DOC_VERSION.
        """
        if isinstance(scene, str):
            try:
                with open(scene, encoding="utf-8") as f:
                    scene = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                return {"ok": False, "error": str(e)}
        # The version is read before anything else is looked at, because a
        # format we do not know may not spell the rest of it the way we expect
        # — "written by a newer version" is the useful thing to say, and
        # "not a scene document" would be a lie.
        version = scene.get("version") if isinstance(scene, dict) else None
        if isinstance(version, int) and version > DOC_VERSION:
            return {"ok": False,
                    "error": f"this scene was written by a newer magpylib-studio "
                             f"(document version {version}); this one reads up to "
                             f"version {DOC_VERSION}"}
        # A document says what it holds: since both keys are optional and an
        # empty scene is legal, something with neither is not an empty scene,
        # it is not a scene — and loading it as one would quietly wipe this.
        if not isinstance(scene, dict) or not {"objects", "events"} & set(scene):
            return {"ok": False,
                    "error": "not a scene document: expected 'objects' or 'events'"}

        def mutate(doc):
            self.doc = _canonical(_migrate_events(json.loads(json.dumps(scene))))

        # Tolerant: a document is allowed to carry events that no longer
        # apply — you can make one that way — so it has to be allowed to open
        # again, with the breakage reported rather than the file refused.
        return self._mutate_doc(mutate, "load scene", tolerant=True)

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
        # And so is a hidden object. `visible` says "do not draw this, but keep
        # summing it into the field" — a studio idea with no magpylib spelling,
        # so a script cannot carry it either. It used to be the one piece of
        # editor state that a script edit silently discarded: hide a magnet,
        # change one line of the script, save, and it was visible again.
        hidden = {
            spec["id"]
            for spec in _walk_specs(before.get("objects") or [])
            if spec.get("visible") is False
        }
        if hidden:
            for event in doc.get("events") or []:
                if event.get("op") == "create" and event["target"] in hidden:
                    event["visible"] = False
            for spec in _walk_specs(doc.get("objects") or []):
                if spec["id"] in hidden:
                    spec["visible"] = False

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

    def list_examples(self):
        """The built-in scenes. Each leans on a different feature, which is
        the point of having more than one: an example is the shortest
        documentation there is."""
        return {
            "examples": [
                {"name": name, "label": label, "description": description}
                for name, (label, description, _) in EXAMPLES.items()
            ]
        }

    def load_example(self, name="halbach"):
        """Load one of the built-in scenes; see list_examples()."""
        if name not in EXAMPLES:
            return {"ok": False,
                    "error": f"unknown example {name!r}; "
                             f"try one of {sorted(EXAMPLES)}"}
        label, _, build = EXAMPLES[name]
        result = self.load_scene(build())
        if result["ok"] and not self._history_paused and self._undo:
            self._undo[-1]["label"] = f"load {label.lower()}"
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

    def expression_help(self):
        """What an expression may contain — for a UI to show while one is
        being typed, rather than after it is rejected."""
        return expressions.reference()

    def check_expression(self, text):
        """Is this a usable expression? Names are not checked: one that does
        not exist yet is well formed, and gets offered for creation."""
        source = (
            expressions.source_of(text)
            if expressions.is_expression(text)
            else str(text)
        )
        problem = expressions.validate(source)
        return {"ok": problem is None, **({"error": problem} if problem else {})}

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
                            soft_min=None, soft_max=None, integer=None,
                            options=None):
        """Limit a variable, so a UI can offer a slider and a typo cannot put
        the scene somewhere meaningless.

        Hard bounds (`min`/`max`) are enforced: a value outside them is
        rejected, including one a variable arrives at through an expression.
        Soft bounds (`soft_min`/`soft_max`) are only the range worth sweeping
        or dragging through — values outside stay legal, which is the point of
        the distinction.

        `integer` says the variable counts things. That is a fact about the
        domain, not a hint for the slider: a count of 7.3 is not a coarse
        7.3, it is meaningless, and the patterns that consume one would
        quietly truncate it. Enforced like the hard bounds, wherever the
        value came from.

        `options` is the same idea for a value that is not on a scale at all:
        a list the variable has to be one of. A rotation axis is the reason it
        exists — `"z"` is a name, not a small number, so a range says nothing
        useful about it and a slider cannot offer it. Options give a UI a
        dropdown for the same reason bounds give it a slider.

        Passing nothing clears the limits.
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
        if integer:
            limits["integer"] = True
        if options is not None:
            if not isinstance(options, list) or not options:
                return {"ok": False,
                        "error": "options must be a non-empty list of choices"}
            if any(not isinstance(o, str | int | float) or isinstance(o, bool)
                   for o in options):
                return {"ok": False, "error": "an option is a name or a number"}
            if len(set(map(str, options))) != len(options):
                return {"ok": False, "error": "options must be distinct"}
            limits["options"] = options
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
                "unit": _FIELDS[field][1], "steps": steps}

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
        """The scene's construction history, in order: what each event did,
        and for any the last fold could not apply, why not."""
        broken = {b["id"]: b["error"] for b in self._broken}
        events = self.doc.get("events") or []
        applied = len(events) if self._rollback is None else self._rollback
        return {
            "rollback": self._rollback,
            "events": [
                {"index": i, "id": e["id"], "target": e["target"],
                 "op": e.get("op", "rotate_from_angax"),
                 "label": _event_label(e),
                 "source": _event_source(e),
                 # past the rollback point: part of the scene, not of what is
                 # currently being shown
                 **({"pending": True} if i >= applied else {}),
                 **({"error": broken[e["id"]]} if e["id"] in broken else {})}
                for i, e in enumerate(events)
            ],
        }

    def _event_index(self, event_id):
        for i, event in enumerate(self.doc.get("events") or []):
            if event["id"] == event_id:
                return i
        raise KeyError(f"unknown event id {event_id!r}")

    def edit_event(self, event_id, changes):
        """Change a past event in place; everything recorded after it is
        re-applied on top, because the scene is rebuilt by folding the whole
        log.

        An edit that cannot itself replay is rolled back. One that applies but
        leaves *later* events with nothing to act on goes through and returns
        them under "broken" — refusing it would mean history could only be
        edited when nothing depended on it, which is most of the time not the
        interesting case.
        """
        index = self._event_index(event_id)
        if not isinstance(changes, dict) or not changes:
            return {"ok": False, "error": "changes must be a non-empty object"}
        if "id" in changes:
            return {"ok": False, "error": "an event's id is not editable"}

        def mutate(doc):
            edited = {**doc["events"][index], **_plain(changes)}
            doc["events"][index] = edited
            self._must_apply = edited["id"]

        return self._edit_log(mutate, f"edit event {event_id}")

    def _edit_log(self, mutate, label):
        """A deliberate edit to the log: applied even when it breaks what came
        after, as long as the edited event itself still works."""
        self._must_apply = None
        result = self._mutate_doc(mutate, label, tolerant=True)
        if result["ok"] and self._must_apply:
            failed = next(
                (b for b in self._broken if b["id"] == self._must_apply), None
            )
            if failed:  # the edit itself is the thing that cannot replay
                self.undo()
                self._redo.clear()
                return {"ok": False, "error": failed["error"]}
        return result

    def remove_event(self, event_id):
        """Drop one event and re-fold the log without it. Whatever depended on
        it comes back under "broken" rather than blocking the removal."""
        index = self._event_index(event_id)

        def mutate(doc):
            del doc["events"][index]

        return self._edit_log(mutate, f"remove event {event_id}")

    def move_event(self, event_id, index):
        """Reorder the log. Transforms do not commute, so this is a real edit:
        rotating then moving lands somewhere else than moving then rotating."""
        current = self._event_index(event_id)
        events = self.doc["events"]
        if not 0 <= index < len(events):
            return {"ok": False, "error": f"index must be 0..{len(events) - 1}"}

        def mutate(doc):
            moved = doc["events"].pop(current)
            doc["events"].insert(index, moved)
            self._must_apply = moved["id"]

        return self._edit_log(mutate, f"move event {event_id}")

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
        """A pattern event as plain runnable magpylib: there is no library
        primitive for "N of these about an axis" or "N of these in a row", so
        each exports as the loop it means. importer.parse_script reads exactly
        these shapes back, which is what keeps an arrangement parametric
        across a round trip."""
        target = event["target"]
        count = _lit(event.get("count", 1))
        body = [
            f"for i in range(1, {count}):",
            f"    _copy = {target}.copy()",
        ]
        # Name the copies the way the studio does, because magpylib's copy()
        # would otherwise increment the label and give every one of them the
        # same wrong name (see _name_copy). Written as a concatenation rather
        # than an f-string so a label containing a brace or a quote cannot
        # break the line.
        label = self._objs[target].style.label if target in self._objs else None
        if label:
            body.append(f"    _copy.style.label = {label + ' #'!r} + str(i)")
        if event.get("op") == "duplicate_along":
            step = event.get("step") or [0, 0, 0]
            offsets = ", ".join(f"i * ({_lit(component)})" for component in step)
            body.append(f"    _copy.move(({offsets}))")
        else:
            spin = _lit(event.get("spin", 0))
            axis = _lit(event.get("axis", "z"))
            anchor = _lit(event.get("anchor", 0))
            body.append(
                f"    _copy.rotate_from_angax(i * 360 / ({count}), {axis}, "
                f"anchor={anchor})"
            )
            if event.get("spin"):
                body.append(
                    f"    _copy.rotate_from_angax(i * ({spin}), {axis}, anchor=None)"
                )
        # the copies go in the group the source was in when this step ran,
        # which is why a pattern needs one: a bare list would have to be
        # threaded into show()
        body.append(f"    {self._parent_at(event.get('id'), target)}.add(_copy)")
        return body

    def to_script(self):
        """The document as runnable magpylib, folded in log order.

        Order is the whole point. Definitions used to be hoisted above every
        step, which reads more like handwritten code and is wrong the moment a
        pattern is involved: an object added to a group that was already
        patterned would be defined *before* the loop that copies the group,
        so the script built it into every copy too. Twelve magnets in the
        scene, fifteen in its own script, and a different field. Emitting each
        event where it happened is the only shape that cannot say that.

        The cost is that a Collection can no longer take its children as
        constructor arguments — it is written before they exist — so they join
        with `.add()`, which parse_script reads back.
        """
        # Only what happened to objects the log still holds. An object that
        # was removed leaves no definition behind, so a step naming it would
        # be a NameError in the exported file — the removal is expressed by
        # its absence, and so is everything that was done to it.
        alive = {spec["id"] for spec in _walk_specs(self.doc.get("objects") or [])}
        # The tree as it ends up, which is what the definitions have to build:
        # a reparent is not written out, so an object belongs where it finally
        # is, not where it was created.
        spec_of, parent_of = {}, {}

        def index(specs, parent):
            for spec in specs:
                spec_of[spec["id"]] = spec
                parent_of[spec["id"]] = parent
                index(spec.get("children") or [], spec["id"])

        index(self.doc.get("objects") or [], None)

        log = [
            e for e in self.doc.get("events") or []
            if e.get("op") not in ("remove", "reparent")
            and e.get("target") in alive
        ]
        events = [e for e in log if e.get("op") != "create"]
        mirrors = [e for e in events if e.get("op") == "mirror"]
        needs_scipy = mirrors or any(e.get("op") == "orientation" for e in events)
        lines = ["import magpylib as magpy"]
        if mirrors:
            lines.append("import numpy as np")
        if needs_scipy:
            lines.append("from scipy.spatial.transform import Rotation as R")
        lines.append("")
        if mirrors:
            # A helper rather than a frozen pose per copy: magpylib has no
            # mirror, but a script that computes one stays parametric — the
            # copy still follows whatever the source does.
            lines += _MIRROR_HELPER + [""]
        variables = self.doc.get("variables") or {}
        if variables:
            # Real Python variables: the script stays parametric, and reading
            # it back recovers them (see importer.parse_script).
            lines += [f"{name} = {_lit(value)}" for name, value in variables.items()]
            lines.append("")

        def define(target):
            """One object, plus the line that puts it in its group."""
            spec = spec_of[target]
            parts = [f"{k}={_lit(v)}" for k, v in spec.get("params", {}).items()]
            if spec.get("style"):
                parts.append(f"style={_nest(spec['style'])!r}")
            ctor = "Collection" if spec["type"] == "Collection" else spec["type"]
            lines.append(f"{target} = magpy.{ctor}({', '.join(parts)})")
            if parent_of.get(target) is not None:
                lines.append(f"{parent_of[target]}.add({target})")

        # The log, in order, in its own notation — which is why editing a line
        # of the script edits an event. Every object has a create event to be
        # emitted from: _migrate_events synthesises one for any spec that
        # arrived without it, so there is no such thing as an object the log
        # does not describe.
        for event in log:
            op = event.get("op")
            if op == "create":
                define(event["target"])
            elif op == "mirror":
                normal = event.get("normal") or _MIRROR_NORMALS[
                    event.get("plane", "xy")
                ]
                lines.append(
                    f"{self._parent_at(event.get('id'), event['target'])}"
                    f".add(_mirror("
                    f"{event['target']}, {_lit(normal)}, "
                    f"{_lit(event.get('anchor', 0))}))"
                )
            elif op in ("duplicate_around", "duplicate_along"):
                lines += self._duplicate_source(event)
            else:
                lines.append(f"{event['target']}.{_op_source(event)}")
        names = [spec["id"] for spec in self.doc.get("objects") or []]
        # Shown as loose objects, not wrapped in a Collection: the script must
        # bind exactly the objects this document holds, so importing it back
        # reproduces the same scene. A wrapper would come back as one nested
        # group and take every id inside it with it.
        lines += ["", f"magpy.show({', '.join(names)}, backend='plotly')"
                  if names else "# empty scene"]
        return "\n".join(lines)
