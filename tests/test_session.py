"""Tests for the headless engine and its JSON-RPC transport."""

import io
import json
import os
import tempfile

import pytest

from magpylib_studio.rpc import serve
from magpylib_studio.session import DOC_VERSION, MagpylibStudioSession

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


def test_inspector_offers_only_planes_the_engine_knows():
    """The Inspector's mirror dropdown is a hardcoded list in a webview, and
    the engine owns the real one. When they drifted, the panel offered "zx"
    and picking it returned KeyError: 'zx' — a menu entry that cannot work.
    Checked from the side that has the truth."""
    import pathlib
    import re

    from magpylib_studio.session import _MIRROR_NORMALS

    source = pathlib.Path(__file__).parent.parent / "vscode-extension/media/inspector.js"
    if not source.exists():  # engine installed without the extension beside it
        pytest.skip("extension source not present")
    listed = re.search(r"plane: \[([^\]]*)\]", source.read_text()).group(1)
    assert sorted(re.findall(r"'(\w+)'", listed)) == sorted(_MIRROR_NORMALS)


def test_a_pattern_adds_its_copies_to_their_group_in_one_call():
    """`Collection.add` rebuilds the collection's source and sensor lists on
    every call, so adding n children one at a time is quadratic — measured at
    400 ms against 1 ms for 2000 of them. The engine does it once per rebuild
    and the exported script once per run, and a pattern's count is a slider.

    Asserted on the shape rather than the clock, because a timing test on a
    shared runner is a coin toss.
    """
    s = MagpylibStudioSession()
    s.load_example("halbach")
    script = s.to_script()

    for line in script.splitlines():
        assert not line.startswith("    ") or ".add(" not in line, (
            f"a pattern still adds one copy at a time: {line.strip()}"
        )
    assert script.count("_copies = []") == 2       # one per ring
    assert script.count(".add(*_copies)") == 2

    # and the engine does the same: one add call for the whole batch
    import magpylib as magpy

    calls = []
    original = magpy.Collection.add

    def counting_add(self, *children, **kwargs):
        calls.append(len(children))
        return original(self, *children, **kwargs)

    magpy.Collection.add = counting_add
    try:
        rebuilt = MagpylibStudioSession()
        rebuilt.load_example("halbach")
    finally:
        magpy.Collection.add = original
    assert max(calls) > 1, "the engine added the copies one at a time"


def test_a_script_that_adds_each_copy_separately_still_reads():
    """The shape to_script emitted before the copies were batched. Scripts
    outlive the version that wrote them, and this one is still perfectly
    good magpylib."""
    source = """import magpylib as magpy

n = 4

ring = magpy.Collection(style={'label': 'Ring'})
m = magpy.magnet.Cuboid(dimension=(1, 1, 1), polarization=(1, 0, 0), position=(2, 0, 0))
ring.add(m)

for i in range(1, n):
    _copy = m.copy()
    _copy.rotate_from_angax(i * 360 / (n), 'z', anchor=(0, 0, 0))
    ring.add(_copy)

magpy.show(ring, backend='plotly')
"""
    s = MagpylibStudioSession()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "old.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        result = s.apply_script(path)

    assert result["ok"] and result["mode"] == "parsed"
    # read as a pattern, not flattened into four declared magnets
    assert [e["op"] for e in s.to_dict()["events"] if e["op"].startswith("dup")] == [
        "duplicate_around"
    ]
    assert len(list(s.scene.sources_all)) == 4


def test_a_variable_can_be_a_choice_rather_than_a_quantity():
    """Not everything a scene is written in terms of sits on a scale.

    A rotation axis is `"z"` — a name, not a small number — so min/max says
    nothing about it and a slider cannot offer it. `options` is the bound that
    fits: enforced like the numeric ones, wherever the value came from, and
    what tells a UI to draw a dropdown instead.
    """
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example("halbach")
    assert s.to_dict()["variable_bounds"]["tilt_axis"] == {"options": ["x", "y", "z"]}

    def where():
        return np.round(s._objs["r1"].position, 3)

    s.set_variable("tilt", 90)
    assert list(where()) == [0, 2.3, 0]          # about z, the default
    assert s.set_variable("tilt_axis", "y")["ok"]
    assert list(where()) == [0, 0, -2.3]         # the same tilt, a different axis

    refused = s.set_variable("tilt_axis", "w")
    assert not refused["ok"] and "not one of its options" in refused["error"]
    assert list(where()) == [0, 0, -2.3], "a refused value changed the scene"

    # and the axis survives being written out and read back as source
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "scene.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(s.to_script())
        with open(path, encoding="utf-8") as f:
            assert "tilt_axis = 'y'" in f.read()
        before = json.dumps(s.to_dict())
        assert s.apply_script(path)["mode"] == "parsed"
        assert json.dumps(s.to_dict()) == before


def test_the_variables_panel_uses_every_bound_the_engine_can_write():
    """A limit the engine records and the panel ignores is invisible: the
    value is refused and nothing on screen ever said it would be.

    `options` was exactly that shape of gap before it had a dropdown, so this
    reads the engine's own signature rather than a list someone has to
    remember to extend. (Same trick as the inspector's plane list.)
    """
    import inspect
    import pathlib

    panel = (pathlib.Path(__file__).parent.parent
             / "vscode-extension" / "media" / "variables.js")
    if not panel.exists():  # engine installed on its own, no extension beside it
        pytest.skip("extension sources not present")
    source = panel.read_text()

    limits = [
        name for name in
        inspect.signature(MagpylibStudioSession.set_variable_bounds).parameters
        if name not in ("self", "name")
    ]
    assert limits, "no bounds to check"
    unused = [limit for limit in limits if f"b.{limit}" not in source]
    assert not unused, f"the variables panel ignores {unused}"
    s = MagpylibStudioSession()
    s.set_variable("plane", "xy")
    assert not s.set_variable_bounds("plane", options=[])["ok"]
    assert not s.set_variable_bounds("plane", options=["xy", "xy"])["ok"]
    assert not s.set_variable_bounds("plane", options=[["xy"]])["ok"]
    assert s.set_variable_bounds("plane", options=["xy", "xz", "yz"])["ok"]

    # A name where a number is expected used to surface as a raw TypeError
    # from the comparison deep inside the build.
    s.set_variable("n", 4)
    s.set_variable_bounds("n", 1, 10)
    failed = s.set_variable("n", "z")
    assert not failed["ok"]
    assert "limited as a number" in failed["error"], failed["error"]


def test_hiding_an_object_survives_editing_the_script():
    """`visible` is editor state with no magpylib spelling, exactly like the
    slider bounds beside it — and it used to be the one piece of it that a
    script edit silently threw away."""
    s = MagpylibStudioSession()
    s.load_example("halbach")
    s.set_visible("r2", False)
    assert any(o.get("visible") is False for o in s.list_objects())

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "scene.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(s.to_script().replace("radius = 2.3", "radius = 2.6"))
        assert s.apply_script(path)["ok"]

    assert s.to_dict()["variables"]["radius"] == 2.6, "the edit did not apply"
    hidden = {o["id"] for o in s.list_objects() if o.get("visible") is False}
    assert "r2" in hidden, "the hidden magnet came back visible"


