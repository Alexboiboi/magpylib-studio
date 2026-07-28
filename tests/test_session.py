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


def supports_property_paths():
    """Path-valued physics properties (current=[...], polarization=[[...]])
    exist on the magpylib property-tree branch, not on released magpylib."""
    import magpylib as magpy

    try:
        magpy.current.Circle(current=[1, 2], diameter=1)
    except Exception:  # noqa: BLE001 - capability probe
        return False
    return True


def make_scene():
    return json.loads(json.dumps(TEST_SCENE))


def exec_script(script):
    """Run a generated script without its final show(), return its namespace
    (which is also what apply_script imports the scene back from)."""
    body = "\n".join(
        line for line in script.splitlines() if not line.startswith("magpy.show(")
    )
    ns = {}
    exec(body, ns)  # noqa: S102 - executing the generated script is the test
    return ns


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
    ns = exec_script(script)
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
    ns = exec_script(session.to_script())
    assert list(ns["cube"].position) == [1, 2, 3]


def test_reset_style(session):
    session.apply_edit("cube", "color", "red")
    session.apply_edit("cube", "opacity", 0.5)
    assert session.reset_style("cube", "color") == {"ok": True}
    assert session._objs["cube"].style.color is None
    assert session._objs["cube"].style.opacity == 0.5  # others untouched
    assert session.reset_style("cube", "color")["ok"] is False  # not set anymore
    assert session.reset_style("cube") == {"ok": True}  # clear all
    assert session._spec("cube").get("style", {}) == {}  # pruned when empty
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
    script = s.to_script()
    assert "# empty scene" in script  # nothing to show(), and it still executes
    exec_script(script)


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
    ns = exec_script(s.to_script())
    assert ns["sensor"].position.shape == (25, 3)  # path along the bore axis
    assert len(ns["halbach"].children) == 2
    assert len(ns["ring1"].children) == 10
    # ring 2 is staggered by an 18 deg group rotation
    assert ns["r2m01"].position.round(3).tolist() != [2.3, 0, 1.5]


def test_transforms(session):
    import numpy as np

    assert session.move("cube", [0, 0, 2]) == {"ok": True}
    assert np.allclose(session._objs["cube"].position, [0, 0, 2])
    # orbit about the origin
    assert session.rotate("cube", 90, "z", anchor=[0, 0, 0]) == {"ok": True}
    assert np.allclose(session._objs["cube"].position, [0, 0, 2])  # on the axis
    assert session.move("cube", [1, 0, 0]) == {"ok": True}
    assert session.rotate("cube", 90, "z", anchor=0) == {"ok": True}
    assert np.allclose(session._objs["cube"].position, [0, 1, 2])
    # absolute pose
    assert session.set_transform("cube", position=[3, 0, 0],
                                 orientation=[0, 0, 45]) == {"ok": True}
    t = session.get_transform("cube")
    assert np.allclose(t["position"], [3, 0, 0])
    assert round(t["euler"][2], 6) == 45.0 and t["path_length"] == 1
    # transforms are undoable like any other edit
    assert session.undo() == {"ok": True}
    assert np.allclose(session._objs["cube"].position, [0, 1, 2])


def test_start_matches_magpylib(session):
    """`start` is passed through to magpylib unchanged, including its default."""
    import magpylib as magpy
    import numpy as np

    for kwargs in ({}, {"start": 0}, {"start": -1}):
        s = MagpylibStudioSession(make_scene())
        s.move("cube", [[0, 0, 10], [0, 0, 20]])
        s.move("cube", [[1, 0, 0], [2, 0, 0]], **kwargs)

        ref = magpy.magnet.Cuboid(polarization=(0, 0, 1), dimension=(1, 1, 1))
        ref.move([[0, 0, 10], [0, 0, 20]])
        ref.move([[1, 0, 0], [2, 0, 0]], **kwargs)

        assert np.allclose(
            np.atleast_2d(s._objs["cube"].position), np.atleast_2d(ref.position)
        ), kwargs


def test_transform_paths(session):
    import numpy as np

    steps = [[0, 0, z] for z in np.linspace(0, 3, 5)]
    assert session.move("cube", steps, start=0) == {"ok": True}
    assert session.get_transform("cube")["path_length"] == 5

    assert session.rotate("cube", list(np.linspace(0, 90, 5)), "z",
                          anchor=0, start=0) == {"ok": True}
    obj = session._objs["cube"]
    assert len(obj.position) == 5 and len(obj.orientation) == 5

    # both paths survive export
    ns = exec_script(session.to_script())
    assert np.allclose(ns["cube"].position, obj.position)
    assert np.allclose(ns["cube"].orientation.as_matrix(), obj.orientation.as_matrix())

    assert session.clear_path("cube") == {"ok": True}
    assert session.get_transform("cube")["path_length"] == 1


