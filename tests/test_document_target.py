"""--document targeting and feature-pattern defaults (offline, fake Onshape)."""
from pathlib import Path

import pytest

from fsgen.cloud import parse_document
from fsgen.costmodel import fake_onshape
from fsgen.native import build as nb
from fsgen.native.script import trace
from fsgen.onshape import Onshape

PLATE = (Path(__file__).resolve().parent.parent / "examples/native/plate.fs").read_text()
DOC_A = "https://cad.onshape.com/documents/" + "a" * 24 + "/w/" + "1" * 24 + "/e/" + "f" * 24
DOC_B = "https://cad.onshape.com/documents/" + "a" * 24 + "/w/" + "2" * 24  # a branch of the same document


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.delenv("FSGEN_DOCUMENT", raising=False)


def test_parse_document_urls():
    assert parse_document(DOC_A) == {"did": "a" * 24, "wid": "1" * 24}
    with pytest.raises(ValueError):
        parse_document("https://cad.onshape.com/documents/abc")


def test_same_part_name_in_two_workspaces_keeps_separate_state():
    from fsgen.cloud import target_workspace
    with fake_onshape():
        o = Onshape()
        for url in (DOC_A, DOC_B):
            ws = target_workspace(o, url)
            st = nb.get_studio(o, "Plate", ws["did"], ws["wid"])
            assert nb.push_trace(o, st, trace(PLATE), log=lambda s: None).ok
        assert o.calls == 2 * (len(trace(PLATE).features) + 1 + 3)  # no document creation calls
    a = nb.find_state("Plate", "a" * 24, "1" * 24)
    b = nb.find_state("Plate", "a" * 24, "2" * 24)
    assert a and b and a.eid != b.eid
    with fake_onshape():  # re-push to the branch: nothing changed, 0 calls
        o = Onshape()
        assert nb.push_trace(o, nb.get_studio(o, "Plate", "a" * 24, "2" * 24), trace(PLATE), log=lambda s: None).ok
        assert o.calls == 0


def test_legacy_state_keyed_by_name_is_still_found(tmp_path):
    st = nb.StudioState("d" * 24, "w" * 24, "e" * 24, "Old part")  # written before per-document keys
    nb.save_state(st)
    assert nb.find_state("Old part", "d" * 24, "w" * 24).eid == "e" * 24
    assert nb.find_state("Old part", "d" * 24, "x" * 24) is None  # other workspace: a new studio


def test_feature_patterns_reapply_features_by_default():
    tr = trace(PLATE)
    for f in tr.features:
        if f.feature_type == "mirror":
            params = {p["parameterId"]: p for p in nb.feature_json(f, {g.name: "F" for g in tr.features})["parameters"]}
            assert params["fullFeaturePattern"]["value"] is True
    explicit = trace(PLATE.replace('"patternType" : PatternType.FEATURE,',
                                   '"patternType" : PatternType.FEATURE, "fullFeaturePattern" : false,', 1))
    m = next(f for f in explicit.features if f.feature_type == "mirror")
    params = {p["parameterId"]: p for p in nb.feature_json(m, {g.name: "F" for g in explicit.features})["parameters"]}
    assert params["fullFeaturePattern"]["value"] is False  # an explicit choice in the script wins