def test_the_script_builds_the_scene_whatever_order_it_was_built_in():
    """`to_script` folds the log; it does not hoist every definition above
    every step.

    Where an object is created *relative to the steps around it* is part of
    the scene. Add a magnet to a group that has already been patterned and it
    belongs to that group alone — but a script that defines it first builds it
    into every generated copy as well. That was 13 sources in the scene and 15
    in its own script, with a field to match, and no surface said so.

    Each of these interleaves creation with patterning differently; all of
    them have to survive the round trip through the script.
    """
    import numpy as np

    def sphere(session, name, parent):
        return session.add_object(
            name, "magnet.Sphere", parent=parent,
            params={"diameter": 0.4, "polarization": [0, 0, 1],
                    "position": [0, 0, 1]})

    def after_patterning(s):
        s.load_example("array")
        sphere(s, "extra", "row")          # into a group already duplicated

    def between_two_patterns(s):
        s.add_object("box", "Collection")
        s.add_object("a", "magnet.Cuboid", parent="box",
                     params={"dimension": [1, 1, 1], "polarization": [1, 0, 0]})
        s.duplicate_along("a", count=3, step=[2, 0, 0])
        sphere(s, "b", "box")              # after the first pattern
        s.duplicate_along("b", count=2, step=[0, 2, 0])

    def removed_after_patterning(s):
        s.load_example("array")
        sphere(s, "extra", "row")
        s.remove_object("tile")

    for build in (after_patterning, between_two_patterns, removed_after_patterning):
        s = MagpylibStudioSession()
        build(s)
        here = len(list(s.scene.sources_all))
        assert here, build.__name__

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scene.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(s.to_script())
            rebuilt = MagpylibStudioSession()
            result = rebuilt.apply_script(path)

        assert result["ok"], f"{build.__name__}: {result.get('error')}"
        # parsed, not executed: the ordering has to survive as *source*
        assert result["mode"] == "parsed", build.__name__
        there = len(list(rebuilt.scene.sources_all))
        assert here == there, (
            f"{build.__name__}: {here} sources in the scene, {there} in its script"
        )
        assert np.allclose(
            s.get_field(points=[[0.7, 0.4, 1.3]])["values"],
            rebuilt.get_field(points=[[0.7, 0.4, 1.3]])["values"],
        ), f"{build.__name__}: same count, different field"


def test_removing_a_patterned_object_leaves_nothing_standing_in_the_field():
    """The strong form: after a removal, the scene the engine holds and the
    scene its script rebuilds have to be the same scene.

    Counting ids cannot see this. Patterning a *group* copies everything
    inside it, and those copied descendants are magpylib objects with no id —
    so deleting one magnet from a 4x3 array left eight copies of it standing:
    absent from the tree, absent from the script, and summed into every field.
    The only check that notices is one that weighs the live scene against what
    the exported script actually builds.
    """
    import numpy as np

    for name in ("array", "halbach", "coil", "pair"):
        s = MagpylibStudioSession()
        s.load_example(name)
        victim = next(o["id"] for o in s.list_objects()
                      if not o.get("derived") and o["type"] != "Collection"
                      and "Sensor" not in o["type"])
        s.remove_object(victim)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scene.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(s.to_script())
            rebuilt = MagpylibStudioSession()
            assert rebuilt.apply_script(path)["ok"], name

        live = len(list(s.scene.sources_all))
        assert live == len(list(rebuilt.scene.sources_all)), (
            f"{name}: {live} sources in the scene, "
            f"{len(list(rebuilt.scene.sources_all))} in its script"
        )
        if live:  # and they are the same sources, not merely as many
            here = s.get_field(points=[[0.7, 0.3, 0.9]])["values"]
            there = rebuilt.get_field(points=[[0.7, 0.3, 0.9]])["values"]
            assert np.allclose(here, there), f"{name}: the field differs"


def test_removing_an_object_takes_its_generated_copies_with_it():
    """A pattern's copies are part of the object they came from.

    Left behind they are invisible — nothing lists a copy once its source is
    gone — while still standing in the scene and contributing to every field
    it computes: nine magnets you cannot see, select or delete.
    """
    from magpylib_studio.session import EXAMPLES

    for name in EXAMPLES:
        s = MagpylibStudioSession()
        s.load_example(name)
        victim = next(o["id"] for o in s.list_objects()
                      if not o.get("derived") and o["type"] != "Collection")
        s.remove_object(victim)

        listed = {o["id"] for o in s.list_objects()}
        known = {id(o): k for k, o in s._objs.items()}
        standing = {known[id(o)] for o in s.scene.sources_all if id(o) in known}
        assert not standing - listed, f"{name}: ghosts {sorted(standing - listed)}"

        # and what is exported still runs: an object that was removed leaves
        # no definition, so a step naming it would be a NameError
        exec_script(s.to_script())


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
    assert parents["r1"] == "ring1"
    assert parents["sensor"] is None
    assert len(s.get_figure()["data"]) > 0
    # the example round-trips through the generated script
    ns = exec_script(s.to_script())
    assert ns["sensor"].position.shape == (25, 3)  # path along the bore axis
    assert len(ns["halbach"].children) == 2
    assert len(ns["ring1"].children) == 10
    # ring 2 is staggered by an 18 deg group rotation
    assert ns["r2"].position.round(3).tolist() != [2.3, 0, 1.5]
    # the script carries the variables, not the numbers they resolve to
    assert "radius = 2.3" in s.to_script()
    assert "position=(radius, 0, 0.0)" in s.to_script()


def test_every_example_builds_and_is_worth_opening():
    """Four scenes, each leaning on a different feature — an example is the
    shortest documentation there is, so each has to show something."""
    import numpy as np

    from magpylib_studio.session import EXAMPLES

    s = MagpylibStudioSession()
    assert [e["name"] for e in s.list_examples()["examples"]] == list(EXAMPLES)
    assert s.load_example("nope")["ok"] is False

    shown = set()
    for name in EXAMPLES:
        assert s.load_example(name) == {"ok": True}, name
        assert s.list_objects(), name
        assert np.abs(np.array(s.get_field()["values"])).max() > 0, name
        assert s.get_variables()["variables"], name  # all of them parametric
        shown.update(e["op"] for e in s.get_events()["events"])
        json.dumps(s.to_dict())
        exec_script(s.to_script())  # and each exports as runnable magpylib

    # between them the examples show every way of generating objects, which
    # is the reason for having four rather than one
    assert {"duplicate_around", "duplicate_along", "mirror"} <= shown

    # the counts are what a variable changes, not what the document declares
    s.load_example("coil")
    assert len(s._leaf_sources()) == 12
    assert s.set_variable("turns", 30) == {"ok": True}
    assert len(s._leaf_sources()) == 30

    s.load_example("array")
    assert len(s._leaf_sources()) == 12  # nx * ny
    assert s.set_variable("nx", 6) == {"ok": True}
    assert len(s._leaf_sources()) == 18

    # a pose that is a path, and a sensor drawing its own reading
    s.load_example("quiver")
    assert np.array(s._objs["magnet"].position).shape == (51, 3)
    assert s.get_values("field")["set"]["pixel.field.symbol"] == "arrow3d"
    assert len(s.get_figure(animation=True)["frames"]) == 51
    # the step says what it does rather than listing 51 angles
    assert s.get_events()["events"][-1]["label"] == (
        "spin through 51 steps about y, to 360.0°"
    )
    assert s.set_variable("lift", 3.0) == {"ok": True}
    assert list(s._objs["field"].position) == [0, 0, 3]  # all 144 arrows move

    # even a table of numbers is parametric: a pixel grid's resolution cannot
    # be (an expression yields a number, not an array of another length) but
    # every coordinate in it can
    s.load_example("pixels")
    grid = np.array(s._objs["probe"].pixel)
    assert grid.shape == (7, 7, 3)
    assert np.allclose(grid[0, 0], [-2, -2, 0])
    assert s.set_variable("span", 8.0) == {"ok": True}
    assert np.allclose(np.array(s._objs["probe"].pixel)[0, 0], [-4, -4, 0])
    assert len(s.get_field_map(sensor_id="probe")["data"][0]["z"]) == 7


