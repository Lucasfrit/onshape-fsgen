"""Fit check, print check, 3D viewer and the local-builder fixes that came with them (all 0 Onshape calls)."""
import base64
import copy
import json
import re
import struct
import tomllib
from pathlib import Path

import pytest

from fsgen.native.fit import FitError, run_fit
from fsgen.native.local import build_local
from fsgen.native.printcheck import overhangs
from fsgen.native.script import trace

ROOT = Path(__file__).resolve().parent.parent
BOX = ROOT / "examples/native/fit/lidded_box.fs"
SPEC = tomllib.loads((ROOT / "examples/native/fit/lidded_box.fit.toml").read_text())


@pytest.fixture(scope="module")
def box_solids():
    loc = build_local(trace(BOX.read_text()))
    assert loc.ok and not loc.partial, (loc.error, loc.warnings)
    return [b.shape for b in loc.run.solids]


# ---------------------------------------------------------------- fit check
def test_lid_fits_and_motions_behave(box_solids):
    res = run_fit(box_solids, SPEC, BOX.parent)
    assert [p.name for p in res.placed] == ["box", "lid", "card"]
    assert res.clashes == []
    lift, push = res.motions
    assert lift.verdict == "free" and lift.ok
    assert push.verdict == "blocked" and push.ok
    assert push.overlaps[0] == 0          # 0.1 mm sideways stays inside the 0.2 mm clearance
    assert push.overlaps[1] > 1           # 0.5 mm does not
    assert res.ok


def test_overlap_in_assembled_position_is_reported(box_solids):
    spec = copy.deepcopy(SPEC)
    spec["parts"]["lid"]["place"][1]["move"] = [-40, 0, 16]    # 1 mm too low: lip and plate hit the box
    res = run_fit(box_solids, spec, BOX.parent)
    assert [(a, b) for a, b, _ in res.clashes] == [("box", "lid")]
    assert not res.ok


def test_wrong_expectation_fails(box_solids):
    spec = copy.deepcopy(SPEC)
    spec["motions"][1]["expect"] = "free"
    res = run_fit(box_solids, spec, BOX.parent)
    assert not res.motions[1].ok and not res.ok
    assert "EXPECTED FREE" in res.report()


def test_snap_and_collision_verdicts():
    from fsgen.native.fit import _classify
    assert _classify([0.2, 0.7, 0.0, 0.0], 3.0) == "snap"
    assert _classify([0.2, 40.0, 0.0], 3.0) == "collides"
    assert _classify([0.0, 2.0, 9.0], 3.0) == "blocked"
    assert _classify([0.0, 0.0], 3.0) == "free"


def test_mirror_placement_is_rejected(box_solids):
    spec = copy.deepcopy(SPEC)
    spec["parts"]["lid"]["place"] = [{"matrix": [[1, 0, 0], [0, 1, 0], [0, 0, -1]], "offset": [0, 0, 0]}]
    with pytest.raises(FitError, match="cannot be mirrored"):
        run_fit(box_solids, spec, BOX.parent)


def test_point_outside_every_part_lists_the_parts(box_solids):
    spec = copy.deepcopy(SPEC)
    spec["parts"]["lid"]["at"] = [100, 100, 100]
    with pytest.raises(FitError, match="inside 0 built parts.*part 1:"):
        run_fit(box_solids, spec, BOX.parent)


def test_cli_fit_and_view(tmp_path, monkeypatch, capsys):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "fit", str(BOX)])
    assert e.value.code == 0
    assert "fit check passed" in capsys.readouterr().out
    assert (tmp_path / "out/lidded_box/lidded_box_assembled.step").exists()
    with pytest.raises(SystemExit) as e:
        main(["studio", "view", str(BOX)])
    assert e.value.code == 0
    page = (tmp_path / "out/lidded_box/lidded_box_view.html").read_text()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
                      .group(1).replace("<\\/", "</"))
    assert [s["name"] for s in data["scenes"]] == ["As printed", "Assembled"]
    assert [p["label"] for p in data["scenes"][1]["parts"]] == ["box", "lid", "card"]
    stl = base64.b64decode(data["scenes"][0]["parts"][0]["stl"])
    n = struct.unpack("<I", stl[80:84])[0]
    assert n > 12 and len(stl) == 84 + 50 * n


