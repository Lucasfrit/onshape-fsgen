"""Local builder for Part Studio scripts must reproduce Onshape's results (volumes measured in Onshape)."""
from pathlib import Path

import pytest

from fsgen.native.local import build_local
from fsgen.native.script import trace

ROOT = Path(__file__).resolve().parent.parent
ONSHAPE_VOLUMES = {  # mm^3, from Onshape massproperties
    "examples/native/plate.fs": 23244.385,
    "examples/native/l_bracket.fs": 24858.988,
    "examples/native/flange.fs": 32356.31,
    "examples/native/enclosure.fs": 30000.836,
    "examples/native/generated/nema17_mount.fs": 19831.865,
    "examples/native/generated/fan_adapter.fs": 43299.793,
}


@pytest.mark.parametrize("path", sorted(ONSHAPE_VOLUMES))
def test_local_matches_onshape(path):
    res = build_local(trace((ROOT / path).read_text()))
    assert res.ok and not res.partial, (res.error, res.warnings)
    m = res.metrics()
    assert m["parts"] == 1
    assert m["volume_mm3"] == pytest.approx(ONSHAPE_VOLUMES[path], rel=1e-5)


def test_empty_selection_is_an_error_like_onshape():
    src = (ROOT / "examples/native/plate.fs").read_text().replace(
        'qParallelEdges(qCreatedBy(id + "Plate", EntityType.EDGE), vector(0, 0, 1))',
        'qParallelEdges(qCreatedBy(id + "Plate", EntityType.EDGE), vector(1, 1, 1))')
    res = build_local(trace(src))
    assert not res.ok and "Corner fillets" in res.error


def test_remove_in_wrong_direction_warns():
    src = (ROOT / "examples/native/plate.fs").read_text().replace(
        '"endBound" : BoundingType.THROUGH_ALL,', '"endBound" : BoundingType.THROUGH_ALL, "oppositeDirection" : true,')
    res = build_local(trace(src))
    assert any("REMOVE did not cut" in w for w in res.warnings)


# Built locally but not yet measured in Onshape (no API calls spent): checked against analytic values instead.
LOCAL_ONLY = ["examples/native/m12_bolt_nut.fs"]


@pytest.mark.parametrize("path", sorted(ONSHAPE_VOLUMES) + LOCAL_ONLY)
def test_paste_export_matches(path):
    from fsgen.native.paste import paste_export
    _, chk = paste_export(trace((ROOT / path).read_text(), "studio"), "Test part")
    assert chk["ok"], chk


# ---------------------------------------------------------------- threads (helix + sweep)
THREAD = """FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{
    var c = newSketch(context, id + "Helix sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(c, "circle", { "center" : vector(0, 0) * millimeter, "radius" : 5 * millimeter });
    skSolve(c);
    helix(context, id + "Helix", { "axisType" : AxisType.CIRCLE,
        "edge" : sketchEntityQuery(id + "Helix sketch", EntityType.EDGE, "circle"),
        "pathType" : PathType.TURNS_PITCH, "revolutions" : TURNS, "helicalPitch" : 2 * millimeter });
    var p = newSketch(context, id + "Profile", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skPolyline(p, "tri", { "points" : [vector(5, 0.2) * millimeter, vector(6, 1) * millimeter,
        vector(5, 1.8) * millimeter, vector(5, 0.2) * millimeter] });
    skSolve(p);
    sweep(context, id + "Thread", { "profiles" : qSketchRegion(id + "Profile"),
        "path" : qCreatedBy(id + "Helix", EntityType.EDGE) });
}
"""