def test_pattern_copies_are_named_after_their_source():
    """A copy is an instance of its source, and has to say so.

    magpylib's copy() increments a trailing number in the label, so every
    copy of "Magnet 1" comes back as "Magnet 2" — and since a pattern makes
    all of them from the same source, a ring of ten reads as one "Magnet 1"
    and nine identical "Magnet 2"s, a name that belongs to a different magnet
    in the same scene. They are numbered after their id instead.
    """
    s = MagpylibStudioSession()
    s.load_example("halbach")
    labels = {o["id"]: o["label"] for o in s.list_objects()}
    assert labels["r1"] == "Magnet 1"
    assert [labels[f"r1#{i}"] for i in (1, 5, 9)] == [
        "Magnet 1 #1", "Magnet 1 #5", "Magnet 1 #9",
    ]
    assert labels["r2"] == "Magnet 2"  # not shadowed by ring 1's copies
    assert len({*labels.values()}) == len(labels)  # every row named once

    # the exported script builds the same names, so the legend of a show()
    # agrees with the tree it came from
    ring = exec_script(s.to_script())["ring1"]
    assert [c.style.label for c in ring.children][:3] == [
        "Magnet 1", "Magnet 1 #1", "Magnet 1 #2",
    ]

    # mirrors too, through the helper the script carries
    s.load_example("pair")
    assert {o["label"] for o in s.list_objects()} >= {"Upper", "Upper #1"}
    mirrored = exec_script(s.to_script())["pair"].children_all
    assert {c.style.label for c in mirrored} >= {"Upper", "Upper #1"}


def test_example_arrives_parametric():
    """The example is the first thing anyone opens, so it is the place to
    find out that a scene can be driven by a variable."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    variables = {v["name"]: v for v in s.get_variables()["variables"]}
    assert variables["radius"]["value"] == 2.3
    assert variables["gap"]["value"] == 1.5
    # bounded, so both arrive with a working slider
    assert variables["radius"]["bounds"]["soft_min"] == 1.6  # 2πr = 10 unit cubes
    assert variables["gap"]["bounds"]["min"] == 0

    # one number moves twenty magnets, in both rings
    assert s.set_variable("radius", 3.5) == {"ok": True}
    assert np.allclose(s._objs["r1"].position, [3.5, 0, 0])
    assert np.linalg.norm(s._objs["r2#4"].position[:2]).round(6) == 3.5
    # and the rings stay where they belong on z
    assert s.set_variable("gap", 3) == {"ok": True}
    assert s._objs["r2"].position[2] == 3
    assert s._objs["r1"].position[2] == 0

    refused = s.set_variable("radius", 9)
    assert refused["ok"] is False and "above its maximum" in refused["error"]
    assert np.allclose(s._objs["r1"].position, [3.5, 0, 0])  # unmoved
    assert len(s.get_figure()["data"]) > 0  # still renders


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

    # An unplaced copy belongs beside its source. For a patterned object that
    # is not a nicety: its copied pattern step needs a group to add to, so
    # landing at the root made copying it fail outright.
    s = MagpylibStudioSession()
    s.load_example("halbach")
    copied = s.copy_object("r1")
    assert copied["ok"] is True, copied.get("error")
    rows = {o["id"]: o for o in s.list_objects()}
    assert rows[copied["id"]]["parent"] == "ring1"  # beside r1, not at the root
    assert sum(1 for o in rows.values() if o.get("derived") == copied["id"]) == 9
    exec_script(s.to_script())
    # naming a destination still overrides that, root included
    assert s.copy_object("sensor", parent=None)["ok"] is True

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
    child = np.array(s._objs["r1"].position)

    assert s.move("ring1", [0, 0, 5]) == {"ok": True}
    assert np.allclose(s._objs["ring1"].position, [0, 0, 5])
    # `child` is a numpy array, so this is element-wise addition rather than
    # list concatenation. Unpacking it (RUF005) would build a six-element
    # list and quietly change what the test asserts.
    assert np.allclose(s._objs["r1"].position, child + [0, 0, 5])  # noqa: RUF005

    assert s.rotate("ring1", 90, "z", anchor=0) == {"ok": True}
    assert np.allclose(s._objs["r1"].position, [0, 2.3, 5])

    # a transform on the outer stack moves the nested rings too
    assert s.move("halbach", [10, 0, 0]) == {"ok": True}
    assert np.allclose(s._objs["r1"].position, [10, 2.3, 5])

    ns = exec_script(s.to_script())
    assert np.allclose(ns["r1"].position, s._objs["r1"].position)
    json.dumps(s.to_dict())  # recorded ops stay JSON-safe


def test_reparenting_a_collection_keeps_its_subtree():
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    kids = np.array([child.position for child in s._objs["ring2"].children])
    assert len(kids) == 10  # the declared magnet plus its generated copies
    assert s.move_object("ring2", None) == {"ok": True}  # out of "halbach"
    assert {o["id"]: o["parent"] for o in s.list_objects()}["ring2"] is None
    assert np.allclose(
        kids, [child.position for child in s._objs["ring2"].children]
    )


def test_transform_respects_parent_frame():
    """A transform inside a rotated Collection stays in world coordinates."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()  # ring2 carries an 18 deg group rotation
    assert s.set_transform("r2", position=[5, 0, 0]) == {"ok": True}
    assert np.allclose(s._objs["r2"].position, [5, 0, 0])
    assert s.move("r2", [0, 0, 1]) == {"ok": True}
    assert np.allclose(s._objs["r2"].position, [5, 0, 1])


def test_move_preserves_world_pose():
    """Reparenting must not teleport: a Collection's group rotation would
    otherwise be applied on top of already-transformed coordinates."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()  # ring2 carries an 18 deg group rotation
    pos = np.array(s._objs["r1"].position)
    rot = s._objs["r1"].orientation.as_matrix()

    assert s.move_object("r1", "ring2") == {"ok": True}
    assert {o["id"]: o["parent"] for o in s.list_objects()}["r1"] == "ring2"
    assert np.allclose(s._objs["r1"].position, pos)
    assert np.allclose(s._objs["r1"].orientation.as_matrix(), rot)

    assert s.move_object("r1") == {"ok": True}  # back out to the root
    assert np.allclose(s._objs["r1"].position, pos)
    assert np.allclose(s._objs["r1"].orientation.as_matrix(), rot)

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
        s.get_field(sensor_id="r1")
    with pytest.raises(ValueError, match=r"\['B', 'H', 'J', 'M'\]"):
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

    assert s.set_pixel_grid("r1", plane="xy")["ok"] is False  # not a sensor
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


def test_the_log_alone_reconstructs_the_scene():
    """The point of the whole thing: `objects` is a projection, so throwing it
    away loses nothing. Creation, removal and reparenting are events, not
    edits to a tree that the events then annotate."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    s.set_variable("radius", 3.0)
    s.rotate("ring1", 30, "z", anchor=[0, 0, 0])
    s.remove_object("ring2")
    s.move_object("r1", "halbach")
    document = json.loads(json.dumps(s.to_dict()))
    field = np.array(s.get_field("sensor")["values"])

    log_only = {k: v for k, v in document.items() if k != "objects"}
    assert set(log_only) == {"version", "generator", "variables",
                             "variable_bounds", "events"}
    rebuilt = MagpylibStudioSession(log_only)

    assert [(o["id"], o["parent"]) for o in rebuilt.list_objects()] == [
        (o["id"], o["parent"]) for o in s.list_objects()
    ]
    assert np.allclose(np.array(rebuilt.get_field("sensor")["values"]), field)
    assert json.dumps(rebuilt.to_dict()) == json.dumps(document)