# ---------------------------------------------------------------- print check
def test_print_check_finds_the_hole_top_only_where_there_is_one():
    plate = build_local(trace((ROOT / "examples/native/plate.fs").read_text()))
    assert overhangs([b.shape for b in plate.run.solids]) == []      # vertical holes, flat on the bed
    bracket = build_local(trace((ROOT / "examples/native/l_bracket.fs").read_text()))
    items = overhangs([b.shape for b in bracket.run.solids])
    assert [o.kind for o in items] == ["hole or arch top"]            # the horizontal hole in the upright
    assert items[0].area_mm2 == pytest.approx(20.6, abs=1.0)


def test_print_check_ignores_45_degree_chamfers():
    src = (ROOT / "examples/native/plate.fs").read_text().replace(
        'fillet(context, id + "Corner fillets", {\n        "entities" : qParallelEdges(qCreatedBy(id + "Plate", EntityType.EDGE), vector(0, 0, 1)),\n        "radius" : 6 * millimeter });',
        'chamfer(context, id + "Bottom chamfer", {\n        "entities" : qContainsPoint(qCreatedBy(id + "Plate", EntityType.EDGE), vector(0, -25, 0) * millimeter),\n'
        '        "chamferType" : ChamferType.EQUAL_OFFSETS, "width" : 2 * millimeter });')
    assert "Bottom chamfer" in src
    loc = build_local(trace(src))
    assert loc.ok and not loc.partial
    assert overhangs([b.shape for b in loc.run.solids]) == []


# ---------------------------------------------------------------- local builder fixes
PLATE = """FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{
    var s = newSketch(context, id + "Plate sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(s, "r", { "firstCorner" : vector(-15, -10) * millimeter, "secondCorner" : vector(15, 10) * millimeter });
    skSolve(s);
    extrude(context, id + "Plate", { "entities" : qSketchRegion(id + "Plate sketch"), "endBound" : BoundingType.BLIND,
        "depth" : 5 * millimeter });
    chamfer(context, id + "Edge chamfer", { "entities" : EDGES, "chamferType" : ChamferType.EQUAL_OFFSETS,
        "width" : 1 * millimeter });
}
"""


def test_sketch_edge_and_solid_edge_are_different_entities():
    # the extrude's bottom face reuses the sketch edges; qEverything returns both, the chamfer must get the solid's
    src = PLATE.replace("EDGES", "qContainsPoint(qEverything(EntityType.EDGE), vector(0, -10, 0) * millimeter)")
    loc = build_local(trace(src))
    assert loc.ok and not loc.partial, (loc.error, loc.warnings)
    assert loc.metrics()["volume_mm3"] == pytest.approx(30 * 20 * 5 - 0.5 * 30, rel=1e-6)


def test_selecting_only_sketch_edges_says_so():
    src = PLATE.replace("EDGES", 'qCreatedBy(id + "Plate sketch", EntityType.EDGE)')
    loc = build_local(trace(src))
    assert not loc.ok and "sketch entities only" in loc.error


def test_chamfer_retried_at_mm_scale_keeps_the_feature_history(monkeypatch):
    from fsgen.fslite import geom as G
    real = G._blend

    def fails_in_meters(make, shape, edges, size, what):
        if size < 0.01:   # sizes in meters: pretend OCCT failed, as it did on a coil former
            raise G.FSError(f"{what} failed")
        return real(make, shape, edges, size, what)

    monkeypatch.setattr(G, "_blend", fails_in_meters)
    # chamfer one edge, then chamfer another found through the extrude's history: the scaled retry must keep it
    src = PLATE.replace("EDGES", 'qContainsPoint(qCreatedBy(id + "Plate", EntityType.EDGE), vector(0, -10, 0) * millimeter)')
    src = src.replace('        "width" : 1 * millimeter });\n}', '''        "width" : 1 * millimeter });
    chamfer(context, id + "Second chamfer", { "entities" : qContainsPoint(qCreatedBy(id + "Plate", EntityType.EDGE),
        vector(0, 10, 5) * millimeter), "chamferType" : ChamferType.EQUAL_OFFSETS, "width" : 1 * millimeter });
}''')
    loc = build_local(trace(src))
    assert loc.ok and not loc.partial, (loc.error, loc.warnings)
    assert loc.metrics()["volume_mm3"] == pytest.approx(30 * 20 * 5 - 2 * 0.5 * 30, rel=1e-6)