def test_copy_object_follows_magpylib_label_convention(session):
    res = session.copy_object("cube")
    assert res["ok"] is True
    copied = {o["id"]: o for o in session.list_objects()}[res["id"]]
    assert copied["label"] == "Cube_01"  # magpylib's suffix convention
    assert copied["parent"] is None
    # copying the copy increments, ids stay unique
    second = session.copy_object(res["id"])
    assert {o["id"]: o["label"] for o in session.list_objects()}[second["id"]] == (
        "Cube_02"
    )
    assert len({o["id"] for o in session.list_objects()}) == 4

    # a copied collection brings its subtree, and can be pasted into a group
    session.add_object("grp", "Collection")
    session.add_object("inner", "magnet.Sphere",
                       params={"polarization": [0, 0, 1], "diameter": 1},
                       parent="grp")
    grp_copy = session.copy_object("grp", parent="grp")
    parents = {o["id"]: o["parent"] for o in session.list_objects()}
    assert parents[grp_copy["id"]] == "grp"
    assert sum(1 for p in parents.values() if p == grp_copy["id"]) == 1
    session.get_figure()  # the duplicated scene still renders


def _geometry(session):
    """Per-trace (name, type, colour, point count) of the current figure."""
    out = []
    for trace in session.get_figure()["data"]:
        x = trace.get("x")
        size = len(x["bdata"]) if isinstance(x, dict) else len(x or [])
        out.append((trace.get("name"), trace.get("type"), trace.get("color"), size))
    return out


def test_set_visible_hides_without_disturbing_colours(session):
    """Hiding uses magpylib's own switches, so the object keeps its slot in
    the colour sequence and the others cannot be recoloured."""
    baseline = _geometry(session)
    assert session.set_visible("cyl", False) == {"ok": True}
    assert {o["id"]: o["visible"] for o in session.list_objects()}["cyl"] is False

    hidden = _geometry(session)
    assert [t[:3] for t in hidden] == [t[:3] for t in baseline]  # same traces/colours
    assert sum(t[3] for t in hidden) < sum(t[3] for t in baseline)  # less geometry
    # display-only: hidden sources still contribute to the field
    assert session.get_field(points=[[0, 0, 5]])["magnitude"][0] > 0

    assert session.set_visible("cyl", True) == {"ok": True}
    assert _geometry(session) == baseline
    assert "hidden_style" not in session._spec("cyl")
    assert "model3d.showdefault" not in session._spec("cyl").get("style", {})


def test_set_visible_preserves_user_style_and_paths(session):
    session.apply_edit("cube", "path.show", False)  # user's own setting
    session.set_visible("cube", False)
    session.set_visible("cube", True)
    assert session._spec("cube")["style"]["path.show"] is False  # not clobbered

    # hiding a collection hides every leaf beneath it, path lines included
    session.add_object("grp", "Collection")
    session.move_object("cyl", "grp")
    before = sum(t[3] for t in _geometry(session))
    assert session.set_visible("grp", False) == {"ok": True}
    assert sum(t[3] for t in _geometry(session)) < before
    assert session._spec("cyl")["style"]["model3d.showdefault"] is False


def test_get_params_exposes_physics_properties(session):
    params = {p["name"]: p for p in session.get_params("cube")}
    assert params["polarization"]["value"] == [0, 0, 1]
    assert params["polarization"]["kind"] == "vector"
    assert params["dimension"]["value"] == [1, 1, 1]
    assert all(p["doc"] for p in params.values())
    assert "position" not in params  # transform-managed, not a property
    json.dumps(session.get_params("cube"))

    # editing one goes through set_param and keeps everything else
    assert session.set_param("cube", "dimension", [2, 1, 1]) == {"ok": True}
    assert {p["name"]: p["value"] for p in session.get_params("cube")}["dimension"] == [
        2, 1, 1,
    ]

    # scalar and matrix kinds
    session.add_object("loop", "current.Circle", params={"current": 5, "diameter": 2})
    loop = {p["name"]: p for p in session.get_params("loop")}
    assert loop["current"]["kind"] == "scalar" and loop["current"]["value"] == 5
    session.add_object("line", "current.Polyline",
                       params={"current": 1, "vertices": [[0, 0, 0], [1, 0, 0]]})
    assert {p["name"]: p["kind"] for p in session.get_params("line")}["vertices"] == (
        "matrix"
    )
    assert session.get_params("cyl") != []  # every source type reports something