def test_a_saved_document_says_what_wrote_it():
    """A file on disk outlives the program that wrote it, so it has to carry
    its own format version — the only way a later engine can tell "old" from
    "broken" without guessing from the shape."""
    s = MagpylibStudioSession()
    s.load_example()
    doc = s.to_dict()

    assert doc["version"] == DOC_VERSION
    assert doc["generator"].startswith("magpylib-studio ")
    # and it reads first, so `head -2` on a scene file identifies it
    assert list(doc)[:2] == ["version", "generator"]


def test_a_document_from_before_versions_still_opens():
    """Every scene saved so far has no version field at all. Absent means
    "the first format", not "invalid" — otherwise the field would break the
    files it exists to protect."""
    s = MagpylibStudioSession()
    assert s.load_scene({
        "objects": [{"id": "c", "type": "magnet.Cuboid",
                     "params": {"dimension": [1, 1, 1],
                                "polarization": [1, 0, 0]}}],
    })["ok"]
    assert [o["id"] for o in s.list_objects()] == ["c"]
    assert s.to_dict()["version"] == DOC_VERSION  # migrated, and stamped as such


def test_a_document_from_the_future_is_refused_not_half_read():
    """The failure mode a version field exists to prevent: opening a document
    whose semantics we do not know, dropping the parts we did not understand,
    and writing the wreckage back over the original."""
    s = MagpylibStudioSession()
    s.load_example()
    before = json.dumps(s.to_dict())

    result = s.load_scene({"version": DOC_VERSION + 1, "objects": []})
    assert not result["ok"]
    assert "newer magpylib-studio" in result["error"]
    assert str(DOC_VERSION + 1) in result["error"]
    assert json.dumps(s.to_dict()) == before  # the open scene is untouched


def test_a_document_keeps_what_this_engine_does_not_understand():
    """Forward compatibility, in the only form a JSON document can have it: a
    key we do not know is carried, not dropped. Without this, opening a v2
    scene in a v1 studio and saving it would silently delete whatever v2
    added — which is exactly what the version check refuses to risk, and this
    is what makes the *lower* half of that promise keepable.

    All three places, because they fail differently: top-level and events are
    stored verbatim, while `objects` is a projection that is regenerated at
    every build, so anything on it has to be moved somewhere durable.
    """
    s = MagpylibStudioSession()
    s.load_example()
    doc = json.loads(json.dumps(s.to_dict()))
    doc["units"] = {"length": "mm"}  # something a later version might add
    doc["events"][0]["note"] = "on an event"
    doc["objects"][0]["material"] = "N52"

    reopened = MagpylibStudioSession()
    assert reopened.load_scene(doc)["ok"]
    out = reopened.to_dict()
    assert out["units"] == {"length": "mm"}
    assert out["events"][0]["note"] == "on an event"
    assert out["objects"][0]["material"] == "N52"

    # and again, so event -> projection -> event does not drift on each open
    again = MagpylibStudioSession()
    assert again.load_scene(json.loads(json.dumps(out)))["ok"]
    assert json.dumps(again.to_dict()) == json.dumps(out)


def test_listing_objects_shows_how_the_scene_is_written():
    """The first thing anything asks about a scene is what is in it, and the
    answer has to carry the scene's *scale* and its parameters — not just ids
    and labels.

    From a real failure: asked to add a third ring to the halbach example, a
    chat model listed the objects, learned only that there were two rings,
    and invented a 15 mm magnet at r = 55 mm for a scene whose magnets are 1
    and whose radius is an expression. The numbers it needed were one call
    away and it had no reason to know that. Now they are in the answer it
    already asked for.
    """
    s = MagpylibStudioSession()
    s.load_example("halbach")
    listed = {o["id"]: o for o in s.list_objects()}

    magnet = listed["r1"]
    assert "dimension=(1, 1, 1)" in magnet["source"]      # the scale
    assert "position=(radius, 0, 0.0)" in magnet["source"]  # and the parameter
    assert "magnet.Cuboid" in magnet["source"]

    # a pattern's copies are generated, so there is no line that wrote them
    copies = [o for o in listed.values() if o.get("derived")]
    assert copies, "the halbach example should carry generated copies"
    assert all("source" not in c for c in copies)


def _scene_schema():
    """The schema the extension registers for `*.magpy.json`, from the one
    place it lives. Kept here rather than duplicated so it cannot describe a
    format the engine stopped writing (same trick as the inspector's plane
    list)."""
    import pathlib

    jsonschema = pytest.importorskip("jsonschema")
    path = (pathlib.Path(__file__).parent.parent
            / "vscode-extension" / "schemas" / "magpy-scene.schema.json")
    if not path.exists():  # engine installed on its own, no extension beside it
        pytest.skip("extension sources not present")
    schema = json.loads(path.read_text())
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def test_every_example_validates_against_the_published_schema():
    """The schema is what an editor checks a hand-written scene against, so
    it has to agree with what the engine actually writes. The examples are
    the broadest thing to hold it to: between them they use every op."""
    validator = _scene_schema()
    s = MagpylibStudioSession()
    for example in s.list_examples()["examples"]:
        s.load_example(example["name"])
        errors = [f"{list(e.path)}: {e.message}"
                  for e in validator.iter_errors(s.to_dict())]
        assert not errors, f"{example['name']} does not validate: {errors[:3]}"