@pytest.mark.parametrize("turns", [0.5, 1, 2.5, 10])
def test_helix_sweep_is_a_screw_motion(turns):
    """A profile swept along a helix is a screw motion: volume = area * centroid radius * swept angle.
    (OCCT's default sweep tolerance gave up to 5 % error here; fixed by SWEEP_TOL.)"""
    import math
    res = build_local(trace(THREAD.replace("TURNS,", f"{turns},")))
    assert res.ok and not res.partial, (res.error, res.warnings)
    area, centroid_r = 1.6 * 1 / 2, 5 + 1 / 3  # mm^2, mm (triangle (5,0.2) (6,1) (5,1.8))
    exact = area * centroid_r * 2 * math.pi * turns
    assert res.metrics()["volume_mm3"] == pytest.approx(exact, rel=1e-5)


def test_helix_is_right_handed_and_starts_at_circle_x():
    res = build_local(trace(THREAD.replace("TURNS,", "0.25,")))
    lo, hi = (res.metrics()["bbox_mm"][:3], res.metrics()["bbox_mm"][3:])
    # a quarter turn of a right-handed (clockwise) helix from +X ends at +Y, 0.5 mm higher
    assert lo[1] == pytest.approx(0, abs=1e-3) and hi[1] == pytest.approx(6, abs=1e-3)
    assert lo[2] == pytest.approx(0.2, abs=1e-3) and hi[2] == pytest.approx(2.3, abs=1e-3)


def test_helix_on_sketch_circle_needs_axis_type_circle():
    src = THREAD.replace('"axisType" : AxisType.CIRCLE,\n        "edge"', '"entities"').replace("TURNS,", "1,")
    res = build_local(trace(src))
    assert not res.ok and "AxisType.CIRCLE" in res.error