def test_collection_transforms_carry_children():
    """Transforming a Collection must transform its whole subtree — magpylib's
    own semantics, which the doc gets by replaying recorded ops."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    child = np.array(s._objs["r1m01"].position)

    assert s.move("ring1", [0, 0, 5]) == {"ok": True}
    assert np.allclose(s._objs["ring1"].position, [0, 0, 5])
    assert np.allclose(s._objs["r1m01"].position, child + [0, 0, 5])

    assert s.rotate("ring1", 90, "z", anchor=0) == {"ok": True}
    assert np.allclose(s._objs["r1m01"].position, [0, 2.3, 5])

    # a transform on the outer stack moves the nested rings too
    assert s.move("halbach", [10, 0, 0]) == {"ok": True}
    assert np.allclose(s._objs["r1m01"].position, [10, 2.3, 5])

    ns = exec_script(s.to_script())
    assert np.allclose(ns["r1m01"].position, s._objs["r1m01"].position)
    json.dumps(s.to_dict())  # recorded ops stay JSON-safe


def test_reparenting_a_collection_keeps_its_subtree():
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    kids = np.array([s._objs[f"r2m{i:02d}"].position for i in range(1, 11)])
    assert s.move_object("ring2", None) == {"ok": True}  # out of "halbach"
    assert {o["id"]: o["parent"] for o in s.list_objects()}["ring2"] is None
    assert np.allclose(
        kids, [s._objs[f"r2m{i:02d}"].position for i in range(1, 11)]
    )


def test_transform_respects_parent_frame():
    """A transform inside a rotated Collection stays in world coordinates."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()  # ring2 carries an 18 deg group rotation
    assert s.set_transform("r2m01", position=[5, 0, 0]) == {"ok": True}
    assert np.allclose(s._objs["r2m01"].position, [5, 0, 0])
    assert s.move("r2m01", [0, 0, 1]) == {"ok": True}
    assert np.allclose(s._objs["r2m01"].position, [5, 0, 1])


def test_move_preserves_world_pose():
    """Reparenting must not teleport: a Collection's group rotation would
    otherwise be applied on top of already-transformed coordinates."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()  # ring2 carries an 18 deg group rotation
    pos = np.array(s._objs["r1m01"].position)
    rot = s._objs["r1m01"].orientation.as_matrix()

    assert s.move_object("r1m01", "ring2") == {"ok": True}
    assert {o["id"]: o["parent"] for o in s.list_objects()}["r1m01"] == "ring2"
    assert np.allclose(s._objs["r1m01"].position, pos)
    assert np.allclose(s._objs["r1m01"].orientation.as_matrix(), rot)

    assert s.move_object("r1m01") == {"ok": True}  # back out to the root
    assert np.allclose(s._objs["r1m01"].position, pos)
    assert np.allclose(s._objs["r1m01"].orientation.as_matrix(), rot)

    # objects with a position path keep it too, and the export agrees
    sensor_path = np.array(s._objs["sensor"].position)
    s.move_object("sensor", "ring2")
    assert np.allclose(s._objs["sensor"].position, sensor_path)
    ns = exec_script(s.to_script())
    assert np.allclose(ns["sensor"].position, sensor_path)

    # the group rotation still applies to the ring as a whole afterwards
    s.apply_edit("ring2", "label", "Ring 2")  # touching ring2 leaves poses put
    assert np.allclose(s._objs["sensor"].position, sensor_path)


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


def test_field_map_plane():
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    fig = s.get_field_map(plane="xy", offset=0.75, resolution=12)
    trace = fig["data"][0]
    assert trace["type"] == "heatmap"
    assert np.array(trace["z"]).shape == (12, 12)
    assert fig["layout"]["yaxis"]["scaleanchor"] == "x"  # undistorted geometry
    assert "zmid" not in trace  # magnitude is sequential, no diverging midpoint
    json.dumps(fig)

    # a signed component gets a diverging scale anchored at zero
    signed = s.get_field_map(plane="xz", component="z", resolution=8)["data"][0]
    assert signed["zmid"] == 0.0
    values = np.array(signed["z"])
    assert values.min() < 0 < values.max()

    # log only applies to the magnitude, and compresses the range
    linear = np.array(s.get_field_map(resolution=8)["data"][0]["z"])
    logged = np.array(s.get_field_map(resolution=8, log=True)["data"][0]["z"])
    assert np.allclose(logged, np.log10(linear))

    with pytest.raises(ValueError, match="plane must be"):
        s.get_field_map(plane="ab")
    with pytest.raises(ValueError, match="component"):
        s.get_field_map(component="q")


def test_field_map_from_sensor_pixel_grid():
    """magpylib's own mechanism: the plane is a Sensor's pixel grid, so it is
    visible in the 3D view and follows the sensor's pose."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    assert s.set_pixel_grid("sensor", plane="xy", size=6, resolution=10) == {"ok": True}
    pixel = np.array(
        next(p["value"] for p in s.get_params("sensor") if p["name"] == "pixel")
    )
    assert pixel.shape == (10, 10, 3)

    fig = s.get_field_map(sensor_id="sensor")
    assert np.array(fig["data"][0]["z"]).shape == (10, 10)  # path dim collapsed
    assert "10×10 pixels" in fig["layout"]["title"]["text"]

    # the measurement plane follows the sensor
    before = np.array(fig["data"][0]["z"])
    s.rotate("sensor", 30, "x")
    assert not np.allclose(np.array(s.get_field_map(sensor_id="sensor")["data"][0]["z"]),
                           before)
    assert "pixel" in s.to_script()  # exported like any other magpylib scene

    assert s.set_pixel_grid("r1m01", plane="xy")["ok"] is False  # not a sensor
    s.add_object("bare", "Sensor")
    with pytest.raises(ValueError, match="no pixel grid"):
        s.get_field_map(sensor_id="bare")


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
    ns = exec_script(s.to_script())
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