def test_the_schema_catches_the_mistakes_it_exists_for():
    """A schema that accepts everything is worse than none, because it is
    believed. Each of these is a real bug shape — `axis: "zx"` shipped twice
    before an enum would have caught it on the way in."""
    validator = _scene_schema()
    s = MagpylibStudioSession()
    s.load_example()
    good = json.loads(json.dumps(s.to_dict()))
    assert not list(validator.iter_errors(good))

    def rejected(mutate):
        doc = json.loads(json.dumps(good))
        mutate(doc)
        return bool(list(validator.iter_errors(doc)))

    def event(doc, op):
        return next(e for e in doc["events"] if e.get("op") == op)

    assert rejected(lambda d: d["events"][0].update(op="teleport"))
    assert rejected(lambda d: event(d, "duplicate_around").update(axis="zx"))
    assert rejected(lambda d: event(d, "create").pop("type"))
    assert rejected(lambda d: d["events"].append(
        {"id": "x", "target": "r1", "op": "move"}))  # no displacement
    assert rejected(lambda d: d["events"].append(
        {"id": "x", "target": "r1", "op": "mirror", "plane": "zx"}))
    assert rejected(lambda d: d["events"].append(
        {"id": "x", "op": "move", "displacement": [1, 0, 0]}))  # no target
    assert rejected(lambda d: d["variables"].update(n="360/x"))  # missing '='
    assert rejected(lambda d: [d.pop("objects"), d.pop("events")])


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
    log = [(e["op"], e["target"]) for e in s.get_events()["events"]]
    # objects come into being before anything can happen to them, parents
    # before children; the transforms then keep the order the per-object
    # build replayed them in, children before parents
    assert log == [
        ("create", "ring"), ("create", "m"),
        ("rotate_from_angax", "m"), ("rotate_from_angax", "ring"),
    ]
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
    # the log opens with the objects coming into being, then what happened
    assert events[0]["source"].startswith("halbach = magpy.Collection(")
    stagger = next(
        e for e in events if e["target"] == "ring2" and e["op"] != "create"
    )
    assert stagger["source"] == "ring2.rotate_from_angax(stagger, 'z', anchor=0)"

    before = np.array(s._objs["r2"].position)
    assert s.edit_event(stagger["id"], {"angle": 45}) == {"ok": True}
    # the whole group followed the edited event, not just the object it names
    moved = np.array(s._objs["r2"].position)
    assert not np.allclose(moved, before)
    assert np.allclose(np.linalg.norm(moved[:2]), np.linalg.norm(before[:2]))
    assert s.get_history()["undo"][-1] == f"edit event {stagger['id']}"
    assert s.undo() == {"ok": True}
    assert np.allclose(s._objs["r2"].position, before)


def test_event_edits_that_cannot_replay_roll_back():
    import numpy as np

    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z", anchor=[0, 0, 0])  # an orbit, so order shows
    s.move("cube", [1, 0, 0])
    events = [e for e in s.get_events()["events"] if e["op"] != "create"]
    assert [e["op"] for e in events] == ["rotate_from_angax", "move"]

    pos = list(s._objs["cube"].position)
    assert s.edit_event(events[0]["id"], {"target": "ghost"})["ok"] is False
    assert s.edit_event(events[0]["id"], {"axis": "banana"})["ok"] is False
    assert list(s._objs["cube"].position) == pos  # log intact, scene intact
    with pytest.raises(KeyError):
        s.edit_event("e99", {"angle": 1})

    # order is semantic: orbit-then-move lands elsewhere than move-then-orbit
    assert np.allclose(pos, [1, 0, 0])
    orbit_at = next(
        e["index"] for e in s.get_events()["events"]
        if e["op"] == "rotate_from_angax"
    )
    assert s.move_event(events[1]["id"], orbit_at) == {"ok": True}
    assert np.allclose(s._objs["cube"].position, [0, 1, 0])

    # but nothing can be dragged above the object it acts on
    refused = s.move_event(events[1]["id"], 0)
    assert refused["ok"] is False and "unknown object 'cube'" in refused["error"]
    assert np.allclose(s._objs["cube"].position, [0, 1, 0])

    assert s.remove_event(events[0]["id"]) == {"ok": True}
    assert [e["op"] for e in s.get_events()["events"] if e["op"] != "create"] == [
        "move"
    ]


def test_editing_history_reports_what_it_broke_instead_of_refusing():
    """Editing an early event usually breaks something later. Refusing for
    that reason would mean history is only editable when nothing depends on
    it, which is not the interesting case — so it applies and says what fell
    over, the way a CAD history flags the items it could not rebuild."""
    s = MagpylibStudioSession()
    s.add_object("ring", "Collection")
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [1, 0, 0], "dimension": [1, 1, 1],
                  "position": [2, 0, 0]}, parent="ring")
    s.rotate("m", 45, "z", anchor=[0, 0, 0])
    s.duplicate_around("m", 4, "z", anchor=[0, 0, 0])
    assert len(s._leaf_sources()) == 4

    create = next(e for e in s.get_events()["events"]
                  if e["op"] == "create" and e["target"] == "m")
    result = s.remove_event(create["id"])
    assert result["ok"] is True
    assert [b["error"] for b in result["broken"]] == [
        "ValueError: targets unknown object 'm'"
    ] * 2

    # the rest of the scene still builds, and the log says which entries did
    # not apply rather than dropping them
    assert [o["id"] for o in s.list_objects()] == ["ring"]
    flagged = [e for e in s.get_events()["events"] if "error" in e]
    assert [e["op"] for e in flagged] == ["rotate_from_angax", "duplicate_around"]

    # a document may hold them, so it must be able to open again
    reopened = MagpylibStudioSession(json.loads(json.dumps(s.to_dict())))
    assert [o["id"] for o in reopened.list_objects()] == ["ring"]
    assert len(reopened.get_events()["events"]) == 3

    assert s.undo() == {"ok": True}
    assert len(s._leaf_sources()) == 4  # and the whole ring is back


def test_events_are_labelled_for_what_they_did():
    """The tree shows these, so they read as steps a person took rather than
    as the call that carried them out — that is `source`, and the script."""
    s = MagpylibStudioSession()
    s.add_object("ring", "Collection")
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [1, 0, 0], "dimension": [1, 1, 1]}, parent="ring")
    s.rotate("m", 45, "z", anchor=[0, 0, 0])  # an anchor makes it an orbit
    s.rotate("m", 90, "z")  # without one it is a spin in place
    s.move("m", [0, 0, 2])
    s.duplicate_around("m", 6, "z", anchor=[0, 0, 0])
    s.move_object("m", None)

    assert [e["label"] for e in s.get_events()["events"]] == [
        "created",
        "created",
        "orbit 45° about z",
        "spin 90° about z",
        "moved by (0, 0, 2) m",
        "6 copies about z",
        "moved to the scene root",
        "placed at (0.0, 0.0, 2.0) m",
        "oriented (0.0, 0.0, 135.0)°",
    ]
    # the call is still there, for the tooltip and the script
    orbit = s.get_events()["events"][2]
    assert orbit["source"] == "m.rotate_from_angax(45, 'z', anchor=(0, 0, 0))"


