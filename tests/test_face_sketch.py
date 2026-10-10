"""Sketches on part faces: fsgen's frame, conversion to Onshape's measured frame, cap/parallel-plane queries."""
from pathlib import Path

import numpy as np
import pytest

from fsgen.costmodel import fake_onshape
from fsgen.native import build as nb
from fsgen.native.local import build_local
from fsgen.native.script import face_sketch_frame, trace
from fsgen.onshape import Onshape
from fsgen.usage import estimate_push

SCRIPT = (Path(__file__).resolve().parent.parent / "examples/native/face_sketches.fs").read_text()
# Onshape's evPlane on faces of test parts (origin mm, normal, x), measured 2026-10-10
MEASURED = {"plate top": ((0, 5, 6), (0, 0, 1), (1, 0, 0)), "plate bottom": ((0, 5, 0), (0, 0, -1), (1, 0, 0)),
            "tower side +Y": ((11, 10, 16), (0, 1, 0), (-1, 0, 0)), "tower start -X": ((8, 0, 21.1), (-1, 0, 0), (0, -1, 0)),
            "slanted": ((12.5, 20, 3), (0.371391, 0.928477, 0), (-0.928477, 0.371391, 0))}


@pytest.mark.parametrize("normal,x,y", [((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
                                        ((0, -1, 0), (1, 0, 0), (0, 0, 1)), ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
                                        ((1, 0, 0), (0, 1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 1, 0), (0, 0, -1))])
def test_face_frame_uses_the_parallel_default_plane(normal, x, y):
    origin, fx, fy = face_sketch_frame(np.array([3.0, 4.0, 5.0]) * np.abs(normal) + 7, np.array(normal, float))
    assert np.allclose(fx, x) and np.allclose(fy, y)
    assert np.allclose(np.cross(origin, normal), 0)  # on the normal through the world origin


@pytest.mark.parametrize("name", MEASURED)
def test_conversion_to_onshape_frame_keeps_world_positions(name):
    o, n, x = (np.array(v, float) for v in MEASURED[name])
    o /= 1000
    R, t = nb.sketch_frame((o, n, x))
    origin, fx, fy = face_sketch_frame(o, n)
    y = np.cross(n, x)
    for p in ([0, 0], [0.012, -0.005], [-0.02, 0.031]):
        world_fsgen = origin + p[0] * fx + p[1] * fy
        u, v = R @ p + t
        # Onshape's sketch on a face: origin = projected world origin, x = evPlane's x (evOwnerSketchPlane)
        assert np.allclose(origin + u * x + v * y, world_fsgen, atol=1e-6)  # measured values are rounded


def test_local_build_and_cap_queries():
    m = build_local(trace(SCRIPT)).metrics()
    # plate 40x30x4 + block 20x15x8 - hole d6x2 - triangle 7x10/2 x2 - slot 4x10x3 - pin d3x3 (by hand)
    assert m["parts"] == 1 and m["volume_mm3"] == pytest.approx(7200 - 9 * np.pi * 2 - 70 - 120 - 2.25 * np.pi * 3, abs=1e-3)


def test_quarter_turn_swaps_horizontal_and_vertical():
    tr = trace(SCRIPT)
    rec = next(f for f in tr.features if f.name == "Block top sketch")
    frame = nb.sketch_frame((np.array([0.004, 0, 0.019]), np.array([0, 0, 1.0]), np.array([0, 1.0, 0])))
    plain = {c["entityId"]: c["constraintType"] for c in nb.feature_json(rec, {}, frame=None)["constraints"]}
    turned = {c["entityId"]: c["constraintType"] for c in nb.feature_json(rec, {}, frame=frame)["constraints"]}
    assert plain["h_slot.bottom"] == "HORIZONTAL" and turned["h_slot.bottom"] == "VERTICAL"
    j = nb.feature_json(rec, {}, frame=frame)
    dirs = {c["entityId"]: p["value"] for c in j["constraints"] for p in c["parameters"] if p["parameterId"] == "direction"}
    assert any(k.startswith("pos") for k in dirs)  # position dimensions to the projected origin
    assert dirs["pos6x"] == "VERTICAL"


def test_push_without_a_measurable_frame_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    tr = trace(SCRIPT)
    est = estimate_push(tr, None)
    assert dict(est.items)["face sketch frames"] == 3
    with fake_onshape():  # the fake server returns no evPlane result
        o = Onshape()
        st = nb.get_studio(o, "Face", "a" * 24, "1" * 24)
        r = nb.push_trace(o, st, tr, log=lambda *a: None)
    assert r.ok and sum("could not measure" in w for w in r.warnings) == 3
    assert o.calls == est.total - 1  # no volume check here


def test_variable_change_does_not_resend_face_sketches():
    a = trace(SCRIPT)
    b = trace(SCRIPT.replace('"width", 40', '"width", 50').replace('"holeDia", 6', '"holeDia", 8'))
    for fa, fb in zip(a.features, b.features):
        if fa.sketch is not None and fa.sketch.origin_base is None:
            assert nb.feature_hash(fa) == nb.feature_hash(fb), fa.name