def test_undo_redo_style_and_structure(session):
    session.apply_edit("cube", "color", "red")
    session.remove_object("cyl")
    history = session.get_history()
    assert history["undo"] == ["edit cube color", "remove cyl"]
    assert [e["label"] for e in history["entries"]] == [
        "Initial state", "edit cube color", "remove cyl",
    ]
    assert history["current"] == 2

    assert session.undo() == {"ok": True}  # cyl back
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl"]
    assert session._objs["cube"].style.color == "red"  # first edit still applied

    assert session.undo() == {"ok": True}  # color back to default
    assert session._objs["cube"].style.color is None

    assert session.redo() == {"ok": True}
    assert session._objs["cube"].style.color == "red"
    history = session.get_history()
    assert history["undo"] == ["edit cube color"]
    assert history["redo"] == ["remove cyl"]
    assert history["current"] == 1  # timeline keeps the redoable change

    # a new edit clears the redo branch
    session.apply_edit("cube", "opacity", 0.5)
    assert session.get_history()["redo"] == []

    assert session.undo(steps=2) == {"ok": True}
    assert session.undo() == {"ok": False, "error": "nothing to undo"}
    assert session.redo(steps=2) == {"ok": True}
    assert session.redo()["ok"] is False


def test_goto_history_jumps_anywhere(session):
    session.apply_edit("cube", "color", "red")
    session.apply_edit("cube", "opacity", 0.5)
    session.remove_object("cyl")
    assert session.get_history()["current"] == 3

    assert session.goto_history(0) == {"ok": True}  # back to the start
    assert session._objs["cube"].style.color is None
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl"]
    # the timeline is intact and everything ahead is redoable
    history = session.get_history()
    assert history["current"] == 0 and len(history["entries"]) == 4

    assert session.goto_history(2) == {"ok": True}  # jump forward
    assert session._objs["cube"].style.color == "red"
    assert session._objs["cube"].style.opacity == 0.5
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl"]

    assert session.goto_history(2) == {"ok": True}  # no-op
    assert session.goto_history(9)["ok"] is False


def test_batch_is_one_undo_step(session):
    session.batch([
        {"method": "apply_edit", "params": {
            "object_id": "cube", "path": "color", "value": "green"}},
        {"method": "remove_object", "params": {"object_id": "cyl"}},
    ])
    assert session.get_history()["undo"] == ["batch (2 ops)"]
    assert session.undo() == {"ok": True}
    assert [o["id"] for o in session.list_objects()] == ["cube", "cyl"]
    assert session._objs["cube"].style.color is None
    # failed edits don't pollute history
    session.apply_edit("cube", "opacity", 7)
    session.add_object("cube", "magnet.Sphere")
    assert session.get_history()["undo"] == []


HALBACH_SCRIPT = """
import numpy as np
import magpylib as magpy

N = 10
angles = np.linspace(0, 360, N, endpoint=False)

halbach = magpy.Collection(style_label="Halbach")

for a in angles:
    cube = magpy.magnet.Cuboid(
        dimension=(1, 1, 1),
        polarization=(1, 0, 0),
        position=(2.3, 0, 0),
    )
    cube.rotate_from_angax(a, 'z', anchor=0)
    cube.rotate_from_angax(a, 'z')
    halbach.add(cube)

sensor = magpy.Sensor(position=[[0, 0, z] for z in (-1, 0, 1)])

halbach.show(backend='plotly')
"""


def test_load_script_captures_show_call(tmp_path):
    import numpy as np

    path = tmp_path / "halbach.py"
    path.write_text(HALBACH_SCRIPT, encoding="utf-8")
    s = MagpylibStudioSession()
    res = s.load_script(str(path))
    assert res["ok"] is True, res
    # default scene = what the script showed: the halbach ring, no sensor
    assert res["scene"] == 0 and len(res["scenes"]) == 2
    parents = {o["id"]: o["parent"] for o in s.list_objects()}
    assert parents["halbach"] is None and "sensor" not in parents
    assert sum(1 for p in parents.values() if p == "halbach") == 10
    # geometry survives: third magnet sits at 72 deg on the r=2.3 ring,
    # spun so its polarization points 144 deg from x
    m = s._objs["halbach"].children[2]
    a = np.deg2rad(72)
    assert np.allclose(m.position, [2.3 * np.cos(a), 2.3 * np.sin(a), 0])
    rotvec = m.orientation.as_rotvec(degrees=True)
    assert round(np.linalg.norm(rotvec), 3) == 144.0

    # switch to the "all script objects" candidate: sensor included
    res2 = s.load_captured(1)
    assert res2["ok"] is True
    assert s._objs["sensor"].position.shape == (3, 3)
    # each import is one undoable step; scene renders; script round-trips
    assert [h.startswith("import ") for h in s.get_history()["undo"]] == [True, True]
    assert len(s.get_figure()["data"]) > 0
    ns = exec_script(s.to_script())
    assert np.allclose(ns["halbach"].children[2].position, m.position)

    assert s.load_captured(5)["ok"] is False  # out of range


