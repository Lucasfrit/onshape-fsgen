"""Local engine regression: compat examples must keep producing Onshape's numbers.

Golden values in examples/compat/golden.json were measured in Onshape (`fsgen compare`).
"""
import json
from pathlib import Path

import pytest

from fsgen.compare import compare_metrics
from fsgen.fslite.runner import run_source

COMPAT = Path(__file__).resolve().parent.parent / "examples" / "compat"
GOLDEN = json.loads((COMPAT / "golden.json").read_text())


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_matches_onshape(name):
    res = run_source((COMPAT / name).read_text())
    assert res.ok, res.error
    assert compare_metrics(res.metrics(), GOLDEN[name]) == []


def test_union_targets_warning_matches_onshape():
    res = run_source((COMPAT / "user_project_box.fs").read_text())
    assert res.metrics()["bodies"] == 5  # Onshape ignores UNION targets without grouping
    assert any("targetsAndToolsNeedGrouping" in line for line in res.output)
