"""STEP -> featuretree IR -> fsgen Part Studio script must reproduce featuretree's own rebuild."""
from pathlib import Path

from fsgen.native.local import build_local
from fsgen.native.script import trace
from fsgen.recognize import _featuretree, ir_to_script


def test_plate_round_trip(tmp_path):
    from fsgen.fslite.runner import export
    root = Path(__file__).resolve().parent.parent
    loc = build_local(trace((root / "examples/native/plate.fs").read_text()))
    step = export(loc.run, tmp_path, "plate", formats=("step",))[0]
    sr, b3d = _featuretree()
    spec, _ = sr.recognize(str(step))
    _, ft = b3d.emit(spec)
    ours = build_local(trace(ir_to_script(spec)))
    assert ours.ok
    assert abs(ours.metrics()["volume_mm3"] - ft["volume"]) < 0.5
    assert abs(ours.metrics()["volume_mm3"] - 23244.385) < 0.5