def test_load_script_orientation_paths(tmp_path):
    import magpylib as magpy
    import numpy as np

    script = """
import numpy as np
import magpylib as magpy

rotor = magpy.magnet.Cuboid(polarization=(1, 0, 0), dimension=(1, 1, 1),
                            position=(2, 0, 0))
rotor.rotate_from_angax(np.linspace(0, 270, 10), 'z', anchor=0)
"""
    path = tmp_path / "paths.py"
    path.write_text(script, encoding="utf-8")
    s = MagpylibStudioSession()
    res = s.load_script(str(path))
    assert res["ok"] is True, res
    assert "warnings" not in res  # orientation paths import exactly

    orig = magpy.magnet.Cuboid(polarization=(1, 0, 0), dimension=(1, 1, 1),
                               position=(2, 0, 0))
    orig.rotate_from_angax(np.linspace(0, 270, 10), "z", anchor=0)
    rotor = s._objs["rotor"]
    assert np.allclose(rotor.position, orig.position)
    assert np.allclose(rotor.orientation.as_matrix(), orig.orientation.as_matrix())
    # and the generated script reproduces it
    ns = exec_script(s.to_script())
    assert np.allclose(ns["rotor"].orientation.as_matrix(), orig.orientation.as_matrix())


@pytest.mark.skipif(
    not supports_property_paths(),
    reason="path-valued properties need the magpylib property-tree branch",
)
def test_load_script_property_paths(tmp_path):
    import numpy as np

    script = """
import magpylib as magpy

pulsed = magpy.current.Circle(current=[100, 200, 300], diameter=2)
fading = magpy.magnet.Sphere(polarization=[[0, 0, 1], [0, 0, 0.5], [0, 0, 0.1]],
                             diameter=1, position=(0, 0, 3))
"""
    path = tmp_path / "props.py"
    path.write_text(script, encoding="utf-8")
    s = MagpylibStudioSession()
    assert s.load_script(str(path))["ok"] is True
    assert np.array(s._objs["pulsed"].current).tolist() == [100, 200, 300]
    assert np.array(s._objs["fading"].polarization).shape == (3, 3)
    ns = exec_script(s.to_script())
    assert np.array(ns["fading"].polarization).shape == (3, 3)


def test_load_script_multiple_shows(tmp_path):
    script = """
import magpylib as magpy
a = magpy.magnet.Sphere(polarization=(0, 0, 1), diameter=1)
b = magpy.magnet.Sphere(polarization=(0, 0, 1), diameter=1, position=(2, 0, 0))
magpy.show(a)
magpy.show(a, b)
"""
    path = tmp_path / "two_shows.py"
    path.write_text(script, encoding="utf-8")
    s = MagpylibStudioSession()
    res = s.load_script(str(path))
    assert res["ok"] is True
    # two show calls; the second equals "all objects" so no extra candidate
    assert len(res["scenes"]) == 2
    assert [o["id"] for o in s.list_objects()] == ["a"]  # first show
    s.load_captured(1)
    assert [o["id"] for o in s.list_objects()] == ["a", "b"]


def test_load_script_errors(tmp_path):
    s = MagpylibStudioSession()
    bad = tmp_path / "bad.py"
    bad.write_text("this is not python", encoding="utf-8")
    assert s.load_script(str(bad))["ok"] is False
    empty = tmp_path / "empty.py"
    empty.write_text("x = 1\n", encoding="utf-8")
    res = s.load_script(str(empty))
    assert res["ok"] is False and "no magpylib objects" in res["error"]
    assert s.load_script(str(tmp_path / "missing.py"))["ok"] is False
    assert s.list_objects() == []  # scene untouched by failed imports


def test_apply_script_parses_its_own_shape_losslessly(tmp_path):
    """The editable script tab. Reading the script as *source* rather than
    running it makes the round trip an identity on the whole document — the
    event log included, which executing it could never recover."""
    s = MagpylibStudioSession()
    s.load_example()
    before = json.dumps(s.to_dict())
    path = tmp_path / "scene.py"
    path.write_text(s.to_script(), encoding="utf-8")

    res = s.apply_script(str(path))
    assert res["ok"] is True and res["mode"] == "parsed"
    assert "warnings" not in res  # nothing was lost, so there is nothing to say
    assert json.dumps(s.to_dict()) == before
    assert s.to_script() == path.read_text(encoding="utf-8")  # a fixed point


