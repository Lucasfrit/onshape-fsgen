"""API call budget regression tests (offline, fake Onshape). Onshape EDU allows 2500 calls/year."""
import os
from pathlib import Path

import pytest

from fsgen.costmodel import fake_onshape
from fsgen.native import build as nb
from fsgen.native.script import trace
from fsgen.onshape import Onshape

PLATE = (Path(__file__).resolve().parent.parent / "examples" / "native" / "plate.fs").read_text()


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.delenv("ONSHAPE_RUN_BUDGET", raising=False)


def push(src, st=None, **kw):
    with fake_onshape(**kw):
        o = Onshape()
        st = st or nb.get_studio(o, "T", "a" * 24, "b" * 24, fresh=True)
        r = nb.push_trace(o, st, trace(src), log=lambda s: None)
        return o.calls, st, r


def test_fresh_push_costs_one_call_per_feature_plus_studio():
    n = len(trace(PLATE).features)  # variables live in a Variable Studio: not features
    assert all(f.feature_type != "assignVariable" for f in trace(PLATE).features)
    calls, _, r = push(PLATE)
    assert r.ok and calls == n + 1 + 3  # + Part Studio + Variable Studio (create, set, reference)


def test_variables_in_tree_cost_one_call_each():
    tr = trace(PLATE, variables_mode="tree")
    with fake_onshape():
        o = Onshape()
        st = nb.get_studio(o, "T", "a" * 24, "b" * 24, fresh=True)
        assert nb.push_trace(o, st, tr, log=lambda s: None).ok
        assert o.calls == len(tr.features) + 1


def test_unchanged_repush_is_free_and_edit_is_cheap():
    _, st, _ = push(PLATE)
    assert push(PLATE, st)[0] == 0
    edited = PLATE.replace("holeDiameter\", 5.5", "holeDiameter\", 6.6")
    calls, _, r = push(edited, st)
    assert r.ok and calls == 2  # set variables + 1 status check of the features
    edited2 = edited.replace("\"radius\" : 6 * millimeter", "\"radius\" : 5 * millimeter")
    calls, _, r = push(edited2, st)
    assert r.ok and calls == 1  # the last feature updated in place, nothing downstream


def test_failure_then_fix():
    n = len(trace(PLATE).features)
    calls, st, r = push(PLATE, fail_features={"Corner fillets"})
    assert not r.ok and calls == n + 1 + 3 + 1  # all features + studio + variable studio + 1 diagnose
    calls, _, r = push(PLATE, st)
    assert r.ok and calls == 1  # failed feature updated in place


def test_exports_and_metrics():
    _, st, _ = push(PLATE)
    with fake_onshape(translation_seconds=6):
        o = Onshape()
        nb.studio_metrics(o, st)
        assert o.calls == 1
        nb.export_studio(o, st, Path("x"), "p", ("stl",))
        assert o.calls == 2
        nb.export_studio(o, st, Path("x"), "p", ("step",))
        assert o.calls <= 6


def test_budget_stops_run():
    from fsgen.onshape import BudgetExceeded
    with fake_onshape():
        o = Onshape(budget=3)
        st = nb.get_studio(o, "T", "a" * 24, "b" * 24, fresh=True)
        with pytest.raises(BudgetExceeded):
            nb.push_trace(o, st, trace(PLATE), log=lambda s: None)
        assert o.calls == 3


@pytest.mark.parametrize("example", ["plate.fs", "l_bracket.fs", "flange.fs", "enclosure.fs"])
def test_estimate_equals_actual(example):
    from fsgen.usage import estimate_push
    src = (PLATE_DIR / example).read_text()
    tr = trace(src)
    est = estimate_push(tr, None, metrics=False)
    calls, st, r = push(src)
    assert r.ok and est.total == calls
    edited = src.replace("millimeter);", "millimeter * 1.1);", 1)  # change the first variable
    assert estimate_push(trace(edited), st, metrics=False).total == push(edited, st)[0]


PLATE_DIR = Path(__file__).resolve().parent.parent / "examples" / "native"


def test_set_official_resets_baseline(tmp_path, monkeypatch):
    from fsgen import usage
    env = tmp_path / ".env"
    env.write_text("ONSHAPE_API_BASELINE=697\nONSHAPE_API_BASELINE_DATE=2026-01-01T00:00:00\n")
    monkeypatch.setattr(usage, "LEDGER", tmp_path / "ledger.jsonl")
    (tmp_path / "ledger.jsonl").write_text('{"t": "2026-02-01T00:00:00", "counted": true, "call": "x"}\n')
    usage.set_official(800, str(env))
    assert "ONSHAPE_API_BASELINE=800" in env.read_text()
    assert usage.allocation_status().used == 800  # older ledger entries are already in the official number