def test_rollback_shows_the_scene_as_it_stood():
    """A history you can only read is far less use than one you can step
    through and watch build. It is a view, so nothing is written."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    whole = json.dumps(s.to_dict())
    field = np.array(s.get_field("sensor")["values"])

    assert s.set_rollback(2)["ok"] is True
    assert [o["id"] for o in s.list_objects()] == ["halbach", "ring1"]
    listed = s.get_events()["events"]
    assert listed[1].get("pending") is None and listed[2]["pending"] is True
    assert s.get_events()["rollback"] == 2
    assert json.dumps(s.to_dict()) == whole  # a view, not an edit

    assert s.set_rollback(999)["ok"] is False
    assert s.set_rollback()["ok"] is True  # the whole scene again
    assert np.allclose(np.array(s.get_field("sensor")["values"]), field)

    # editing a variable while previewing updates the preview, and stays there
    s.set_rollback(2)
    assert s.set_variable("radius", 2.5) == {"ok": True}
    assert s.get_events()["rollback"] == 2
    assert [o["id"] for o in s.list_objects()] == ["halbach", "ring1"]


def test_edits_while_rolled_back_are_inserted_at_that_step():
    """The other half of the CAD gesture: go back to a step and work from
    there. It is well defined because a rolled-back scene holds only what
    existed then, so nothing inserted can refer to something made later."""
    import numpy as np

    s = MagpylibStudioSession()
    s.add_object("a", "magnet.Sphere", {"polarization": [0, 0, 1], "diameter": 1})
    s.move("a", [10, 0, 0])
    assert [e["label"] for e in s.get_events()["events"]] == [
        "created", "moved by (10, 0, 0) m"
    ]

    # step back to just after the sphere was made, and orbit it there
    assert s.set_rollback(1)["ok"] is True
    result = s.rotate("a", 90, "z", anchor=[0, 0, 0])
    assert result == {"ok": True, "inserted_at": 1}
    assert [e["label"] for e in s.get_events()["events"]] == [
        "created", "orbit 90° about z", "moved by (10, 0, 0) m"
    ]
    # the step advances past what was inserted, so the next edit follows it
    assert s.get_events()["rollback"] == 2
    assert s.move("a", [0, 0, 5])["inserted_at"] == 2

    # and the events after the point still apply once the preview is released
    assert s.set_rollback()["ok"] is True
    assert np.allclose(s._objs["a"].position, [10, 0, 5])
    assert [e["label"] for e in s.get_events()["events"]][-1] == "moved by (10, 0, 0) m"

    # loading a document is not an insertion: it replaces everything
    s.set_rollback(1)
    assert s.load_scene(make_scene())["ok"] is True
    assert s.get_events()["rollback"] is None
    assert len(s.list_objects()) == 2


def test_a_step_can_be_given_a_variable_after_the_fact():
    """Putting a variable on a step that was recorded with a number: the
    scene becomes parametric in something nobody planned for up front."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    stagger = next(e for e in s.get_events()["events"]
                   if e["target"] == "ring2" and e["op"] != "create")
    assert stagger["label"] == "orbit stagger° about z"

    assert s.set_variable("stagger", 18) == {"ok": True}
    assert s.edit_event(stagger["id"], {"angle": "=stagger"}) == {"ok": True}
    relabelled = next(e for e in s.get_events()["events"] if e["id"] == stagger["id"])
    assert relabelled["label"] == "orbit stagger° about z"

    # and the ring now follows it
    before = np.array(s._objs["r2"].position)
    assert s.set_variable("stagger", 45) == {"ok": True}
    assert not np.allclose(s._objs["r2"].position, before)
    assert np.allclose(s._objs["r2"].position[:2], [1.626, 1.626], atol=1e-3)
    # exported as the variable, not as what it currently comes to
    assert "ring2.rotate_from_angax(stagger, 'z', anchor=0)" in s.to_script()


def test_a_create_event_is_where_an_object_is_changed_after_the_fact():
    """"Change the dimensions of that magnet" is an edit to the step that
    made it — the same edit the Inspector makes, reached from the history."""
    import numpy as np

    s = MagpylibStudioSession()
    s.load_example()
    create = next(e for e in s.get_events()["events"]
                  if e["op"] == "create" and e["target"] == "r1")
    stored = next(e for e in s.to_dict()["events"] if e["id"] == create["id"])
    before = len(s.doc["events"])

    assert s.edit_event(create["id"], {
        "params": {**stored["params"], "dimension": [2, 1, 0.5]}
    }) == {"ok": True}
    assert list(s._objs["r1"].dimension) == [2, 1, 0.5]
    # everything recorded after it still applies: the magnet is still orbited
    assert np.allclose(s._objs["r1"].position, [2.3, 0, 0])
    assert len(s.doc["events"]) == before  # an edit, not another entry


def test_placing_an_object_repeatedly_does_not_grow_the_log():
    """Nudging a position field is one act of placing something. A log that
    grew by two entries per nudge would be unreadable, which is the thing it
    most needs not to be."""
    s = MagpylibStudioSession()
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [0, 0, 1], "dimension": [1, 1, 1]})
    s.add_object("n", "magnet.Sphere", {"polarization": [0, 0, 1], "diameter": 1})
    for x in range(4):
        s.set_transform("m", position=[x, 0, 0])
    assert [e["op"] for e in s.doc["events"]] == [
        "create", "create", "position", "orientation"
    ]

    # but once anything else has happened, order matters and it must append
    s.set_transform("n", position=[9, 0, 0])
    s.set_transform("m", position=[7, 0, 0])
    assert [e["op"] for e in s.doc["events"]] == [
        "create", "create", "position", "orientation",
        "position", "orientation", "position", "orientation",
    ]
    assert list(s._objs["m"].position) == [7, 0, 0]
    assert list(s._objs["n"].position) == [9, 0, 0]


def test_ordinary_edits_still_refuse_to_break_things():
    """Only deliberate edits to the log are tolerant. A normal edit that
    would break the scene is still rolled back and reported."""
    s = MagpylibStudioSession(make_scene())
    assert s.add_object("x", "magnet.Nope")["ok"] is False
    assert s.add_object("y", "magnet.Sphere", params={"bogus": 1})["ok"] is False
    assert s.add_object("cube", "magnet.Sphere")["ok"] is False  # duplicate id
    assert [o["id"] for o in s.list_objects()] == ["cube", "cyl"]
    assert not s._broken

    # and an edit to an event that cannot itself replay is rolled back too
    rotate = s.rotate("cube", 30, "z")
    assert rotate == {"ok": True}
    event = next(e for e in s.get_events()["events"] if e["op"] != "create")
    assert s.edit_event(event["id"], {"axis": "sideways"})["ok"] is False
    assert not s._broken
    assert s.get_events()["events"][-1]["source"].endswith("(30, 'z')")


def test_removing_an_object_is_recorded_not_erased():
    """A removal is something that happened, so it goes on the end of the log.
    Rewriting the earlier events out would make the log a different story from
    the one the scene actually went through."""
    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z")
    s.rotate("cyl", 45, "z")
    assert s.remove_object("cube") == {"ok": True}

    assert [o["id"] for o in s.list_objects()] == ["cyl"]  # gone from the scene
    log = [(e["op"], e["target"]) for e in s.get_events()["events"]]
    assert log[-1] == ("remove", "cube")
    assert ("create", "cube") in log and ("rotate_from_angax", "cube") in log

    # replaying the whole log still works: the events before the removal ran
    # while the object existed, and the removal is what ends it
    rebuilt = MagpylibStudioSession(json.loads(json.dumps(s.to_dict())))
    assert [o["id"] for o in rebuilt.list_objects()] == ["cyl"]
    assert s.undo() == {"ok": True}
    assert [o["id"] for o in s.list_objects()] == ["cube", "cyl"]


def test_copying_an_object_copies_its_events():
    import numpy as np

    s = MagpylibStudioSession(make_scene())
    s.rotate("cube", 90, "z", anchor=[0, 0, 0])
    s.move("cube", [0, 0, 2])
    res = s.copy_object("cube")
    assert res["ok"] is True
    # the copy replays the same construction, so it lands on the original
    assert np.allclose(s._objs[res["id"]].position, s._objs["cube"].position)
    copied = [e for e in s.get_events()["events"] if e["target"] == res["id"]]
    assert [e["op"] for e in copied] == ["create", "rotate_from_angax", "move"]


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