def test_apply_script_runs_what_it_cannot_parse(tmp_path):
    """A script with real Python in it still imports — by execution, which
    sees only the objects, so the flattening is reported."""
    import numpy as np

    s = MagpylibStudioSession()
    path = tmp_path / "loop.py"
    path.write_text(
        "import magpylib as magpy\n"
        "ring = magpy.Collection()\n"
        "for i in range(4):\n"
        "    m = magpy.magnet.Cuboid(polarization=(1, 0, 0), dimension=(1, 1, 1),\n"
        "                            position=(2, 0, 0))\n"
        "    m.rotate_from_angax(90 * i, 'z', anchor=0)\n"
        "    ring.add(m)\n"
        "magpy.show(ring, backend='plotly')\n",
        encoding="utf-8",
    )
    res = s.apply_script(str(path))
    assert res["ok"] is True and res["mode"] == "executed"
    # the loop flattened into four concrete magnets, geometry intact
    assert len(s.list_objects()) == 5  # the collection plus its four magnets
    assert np.allclose(s._objs["ring"].children[1].position, [0, 2, 0])


def test_apply_script_keeps_variables_through_the_round_trip(tmp_path):
    s = MagpylibStudioSession(make_scene())
    assert s.set_variable("gap", 0.75) == {"ok": True}
    assert s.set_variable("twice", "=gap*2") == {"ok": True}
    assert s.set_param("cube", "position", [0, 0, "=twice"]) == {"ok": True}
    assert list(s._objs["cube"].position) == [0, 0, 1.5]

    path = tmp_path / "scene.py"
    script = s.to_script()
    assert "gap = 0.75" in script and "twice = gap * 2" in script
    assert "position=(0, 0, twice)" in script  # parametric, not resolved away
    path.write_text(script, encoding="utf-8")

    res = s.apply_script(str(path))
    assert res["mode"] == "parsed"
    assert s.doc["variables"] == {"gap": 0.75, "twice": "=gap * 2"}
    assert s._spec("cube")["params"]["position"] == [0, 0, "=twice"]
    # and the variable still drives the scene after the round trip
    assert s.set_variable("gap", 1.0) == {"ok": True}
    assert list(s._objs["cube"].position) == [0, 0, 2.0]


def test_apply_script_applies_edits_as_one_undo_step(tmp_path):
    s = MagpylibStudioSession(make_scene())
    path = tmp_path / "scene.py"
    path.write_text(
        s.to_script().replace("dimension=(1, 1, 1)", "dimension=(2, 2, 2)"),
        encoding="utf-8",
    )
    assert s.apply_script(str(path))["ok"] is True
    assert s._spec("cube")["params"]["dimension"] == [2.0, 2.0, 2.0]
    assert s.get_history()["undo"][-1] == "edit script"
    assert s.undo() == {"ok": True}
    assert s._spec("cube")["params"]["dimension"] == [1, 1, 1]


def test_apply_script_errors_leave_the_scene_alone(tmp_path):
    s = MagpylibStudioSession(make_scene())
    bad = tmp_path / "bad.py"
    bad.write_text("import magpylib as magpy\nthis is not python\n", encoding="utf-8")
    assert s.apply_script(str(bad))["ok"] is False
    empty = tmp_path / "empty.py"
    empty.write_text("x = 1\n", encoding="utf-8")
    res = s.apply_script(str(empty))
    # emptying the script is a failure, not a silent wipe of the scene
    assert res["ok"] is False and "no magpylib objects" in res["error"]
    assert [o["id"] for o in s.list_objects()] == ["cube", "cyl"]


def test_legacy_per_object_transforms_migrate_into_the_log():
    """Documents written before the log keep working: their per-object ops
    fold into it in the order the old build replayed them — children first,
    so a Collection's group transform still lands on top of them."""
    doc = {
        "objects": [{
            "id": "ring", "type": "Collection",
            "rotations": [{"angle": 18, "axis": "z", "anchor": 0}],
            "children": [{
                "id": "m", "type": "magnet.Cuboid",
                "params": {"polarization": [1, 0, 0], "dimension": [1, 1, 1],
                           "position": [2, 0, 0]},
                "rotations": [{"angle": 90, "axis": "z", "anchor": 0}],
            }],
        }]
    }
    s = MagpylibStudioSession(json.loads(json.dumps(doc)))
    assert [e["target"] for e in s.get_events()["events"]] == ["m", "ring"]
    assert "transforms" not in s._spec("m") and "rotations" not in s._spec("m")
    # 90 deg orbit then an 18 deg group orbit = 108 deg from +x, at radius 2
    import numpy as np

    a = np.deg2rad(108)
    assert np.allclose(s._objs["m"].position, [2 * np.cos(a), 2 * np.sin(a), 0])


