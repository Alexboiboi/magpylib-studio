"""Style access that works on released magpylib as well as the property-tree
branch.

The engine only needs four things from the style layer: a JSON Schema, a
dotted-path setter, the explicitly-set values, and the resolved values. The
property-tree branch provides all four directly; released magpylib (5.2.x) has
`style.update()` and `style.as_dict()` instead, which is enough to reproduce
them. Everything else the engine uses — objects, collections, transforms,
getB/getH, show() — is core magpylib and needs no shim.

The schema is the one thing that cannot be derived from released magpylib (its
style objects carry no type/enum/range metadata), so the branch's schema is
generated into `style_schemas.json` and used as the fallback. The two style
trees are the same shape, so the inspector keeps its real widgets either way.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

_SCHEMA_FILE = Path(__file__).with_name("style_schemas.json")


@cache
def _fallback_schemas():
    with _SCHEMA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _nest(path, value):
    """'a.b.c', v -> {'a': {'b': {'c': v}}}"""
    parts = path.split(".")
    nested = value
    for part in reversed(parts):
        nested = {part: nested}
    return nested


def _flatten(mapping, prefix=""):
    flat = {}
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def schema(obj):
    """JSON Schema of an object's style — the contract the inspector widgets
    and the LLM tool both read."""
    style_cls = type(obj.style)
    if hasattr(style_cls, "schema"):
        return style_cls.schema()
    schemas = _fallback_schemas()
    if style_cls.__name__ in schemas:
        return schemas[style_cls.__name__]
    return schemas["BaseStyle"]  # unknown class: the shared properties still apply


def set_style(obj, path, value):
    """Set one dotted style path. Validation stays magpylib's either way —
    the branch validates in `set`, released magpylib in its property setters."""
    style = obj.style
    if hasattr(style, "set"):
        style.set(path, value)
    else:
        style.update(_nest(path, value))


@cache
def _pristine(style_cls):
    """A default-constructed style of this class, or None if it needs args."""
    try:
        return _flatten(style_cls().as_dict())
    except Exception:  # noqa: BLE001 - best-effort baseline
        return None


def set_values(obj):
    """Explicitly set style values, as a flat dotted dict."""
    style = obj.style
    if hasattr(style, "set_values"):
        return dict(style.set_values())
    # Released magpylib reports every property, unset ones as None. Diffing
    # against a pristine style also drops the ones that merely hold their
    # default, so the document records what the user actually set.
    current = _flatten(style.as_dict())
    baseline = _pristine(type(style)) or {}
    return {
        key: value
        for key, value in current.items()
        if value is not None and value != baseline.get(key)
    }


def resolved_values(obj):
    """Effective style values (set values merged over the defaults). Returns
    {} if this magpylib cannot resolve them."""
    try:
        from magpylib._src.defaults.defaults_classes import default_settings
        from magpylib._src.style import get_style

        resolved = get_style(obj, default_settings)
    except Exception:  # noqa: BLE001 - private API, optional feature
        return {}
    if hasattr(resolved, "as_dict"):
        try:
            return resolved.as_dict(flatten=True)
        except TypeError:
            return _flatten(resolved.as_dict())
    return {}
