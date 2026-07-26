"""Tests for the headless engine and its JSON-RPC transport."""

import io
import json

import pytest

from magpylib_studio.rpc import serve
from magpylib_studio.session import MagpylibStudioSession


@pytest.fixture
def session():
    return MagpylibStudioSession()


def test_list_objects(session):
    objs = session.list_objects()
    assert [o["id"] for o in objs] == ["cube", "cyl"]
    assert objs[0]["label"] == "Cube"
    assert objs[0]["type"] == "magnet.Cuboid"


def test_get_schema_is_json_and_has_paths(session):
    schema = session.get_schema("cube")
    props = schema["properties"]
    assert "opacity" in props and "magnetization" in props
    json.dumps(schema)  # must be JSON-serializable


def test_get_figure_is_json_serializable(session):
    fig = session.get_figure()
    assert "data" in fig and "layout" in fig
    json.dumps(fig)  # to_json handled numpy/bdata


def test_apply_edit_updates_object_and_document(session):
    assert session.apply_edit("cube", "opacity", 0.4) == {"ok": True}
    assert session._objs["cube"].style.opacity == 0.4
    # nested path + document sync
    session.apply_edit("cube", "magnetization.mode", "arrow")
    assert session._spec("cube")["style"]["magnetization.mode"] == "arrow"


def test_apply_edit_invalid_reports_error_not_raises(session):
    res = session.apply_edit("cube", "opacity", 5)  # out of 0..1
    assert res["ok"] is False and "opacity" in res["error"]
    assert session._objs["cube"].style.opacity is None  # unchanged


def test_get_values_splits_set_and_resolved(session):
    session.apply_edit("cube", "opacity", 0.3)
    vals = session.get_values("cube")
    assert vals["set"]["opacity"] == 0.3  # explicitly set
    assert vals["resolved"]["path.line.width"] == 1  # effective default


def test_document_round_trips_through_rebuild(session):
    session.apply_edit("cube", "magnetization.mode", "arrow")
    session.apply_edit("cube", "color", "red")
    doc = session.to_dict()
    rebuilt = MagpylibStudioSession(json.loads(json.dumps(doc)))
    assert rebuilt._objs["cube"].style.magnetization.mode == "arrow"
    assert rebuilt._objs["cube"].style.color == "red"


def test_to_script_is_valid_magpylib_code(session):
    session.apply_edit("cube", "magnetization.mode", "arrow")
    script = session.to_script()
    assert "import magpylib as magpy" in script
    assert "magpy.magnet.Cuboid(" in script
    # the generated script executes and reproduces the styled scene
    ns = {}
    exec(script.replace("scene.show(backend='plotly')", ""), ns)  # noqa: S102
    assert ns["cube"].style.magnetization.mode == "arrow"


def test_add_object(session):
    res = session.add_object(
        "sphere", "magnet.Sphere",
        params={"polarization": [0, 1, 0], "diameter": 1, "position": [0, 2.5, 0]},
        style={"label": "Ball", "color": "green"},
    )
    assert res == {"ok": True}
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl", "sphere"]
    assert session._objs["sphere"].style.color == "green"
    assert len(session.scene.children) == 3


def test_add_object_rejects_duplicate_id_and_bad_specs(session):
    assert session.add_object("cube", "magnet.Sphere")["ok"] is False
    # unknown type and invalid params roll back without touching the scene
    assert session.add_object("x", "magnet.Nope")["ok"] is False
    assert session.add_object("x", "magnet.Sphere", params={"bogus": 1})["ok"] is False
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl"]
    assert session._objs["cube"] is not None  # scene rebuilt and usable
    session.get_figure()


def test_remove_object(session):
    assert session.remove_object("cyl") == {"ok": True}
    assert [o["id"] for o in session.list_objects()] == ["cube"]
    assert len(session.scene.children) == 1
    with pytest.raises(KeyError):
        session.remove_object("cyl")  # unknown id raises, like apply_edit


def test_set_param_moves_object_and_syncs_doc(session):
    assert session.set_param("cube", "position", [0, 0, 3]) == {"ok": True}
    assert list(session._objs["cube"].position) == [0, 0, 3]
    assert session._spec("cube")["params"]["position"] == [0, 0, 3]
    # bad param name rolls back
    res = session.set_param("cube", "bogus", 1)
    assert res["ok"] is False
    assert "bogus" not in session._spec("cube")["params"]


def test_set_param_survives_round_trip(session):
    session.set_param("cube", "position", [1, 2, 3])
    ns = {}
    exec(session.to_script().replace("scene.show(backend='plotly')", ""), ns)  # noqa: S102
    assert list(ns["cube"].position) == [1, 2, 3]


def test_reset_style(session):
    session.apply_edit("cube", "color", "red")
    session.apply_edit("cube", "opacity", 0.5)
    assert session.reset_style("cube", "color") == {"ok": True}
    assert session._objs["cube"].style.color is None
    assert session._objs["cube"].style.opacity == 0.5  # others untouched
    assert session.reset_style("cube", "color")["ok"] is False  # not set anymore
    assert session.reset_style("cube") == {"ok": True}  # clear all
    assert session._spec("cube")["style"] == {}
    assert session._objs["cube"].style.opacity is None


def test_load_scene_from_dict_and_file(session, tmp_path):
    doc = {"objects": [{"id": "solo", "type": "magnet.Sphere",
                        "params": {"polarization": [0, 0, 1], "diameter": 2},
                        "style": {"label": "Solo"}}]}
    assert session.load_scene(doc) == {"ok": True}
    assert [o["id"] for o in session.list_objects()] == ["solo"]

    path = tmp_path / "scene.json"
    path.write_text(json.dumps({"objects": []}), encoding="utf-8")
    assert session.load_scene(str(path)) == {"ok": True}
    assert session.list_objects() == []

    # bad path and bad document are reported, scene untouched
    assert session.load_scene(str(tmp_path / "missing.json"))["ok"] is False
    assert session.load_scene({"nope": []})["ok"] is False
    assert session.list_objects() == []


def test_jsonrpc_roundtrip():
    """Drive the stdio server end to end through pipes."""
    requests = [
        {"id": 1, "method": "list_objects"},
        {"id": 2, "method": "apply_edit",
         "params": {"object_id": "cube", "path": "opacity", "value": 0.5}},
        {"id": 3, "method": "get_values", "params": {"object_id": "cube"}},
        {"id": 4, "method": "bogus_method"},
    ]
    inp = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    out = io.StringIO()
    serve(inp=inp, out=out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    assert responses[0]["result"][0]["id"] == "cube"
    assert responses[1]["result"] == {"ok": True}
    assert responses[2]["result"]["set"]["opacity"] == 0.5
    assert responses[3]["error"]["type"] == "MethodError"  # unknown method rejected