def test_editing_a_past_event_reapplies_the_later_ones():
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()  # ring2's group stagger is the last event, its magnets' earlier
    events = s.get_events()["events"]
    stagger = next(e for e in events if e["target"] == "ring2")
    assert stagger["source"] == "ring2.rotate_from_angax(18, 'z', anchor=0)"

    before = np.array(s._objs["r2m01"].position)
    assert s.edit_event(stagger["id"], {"angle": 45}) == {"ok": True}
    # the whole group followed the edited event, not just the object it names
    moved = np.array(s._objs["r2m01"].position)
    assert not np.allclose(moved, before)
    assert np.allclose(np.linalg.norm(moved[:2]), np.linalg.norm(before[:2]))
    assert s.get_history()["undo"][-1] == f"edit event {stagger['id']}"
    assert s.undo() == {"ok": True}
    assert np.allclose(s._objs["r2m01"].position, before)


def test_event_edits_that_cannot_replay_roll_back():
    import numpy as np

    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z", anchor=[0, 0, 0])  # an orbit, so order shows
    s.move("cube", [1, 0, 0])
    events = s.get_events()["events"]
    assert [e["index"] for e in events] == [0, 1]

    pos = list(s._objs["cube"].position)
    assert s.edit_event(events[0]["id"], {"target": "ghost"})["ok"] is False
    assert s.edit_event(events[0]["id"], {"axis": "banana"})["ok"] is False
    assert list(s._objs["cube"].position) == pos  # log intact, scene intact
    with pytest.raises(KeyError):
        s.edit_event("e99", {"angle": 1})

    # order is semantic: orbit-then-move lands elsewhere than move-then-orbit
    assert np.allclose(pos, [1, 0, 0])
    assert s.move_event(events[1]["id"], 0) == {"ok": True}
    assert np.allclose(s._objs["cube"].position, [0, 1, 0])
    assert s.remove_event(events[0]["id"]) == {"ok": True}
    assert len(s.get_events()["events"]) == 1


def test_removing_an_object_takes_its_events_with_it():
    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z")
    s.rotate("cyl", 45, "z")
    assert len(s.get_events()["events"]) == 2
    assert s.remove_object("cube") == {"ok": True}
    assert [e["target"] for e in s.get_events()["events"]] == ["cyl"]


def test_copying_an_object_copies_its_events():
    import numpy as np

    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z", anchor=[0, 0, 0])
    s.move("cube", [0, 0, 2])
    res = s.copy_object("cube")
    assert res["ok"] is True
    # the copy replays the same construction, so it lands on the original
    assert np.allclose(s._objs[res["id"]].position, s._objs["cube"].position)
    assert len(s.get_events()["events"]) == 4


def test_expressions_are_evaluated_not_executed():
    """A document is something you open from someone else: an expression is
    arithmetic over the variables, never a way to run code."""
    from magpylib_studio import expressions

    lookup = {"a": 3.0, "b": 4.0}.__getitem__
    assert expressions.evaluate("a * b + 1", lookup) == 13.0
    assert expressions.evaluate("hypot(a, b)", lookup) == 5.0
    assert expressions.evaluate("round(degrees(pi))", lookup) == 180
    assert expressions.evaluate("[0, 0, a]", lookup) == [0, 0, 3.0]
    for hostile in (
        "__import__('os').system('true')",
        "a.__class__",
        "open('/etc/passwd')",
        "(lambda: 1)()",
        "[x for x in (1, 2)]",
    ):
        with pytest.raises(ValueError):
            expressions.evaluate(hostile, lookup)


def test_variables_drive_the_scene():
    s = MagpylibStudioSession()
    assert s.set_variable("gap", 2.0) == {"ok": True}
    assert s.set_variable("twice", "=gap * 2") == {"ok": True}
    s.add_object("m", "magnet.Sphere",
                 {"polarization": [0, 0, 1], "diameter": 1,
                  "position": [0, 0, "=twice"]})
    assert list(s._objs["m"].position) == [0, 0, 4.0]
    assert s.set_variable("gap", 3.0) == {"ok": True}
    assert list(s._objs["m"].position) == [0, 0, 6.0]

    assert [v["value"] for v in s.get_variables()["variables"]] == [3.0, 6.0]
    assert s.set_variable("twice", "=twice + 1")["ok"] is False  # self-reference
    assert s.set_variable("pi", 3)["ok"] is False  # built-in name
    assert s.remove_variable("nope")["error"] == "unknown variable 'nope'"
    # removing a variable something still uses is rejected, and says why —
    # not the rollback's "unknown variable", which reads like it never existed
    used = s.remove_variable("twice")
    assert used["ok"] is False and "still used by the scene" in used["error"]
    assert list(s._objs["m"].position) == [0, 0, 6.0]
    # and one nothing refers to goes cleanly
    assert s.set_variable("spare", 1) == {"ok": True}
    assert s.remove_variable("spare") == {"ok": True}


