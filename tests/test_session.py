"""Tests for the headless engine and its JSON-RPC transport."""

import io
import json

import pytest

from magpylib_studio.rpc import serve
from magpylib_studio.session import MagpylibStudioSession

# Small fixed scene for tests (sessions start empty by default).
TEST_SCENE = {
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


def make_scene():
    return json.loads(json.dumps(TEST_SCENE))


@pytest.fixture
def session():
    return MagpylibStudioSession(make_scene())


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


def test_default_scene_is_empty_and_renders():
    s = MagpylibStudioSession()
    assert s.list_objects() == []
    fig = s.get_figure()
    assert fig["data"] == []
    json.dumps(fig)
    assert "magpy.Collection()" in s.to_script()


def test_load_example():
    s = MagpylibStudioSession()
    assert s.load_example() == {"ok": True}
    objs = s.list_objects()
    assert {o["type"] for o in objs} == {"Collection", "magnet.Cuboid", "Sensor"}
    assert len(objs) == 24  # halbach + 2 rings + 20 cuboids + sensor
    parents = {o["id"]: o["parent"] for o in objs}
    assert parents["halbach"] is None
    assert parents["ring1"] == "halbach"
    assert parents["r1m01"] == "ring1"
    assert parents["sensor"] is None
    assert len(s.get_figure()["data"]) > 0
    # the example round-trips through the generated script
    ns = {}
    exec(s.to_script().replace("scene.show(backend='plotly')", ""), ns)  # noqa: S102
    assert ns["sensor"].position.shape == (25, 3)  # path along the bore axis
    assert len(ns["halbach"].children) == 2
    assert len(ns["ring1"].children) == 10
    # ring 2 is staggered by an 18 deg group rotation
    assert ns["r2m01"].position.round(3).tolist() != [2.3, 0, 1.5]


def test_get_field_at_points_matches_direct_getB(session):
    import magpylib as magpy
    import numpy as np

    res = session.get_field(points=[[0, 0, 2], [0, 0, 3]])
    direct = magpy.getB(
        [session._objs["cube"], session._objs["cyl"]], [[0, 0, 2], [0, 0, 3]],
        sumup=True,
    )
    assert res["field"] == "B" and res["unit"] == "T"
    assert np.allclose(res["values"], direct)
    assert len(res["magnitude"]) == 2
    json.dumps(res)


def test_get_field_from_example_sensor_path():
    s = MagpylibStudioSession()
    s.load_example()
    res = s.get_field()  # defaults to the sensor, whole path
    assert len(res["points"]) == 25 and len(res["values"]) == 25
    assert all(m > 0 for m in res["magnitude"])  # Halbach bore field is nonzero
    h = s.get_field(field="H")
    assert h["unit"] == "A/m"


def test_get_field_errors():
    s = MagpylibStudioSession()
    with pytest.raises(ValueError, match="no field sources"):
        s.get_field(points=[[0, 0, 0]])
    s.load_example()
    with pytest.raises(ValueError, match="not a Sensor"):
        s.get_field(sensor_id="r1m01")
    with pytest.raises(ValueError, match="'B' or 'H'"):
        s.get_field(field="X")
    s.remove_object("sensor")
    with pytest.raises(ValueError, match="no sensor"):
        s.get_field()


def test_get_field_figure():
    s = MagpylibStudioSession()
    s.load_example()
    fig = s.get_field_figure(template="plotly_dark")
    assert len(fig["data"]) == 1  # one trace per sensor (magpylib-rendered)
    assert fig["data"][0]["type"] == "scatter"
    assert fig["layout"]["yaxis"]["title"]["text"] == "B (T)"
    assert "template" in fig["layout"]
    json.dumps(fig)
    assert s.get_field_figure(output="Hx")["layout"]["yaxis"]["title"]["text"] == "Hx (A/m)"
    assert len(s.get_field_figure(animation=True).get("frames", [])) == 25


def test_get_figure_template(session):
    dark = session.get_figure(template="plotly_dark")
    assert dark["layout"]["template"]["layout"]["paper_bgcolor"] != "white"
    json.dumps(dark)
    # unknown template names are reported, not crashed (RPC would relay this)
    with pytest.raises(Exception, match="emplate"):
        session.get_figure(template="not_a_template")


def test_get_figure_animation():
    s = MagpylibStudioSession()
    s.load_example()
    animated = s.get_figure(animation=True)
    assert len(animated.get("frames", [])) == 25  # one per sensor path point
    assert "updatemenus" in animated["layout"]  # play button
    json.dumps(animated)
    assert not s.get_figure().get("frames")  # static by default


def test_nested_structure_ops(session):
    assert session.add_object("grp", "Collection")["ok"] is True
    assert session.add_object("ball", "magnet.Sphere",
                              params={"polarization": [0, 0, 1], "diameter": 1},
                              parent="grp")["ok"] is True
    parents = {o["id"]: o["parent"] for o in session.list_objects()}
    assert parents["ball"] == "grp" and parents["grp"] is None
    # nesting into a non-collection is rejected
    assert session.add_object("x", "magnet.Sphere", parent="cube")["ok"] is False
    # duplicate ids are caught anywhere in the tree
    assert session.add_object("ball", "magnet.Sphere")["ok"] is False
    # move: root -> group, cycle rejected, back to root
    assert session.move_object("cube", "grp")["ok"] is True
    assert {o["id"]: o["parent"] for o in session.list_objects()}["cube"] == "grp"
    assert session.move_object("grp", "grp")["ok"] is False
    assert session.move_object("cube")["ok"] is True
    # removing a collection removes its subtree
    assert session.remove_object("grp")["ok"] is True
    ids = [o["id"] for o in session.list_objects()]
    assert "ball" not in ids and "grp" not in ids and "cube" in ids
    session.get_figure()  # scene still renders


def test_rotations_build_and_round_trip():
    doc = {"objects": [{
        "id": "m", "type": "magnet.Cuboid",
        "params": {"dimension": [1, 1, 1], "polarization": [1, 0, 0],
                   "position": [2.3, 0, 0]},
        "rotations": [{"angle": 90, "axis": "z", "anchor": 0},
                      {"angle": 90, "axis": "z"}],
    }]}
    s = MagpylibStudioSession(json.loads(json.dumps(doc)))
    assert s._objs["m"].position.round(6).tolist() == [0, 2.3, 0]  # orbited 90°
    # generated script replays the rotations: same position, 180° total spin
    ns = {}
    exec(s.to_script().replace("scene.show(backend='plotly')", ""), ns)  # noqa: S102
    assert ns["m"].position.round(6).tolist() == [0, 2.3, 0]
    zrot = ns["m"].orientation.as_euler("xyz", degrees=True)[2]
    assert abs(round(abs(zrot), 3)) == 180
    # rebuild from the exported doc reproduces the same scene
    rebuilt = MagpylibStudioSession(json.loads(json.dumps(s.to_dict())))
    assert rebuilt._objs["m"].position.round(6).tolist() == [0, 2.3, 0]


def test_clear_scene(session):
    assert session.clear_scene() == {"ok": True}
    assert session.list_objects() == []
    assert session.get_figure()["data"] == []


def test_batch_applies_all_and_reports_per_op(session):
    res = session.batch([
        {"method": "clear_scene"},
        {"method": "add_object", "params": {
            "object_id": "s1", "type": "magnet.Sphere",
            "params": {"polarization": [0, 0, 1], "diameter": 1}}},
        {"method": "add_object", "params": {
            "object_id": "s2", "type": "magnet.Sphere",
            "params": {"polarization": [0, 0, 1], "diameter": 1, "position": [2, 0, 0]}}},
        {"method": "apply_edit", "params": {
            "object_id": "s1", "path": "color", "value": "green"}},
    ])
    assert res["ok"] is True
    assert all(r["ok"] for r in res["results"])
    assert [o["id"] for o in session.list_objects()] == ["s1", "s2"]
    assert session._objs["s1"].style.color == "green"


def test_batch_continues_past_failures(session):
    res = session.batch([
        {"method": "apply_edit", "params": {
            "object_id": "cube", "path": "opacity", "value": 5}},  # invalid
        {"method": "to_script"},  # not batchable
        {"method": "remove_object", "params": {"object_id": "cyl"}},  # fine
    ])
    assert res["ok"] is False
    assert [r["ok"] for r in res["results"]] == [False, False, True]
    assert [o["id"] for o in session.list_objects()] == ["cube"]


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
    serve(session=MagpylibStudioSession(make_scene()), inp=inp, out=out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    assert responses[0]["result"][0]["id"] == "cube"
    assert responses[1]["result"] == {"ok": True}
    assert responses[2]["result"]["set"]["opacity"] == 0.5
    assert responses[3]["error"]["type"] == "MethodError"  # unknown method rejected