def test_expression_help_comes_from_the_rule_it_documents():
    """A UI that lists what expressions allow has to read it off the
    allow-list, or the two drift and the help becomes a lie."""
    from magpylib_studio import expressions

    s = MagpylibStudioSession()
    help_ = s.expression_help()
    assert set(help_["functions"]) == set(expressions._FUNCTIONS)
    assert set(help_["constants"]) == set(expressions._CONSTANTS)
    for example in help_["examples"]:
        assert s.check_expression(example) == {"ok": True}, example

    # said while it is typed, and specific about what went wrong
    assert s.check_expression("gap * 2") == {"ok": True}
    assert s.check_expression("=360 / n") == {"ok": True}  # the marker is fine
    for text, wrong in (
        ("sinh(x)", "not one of the functions"),
        ("a.b", "Attribute is not allowed"),
        ("radius[0]", "Subscript is not allowed"),
        ("2 if a else 3", "IfExp is not allowed"),
        ("gap *", "not an expression"),
    ):
        result = s.check_expression(text)
        assert result["ok"] is False and wrong in result["error"], text

    # a name that does not exist yet is well formed: it gets offered instead
    assert s.check_expression("nothing_yet * 2") == {"ok": True}
    assert s.unknown_variables(["=nothing_yet * 2"])["unknown"] == ["nothing_yet"]


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


def test_variable_bounds_are_hard_or_only_advisory(tmp_path):
    s = MagpylibStudioSession()
    s.set_variable("gap", 2)
    s.add_object("m", "magnet.Sphere",
                 {"polarization": [0, 0, 1], "diameter": 1,
                  "position": [0, 0, "=gap"]})
    assert s.set_variable_bounds("gap", min=0, max=10,
                                 soft_min=1, soft_max=5) == {"ok": True}
    assert s.get_variables()["variables"][0]["bounds"] == {
        "min": 0, "max": 10, "soft_min": 1, "soft_max": 5
    }
    # soft bounds do not constrain: only the slider cares
    assert s.set_variable("gap", 8) == {"ok": True}
    assert list(s._objs["m"].position) == [0, 0, 8]
    # hard bounds do, and the scene is left where it was
    refused = s.set_variable("gap", 12)
    assert refused["ok"] is False and "above its maximum" in refused["error"]
    assert list(s._objs["m"].position) == [0, 0, 8]

    # enforced however the value arrives, including through another variable:
    # gap stays inside its own limits, but quad would leave its own
    s.set_variable("gap", 4)
    s.set_variable("quad", "=gap*4")
    assert s.set_variable_bounds("quad", max=20) == {"ok": True}
    assert s.set_variable("gap", 5)["ok"] is True  # quad = 20, at the limit
    breached = s.set_variable("gap", 6)  # quad would be 24
    assert breached["ok"] is False and "quad = 24" in breached["error"]
    assert list(s._objs["m"].position) == [0, 0, 5]  # scene held at gap = 5

    # nonsense is refused, and limits are editor metadata that survive a save
    assert s.set_variable_bounds("gap", min=5, max=1)["ok"] is False
    assert s.set_variable_bounds("gap", min=0, max=10, soft_max=99)["ok"] is False
    assert s.set_variable_bounds("nope", min=0)["ok"] is False
    path = tmp_path / "scene.py"
    path.write_text(s.to_script(), encoding="utf-8")
    assert s.apply_script(str(path))["mode"] == "parsed"
    assert s.to_dict()["variable_bounds"]["gap"]["max"] == 10

    # and they go when the variable does
    assert s.set_variable_bounds("gap") == {"ok": True}  # cleared
    assert "gap" not in s.to_dict().get("variable_bounds", {})
    assert s.remove_variable("quad") == {"ok": True}
    assert "variable_bounds" not in s.to_dict()


def test_params_say_their_unit_and_component_names():
    """The Inspector should not have to dig a unit out of a doc string, nor
    label a polarization's components 1, 2, 3."""
    s = MagpylibStudioSession()
    s.load_example("quiver")
    params = {p["name"]: p for p in s.get_params("magnet")}
    assert params["polarization"]["unit"] == "T"
    assert params["polarization"]["components"] == ["x", "y", "z"]
    # a dimension's components have no names — they depend on the shape, and
    # its doc says which, so it deliberately carries none
    assert params["dimension"]["unit"] == "m"
    assert "components" not in params["dimension"]
    assert "Cuboid" in params["dimension"]["doc"]

    pixel = next(p for p in s.get_params("field") if p["name"] == "pixel")
    assert pixel["kind"] == "matrix" and pixel["unit"] == "m"


def test_a_pixel_field_source_is_choosable():
    """magpylib's schema says only that a pixel field source exists, not what
    it may be — so the inspector had nothing to build a widget from and the
    property silently did not appear. The engine knows the answer."""
    from magpylib_studio.session import _field_sources

    s = MagpylibStudioSession()
    s.load_example("quiver")
    source = (s.get_schema("field")["properties"]["pixel"]["properties"]
              ["field"]["properties"]["source"])
    assert source["enum"][0] is None  # (default) stays available
    assert source["enum"][1:4] == ["B", "Bx", "By"]
    assert "Jxy" in source["enum"] and "Mxyz" in source["enum"]

    # every offered value is one magpylib actually takes, so a dropdown built
    # from the schema cannot produce a rejected edit
    for value in _field_sources():
        assert s.apply_edit("field", "pixel.field.source", value) == {"ok": True}
    assert s.apply_edit("field", "pixel.field.source", "nope")["ok"] is False


def test_every_field_magpylib_can_evaluate_is_offered():
    """B and H are what a scene is usually read for; J and M are zero outside
    a magnet and constant inside it, which makes them the quick way to see
    what a shape actually covers."""
    import numpy as np

    from magpylib_studio.session import _FIELDS

    s = MagpylibStudioSession()
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [0, 0, 1], "dimension": [1, 1, 1]})
    inside, outside = [[0, 0, 0]], [[2, 0, 0]]
    assert set(_FIELDS) == {"B", "H", "J", "M"}

    units = {f: s.get_field(points=inside, field=f)["unit"] for f in _FIELDS}
    assert units == {"B": "T", "H": "A/m", "J": "T", "M": "A/m"}

    # J is the polarization itself where there is material, and nothing where
    # there is not — the property that makes it worth plotting
    assert np.allclose(s.get_field(points=inside, field="J")["values"], [[0, 0, 1]])
    assert np.allclose(s.get_field(points=outside, field="J")["values"], [[0, 0, 0]])
    assert np.array(s.get_field(points=outside, field="B")["values"]).any()

    # and the same set reaches the map, off a sensor's own grid
    s.load_example("pixels")
    figure = s.get_field_map(sensor_id="probe", field="J")
    assert "|J| (T)" in figure["layout"]["title"]["text"]