def _bolt_and_nut(src=None):
    import numpy as np
    from fsgen.fslite import geom as G
    res = build_local(trace(src or (ROOT / "examples/native/m12_bolt_nut.fs").read_text()))
    assert res.ok and not res.partial, (res.error, res.warnings)
    bolt, nut = sorted(res.run.solids, key=lambda b: G.bbox([b.shape])[0][0])

    def overlap_mm3(dz_mm, turn_deg=0.0):
        """Nut moved onto the bolt axis, raised dz and turned; volume shared with the bolt."""
        a = np.radians(turn_deg)
        m = np.array([[np.cos(a), -np.sin(a), 0, 0], [np.sin(a), np.cos(a), 0, 0], [0, 0, 1, dz_mm / 1000],
                      [0, 0, 0, 1]]) @ np.array([[1, 0, 0, -0.03], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        shared, _ = G.boolean("INTERSECTION", [bolt.shape], [G.transform_shape(nut.shape, m)[0]])
        return sum(G.props(s, "volume").Mass() for s in G.solids_of(shared)) * 1e9
    return overlap_mm3


def test_bolt_and_nut_screw_together():
    overlap = _bolt_and_nut()
    first = 9.5 - 1 + 2  # nut ridges sit in the bolt's grooves: bolt thread start - P/2 + one pitch
    for dz, turn in ((first, 0), (first + 0.5, 90), (first + 6.75, 135), (first + 12, 0)):
        assert overlap(dz, turn) < 1e-3, (dz, turn)  # turning right-handed along the thread: no collision
    assert overlap(first + 1, 0) > 50  # half a pitch out of phase
    assert overlap(first - 0.5, 90) > 50  # turned the wrong way (left-handed)


def test_paste_export_threads_follow_parameters():
    from fsgen.fslite.runner import run_source
    from fsgen.fslite.values import Q
    from fsgen.native.paste import paste_export
    src = (ROOT / "examples/native/m12_bolt_nut.fs").read_text()
    code, _ = paste_export(trace(src, "studio"), "Bolt and nut")
    native = build_local(trace(src.replace('"threadPitch", 2 * millimeter', '"threadPitch", 1.75 * millimeter')
                               .replace('"threadTurns", 12', '"threadTurns", 9')))
    pasted = run_source(code, params={"threadPitch": Q(0.00175, (1, 0)), "threadTurns": 9.0})
    assert pasted.ok, pasted.error
    assert pasted.metrics()["volume_mm3"] == pytest.approx(native.metrics()["volume_mm3"], rel=1e-6)


PIPE = """FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{
    var p = newSketch(context, id + "Path", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skLineSegment(p, "l1", { "start" : vector(0, 0) * millimeter, "end" : vector(0, 20) * millimeter });
    skArc(p, "a1", { "start" : vector(0, 20) * millimeter,
        "mid" : vector(10 - 10 * cos(45 * degree), 20 + 10 * sin(45 * degree)) * millimeter, "end" : vector(10, 30) * millimeter });
    skSolve(p);
    var c = newSketch(context, id + "Circle", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(c, "c", { "center" : vector(0, 0) * millimeter, "radius" : 2 * millimeter });
    skSolve(c);
    sweep(context, id + "Pipe", { "profiles" : qSketchRegion(id + "Circle"),
        "path" : qCreatedBy(id + "Path", EntityType.EDGE) });
}
"""


def test_sweep_along_line_and_arc():
    """Non-helical path (corrected Frenet frame); also a sketch named "Circle" must not shadow skCircle()."""
    import math
    from fsgen.native.paste import paste_export
    res = build_local(trace(PIPE))
    assert res.ok and not res.partial, (res.error, res.warnings)
    assert res.metrics()["volume_mm3"] == pytest.approx(math.pi * 4 * (20 + 10 * math.pi / 2), rel=1e-6)
    _, chk = paste_export(trace(PIPE, "studio"), "Pipe")
    assert chk["ok"], chk


AXIS_HELIX = """FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{
    variable(context, "turns", 3);
    var a = newSketch(context, id + "Axis sketch", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skLineSegment(a, "axis", { "start" : vector(0, 0) * millimeter, "end" : vector(0, 10) * millimeter, "construction" : true });
    skPolyline(a, "tri", { "points" : [vector(5, 0.2) * millimeter, vector(6, 1) * millimeter,
        vector(5, 1.8) * millimeter, vector(5, 0.2) * millimeter] });
    skSolve(a);
    helix(context, id + "Helix", { "axisType" : AxisType.AXIS,
        "axis" : sketchEntityQuery(id + "Axis sketch", EntityType.EDGE, "axis"), "startRadius" : 5 * millimeter,
        "pathType" : PathType.PITCH, "helicalPitch" : 2 * millimeter, "height" : #turns * 2 * millimeter });
    sweep(context, id + "Thread", { "profiles" : qSketchRegion(id + "Axis sketch"),
        "path" : qCreatedBy(id + "Helix", EntityType.EDGE) });
}
"""


def test_helix_around_axis():
    """axisType AXIS: start radius from the axis' first point, start angle from perpendicularVector (+X here)."""
    import math
    from fsgen.native.paste import paste_export
    res = build_local(trace(AXIS_HELIX))
    assert res.ok and not res.partial, (res.error, res.warnings)
    assert res.metrics()["volume_mm3"] == pytest.approx(0.8 * (5 + 1 / 3) * 2 * math.pi * 3, rel=1e-5)
    _, chk = paste_export(trace(AXIS_HELIX, "studio"), "Axis helix")
    assert chk["ok"], chk


LOCAL_ONLY = {  # examples without an Onshape measurement yet: local build + paste export must agree
    "examples/native/generated/neo_screwdriver.fs": 21740.158,
}


@pytest.mark.parametrize("path", sorted(LOCAL_ONLY))
def test_local_only_examples(path):
    from fsgen.native.paste import paste_export
    res = build_local(trace((ROOT / path).read_text()))
    assert res.ok and not res.partial, (res.error, res.warnings)
    assert res.metrics()["volume_mm3"] == pytest.approx(LOCAL_ONLY[path], rel=1e-5)
    _, chk = paste_export(trace((ROOT / path).read_text(), "studio"), "Test part")
    assert chk["ok"], chk
