"""Newline-delimited JSON-RPC over stdio — the transport the VS Code extension
(or any host) drives the session through. One JSON object per line:

    -> {"id": 1, "method": "get_schema", "params": {"object_id": "cube"}}
    <- {"id": 1, "result": {...}}
    <- {"id": 2, "error": {"type": "KeyError", "message": "..."}}

No dependencies, no ports, no framework — the host spawns `python -m
magpylib_studio` and reads/writes its stdio.
"""

from __future__ import annotations

import json
import sys

from magpylib_studio.session import MagpylibStudioSession

# Only these methods are callable over the wire (no dunders / private helpers).
_PUBLIC = {
    "list_objects",
    "get_schema",
    "get_values",
    "get_figure",
    "apply_edit",
    "add_object",
    "remove_object",
    "set_param",
    "reset_style",
    "load_scene",
    "load_example",
    "clear_scene",
    "batch",
    "to_dict",
    "to_script",
}


def handle(session, request):
    """Dispatch one JSON-RPC request dict, return the response dict."""
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method not in _PUBLIC:
        return {"id": rid, "error": {"type": "MethodError",
                                     "message": f"unknown method {method!r}"}}
    try:
        result = getattr(session, method)(**params)
        return {"id": rid, "result": result}
    except Exception as e:  # noqa: BLE001 - report every failure to the caller
        return {"id": rid, "error": {"type": type(e).__name__, "message": str(e)}}


def serve(session=None, inp=None, out=None):
    """Read requests line-by-line, write one response line each."""
    session = session or MagpylibStudioSession()
    inp = inp or sys.stdin
    out = out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            out.write(json.dumps({"id": None, "error": {"type": "JSONDecodeError",
                                                        "message": str(e)}}) + "\n")
            out.flush()
            continue
        out.write(json.dumps(handle(session, request)) + "\n")
        out.flush()