def test_a_variable_that_counts_things_stays_whole():
    """A count of 7.3 is not a coarse 7.3, it is meaningless — and the
    patterns that consume one used to truncate it silently."""
    s = MagpylibStudioSession()
    s.load_example("coil")
    turns = {v["name"]: v for v in s.get_variables()["variables"]}["turns"]
    assert turns["bounds"]["integer"] is True
    assert len(s._leaf_sources()) == 12

    assert s.set_variable("turns", 20) == {"ok": True}
    assert len(s._leaf_sources()) == 20
    refused = s.set_variable("turns", 7.3)
    assert refused["ok"] is False and "whole number" in refused["error"]
    assert len(s._leaf_sources()) == 20  # unchanged

    # enforced wherever the value came from, including through an expression
    assert s.set_variable("half", "=turns / 2") == {"ok": True}  # not a count
    assert s.set_variable_bounds("half", integer=True)["ok"] is True
    assert s.set_variable("turns", 21)["ok"] is False  # would make half 10.5
    assert s.set_variable("turns", 22) == {"ok": True}

    # and a pattern refuses a fractional count rather than rounding it
    s2 = MagpylibStudioSession()
    s2.add_object("g", "Collection")
    s2.add_object("m", "magnet.Sphere",
                  {"polarization": [0, 0, 1], "diameter": 1}, parent="g")
    s2.set_variable("k", 4)
    assert s2.duplicate_around("m", "=k", "z", anchor=[0, 0, 0]) == {"ok": True}
    assert len(s2._leaf_sources()) == 4
    broken = s2.set_variable("k", 6.5)
    assert broken["ok"] is False and "whole number" in broken["error"]
    assert len(s2._leaf_sources()) == 4


def test_batch_builds_a_parametric_scene_in_one_step():
    """What an assistant sends for "a Halbach ring of 8": the variables, the
    one magnet written in terms of them, and the arrangement — one undo."""
    s = MagpylibStudioSession()
    result = s.batch([
        {"method": "set_variable", "params": {"name": "n", "value": 8}},
        {"method": "set_variable", "params": {"name": "r", "value": 2.3}},
        {"method": "add_object", "params": {"object_id": "ring",
                                            "type": "Collection"}},
        {"method": "add_object", "params": {
            "object_id": "m", "type": "magnet.Cuboid", "parent": "ring",
            "params": {"polarization": [1, 0, 0], "dimension": [1, 1, 1],
                       "position": ["=r", 0, 0]}}},
        {"method": "duplicate_around", "params": {
            "object_id": "m", "count": "=n", "axis": "z", "anchor": [0, 0, 0],
            "spin": "=360/n"}},
    ])
    assert result["ok"] is True
    assert [r["ok"] for r in result["results"]] == [True] * 5
    assert len(s._leaf_sources()) == 8
    assert s.get_history()["undo"] == ["batch (5 ops)"]
    assert s.undo() == {"ok": True}
    assert s.list_objects() == []


def test_unknown_variables_are_reported_before_a_value_is_stored():
    """Typing `a*2` into a field is a way of saying "and let me set a" — the
    UI asks this what to prompt for, rather than storing and failing."""
    s = MagpylibStudioSession()
    s.set_variable("gap", 1)

    # reading order, deduplicated, nested structures included
    assert s.unknown_variables(["=a", "=a*2", 3, ["=b/gap"]]) == {"unknown": ["a", "b"]}
    assert s.unknown_variables({"dimension": ["=w", "=w", "=2*h"]}) == {
        "unknown": ["w", "h"]
    }
    # what is already defined, what is a function, and what is a constant are
    # none of them things to ask for
    assert s.unknown_variables(["=gap*2", "=sqrt(gap)", "=pi", 5, "z"]) == {
        "unknown": []
    }
    # once asked and set, the same values store cleanly
    assert s.set_variable("a", 2) == {"ok": True}
    assert s.add_object("m", "magnet.Cuboid",
                        {"polarization": [0, 0, 1],
                         "dimension": ["=a", "=a", "=2*a"]}) == {"ok": True}
    assert list(s._objs["m"].dimension) == [2, 2, 4]


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


def test_two_linear_patterns_compose_into_a_grid(tmp_path):
    """The CAD pattern family: `duplicate_along` is the linear one, and a
    rectangular grid is it applied twice — to the object, then to the group
    holding it — so there is no separate grid op to keep in step."""
    s = MagpylibStudioSession()
    s.set_variable("nx", 4)
    s.set_variable("ny", 3)
    s.set_variable("pitch", 2.0)
    s.add_object("grid", "Collection")
    s.add_object("row", "Collection", parent="grid")
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [0, 0, 1], "dimension": [1, 1, 1]}, parent="row")

    assert s.duplicate_along("m", "=nx", ["=pitch", 0, 0]) == {"ok": True}
    assert s.duplicate_along("row", "=ny", [0, "=pitch", 0]) == {"ok": True}
    assert len(s._leaf_sources()) == 12
    assert sorted({round(float(o.position[0]), 1) for o in s._leaf_sources()}) == [
        0.0, 2.0, 4.0, 6.0
    ]
    assert sorted({round(float(o.position[1]), 1) for o in s._leaf_sources()}) == [
        0.0, 2.0, 4.0
    ]

    # two numbers describe the whole array
    assert s.set_variable("nx", 6) == {"ok": True}
    assert s.set_variable("ny", 5) == {"ok": True}
    assert len(s._leaf_sources()) == 30

    assert [e["label"] for e in s.get_events()["events"] if "copies" in e["label"]] == [
        "nx copies every (pitch, 0, 0) m",
        "ny copies every (0, pitch, 0) m",
    ]

    # and it survives the script as plain runnable magpylib
    path = tmp_path / "grid.py"
    before = json.dumps(s.to_dict())
    path.write_text(s.to_script(), encoding="utf-8")
    assert s.apply_script(str(path))["mode"] == "parsed"
    assert json.dumps(s.to_dict()) == before
    assert len(s._leaf_sources()) == 30


def test_mirror_reflects_the_physics_not_just_the_geometry(tmp_path):
    """Polarization is an axial vector, so a mirror keeps its component along
    the normal and reverses the others — the opposite of what the position
    does. B is axial too, which is what makes this checkable."""
    import numpy as np

    s = MagpylibStudioSession()
    s.add_object("asm", "Collection")
    s.add_object("m", "magnet.Cuboid",
                 {"polarization": [0.3, -0.5, 0.8], "dimension": [1, 2, 3],
                  "position": [1.5, -0.8, 2.2]}, parent="asm")
    # created before the transforms, because to_script writes every
    # definition before every step: a scene that makes an object *after*
    # moving another comes back with the creates hoisted
    s.add_object("t", "magnet.Tetrahedron",
                 {"polarization": [0, 0, 1],
                  "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]},
                 parent="asm")
    s.rotate("m", 40, "x")  # a pose with nothing special about it
    assert s.mirror("m", "xy") == {"ok": True}

    normal = np.array([0.0, 0.0, 1.0])
    reflect = np.eye(3) - 2 * np.outer(normal, normal)
    point = np.array([0.7, 1.1, -1.4])
    field = s._objs["m"].getB(point)
    mirrored = s._objs["m#1"].getB(reflect @ point)
    assert np.allclose(mirrored, 2 * np.dot(field, normal) * normal - field)

    # a shape with no mirror symmetry of its own cannot borrow one
    refused = s.mirror("t", "xy")
    assert refused["ok"] is False and "no mirror symmetry" in refused["error"]

    # magpylib has no mirror, so the script carries a helper — and stays
    # parametric, the copy still following whatever the source does
    path = tmp_path / "mirror.py"
    before = json.dumps(s.to_dict())
    script = s.to_script()
    assert "def _mirror(obj, normal, anchor=(0, 0, 0)):" in script
    assert "asm.add(_mirror(m, (0, 0, 1), 0))" in script
    path.write_text(script, encoding="utf-8")
    assert s.apply_script(str(path))["mode"] == "parsed"
    assert json.dumps(s.to_dict()) == before
    assert s.to_script() == script

    ns = exec_script(script)  # and it runs outside the studio
    assert len(ns["asm"].children) == 3


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