def test_editors_see_expressions_as_written_not_only_resolved():
    """What the inspector needs: a field showing only the resolved number
    would replace the expression the moment the user touched a neighbour."""
    s = MagpylibStudioSession()
    s.set_variable("gap", 2)
    s.add_object("m", "magnet.Sphere",
                 {"polarization": [0, 0, 1], "diameter": "=gap/4",
                  "position": [0, 0, "=gap"]})

    diameter = next(p for p in s.get_params("m") if p["name"] == "diameter")
    assert diameter["value"] == 0.5 and diameter["written"] == "=gap / 4"
    polarization = next(p for p in s.get_params("m") if p["name"] == "polarization")
    assert "written" not in polarization  # plain numbers stay plain

    # position came from the constructor, so the transform editor reads it there
    assert s.get_transform("m")["written_position"] == [0, 0, "=gap"]

    # setting a pose symbolically keeps it live rather than freezing the value
    assert s.set_transform("m", position=[5, "=gap*3", 0]) == {"ok": True}
    transform = s.get_transform("m")
    assert transform["position"] == [5, 6, 0]
    assert transform["written_position"] == [5, "=gap * 3", 0]
    assert s.set_variable("gap", 10) == {"ok": True}
    assert s.get_transform("m")["position"] == [5, 30, 0]  # x stayed, y followed

    # a generated copy has no spec, and asking for its params must not raise
    s.add_object("ring", "Collection")
    s.add_object("c", "magnet.Sphere", {"polarization": [0, 0, 1], "diameter": 1},
                 parent="ring")
    assert s.duplicate_around("c", 3) == {"ok": True}
    assert [p["name"] for p in s.get_params("c#1")] == [
        p["name"] for p in s.get_params("c")
    ]
    assert "written_position" not in s.get_transform("c#1")


def test_sweep_reads_the_field_and_leaves_the_scene_where_it_found_it():
    import numpy as np

    s = MagpylibStudioSession()
    s.set_variable("gap", 0.01)
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [0, 0, 1], "dimension": [0.01, 0.01, 0.01]})
    s.add_object("sens", "Sensor", {"position": [0, 0, "=gap"]})
    steps_before = len(s.get_history()["undo"])

    res = s.sweep("gap", [0.01, 0.02, 0.04])
    assert res["ok"] is True and len(res["steps"]) == 3
    field = [step["magnitude"][0] for step in res["steps"]]
    assert field[0] > field[1] > field[2]  # falls off with distance
    assert np.isclose(field[1] / field[2], 8, rtol=0.15)  # ~1/r³ per doubling

    assert s.doc["variables"]["gap"] == 0.01  # restored
    assert list(s._objs["sens"].position) == [0, 0, 0.01]
    assert len(s.get_history()["undo"]) == steps_before  # not an edit
    assert s.sweep("nope", [1])["ok"] is False

    fig = s.get_sweep_figure("gap", [0.01, 0.02])
    assert fig["data"][0]["x"] == [0.01, 0.02]
    assert "gap" in fig["layout"]["title"]["text"]


def test_duplicate_around_keeps_an_arrangement_parametric(tmp_path):
    import numpy as np

    s = MagpylibStudioSession()
    s.set_variable("n", 8)
    s.add_object("ring", "Collection")
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [1, 0, 0], "dimension": [1, 1, 1],
                  "position": [2.3, 0, 0]}, parent="ring")
    assert s.duplicate_around("m", "=n", "z", anchor=[0, 0, 0],
                              spin="=360/n") == {"ok": True}

    # one object and one event stand for the whole ring
    assert len(s._spec("ring")["children"]) == 1
    assert len(s._leaf_sources()) == 8
    listed = s.list_objects()
    assert [o["id"] for o in listed if o.get("derived")] == [f"m#{i}" for i in range(1, 8)]
    third = s._objs["m#2"]
    a = np.deg2rad(2 * 360 / 8)
    assert np.allclose(third.position, [2.3 * np.cos(a), 2.3 * np.sin(a), 0])

    # the count is a number, not twenty objects to keep in step
    assert s.set_variable("n", 12) == {"ok": True}
    assert len(s._leaf_sources()) == 12

    # and it survives the script, as plain runnable magpylib
    path = tmp_path / "ring.py"
    before = json.dumps(s.to_dict())
    script = s.to_script()
    assert "for i in range(1, n):" in script and ".copy()" in script
    path.write_text(script, encoding="utf-8")
    res = s.apply_script(str(path))
    assert res["mode"] == "parsed"
    assert json.dumps(s.to_dict()) == before
    ns = exec_script(script)  # the loop is real magpylib, runnable outside
    assert len(ns["ring"].children) == 12


def test_duplicate_around_needs_a_group():
    s = MagpylibStudioSession(make_scene())
    res = s.duplicate_around("cube", 4)
    assert res["ok"] is False and "Collection" in res["error"]
    assert len(s._leaf_sources()) == 2


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
