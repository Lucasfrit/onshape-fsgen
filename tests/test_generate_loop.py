"""The unattended generate loop with a scripted fake LLM (no Claude, no Onshape)."""
from pathlib import Path

import pytest

from fsgen.native import generate as gen

ROOT = Path(__file__).resolve().parent.parent
PLATE = (ROOT / "examples/native/plate.fs").read_text()
THICKEN = PLATE.replace('''    fillet(context, id + "Corner fillets", {''', '''    thicken(context, id + "Skin", { "entities" : qCreatedBy(id + "Plate", EntityType.FACE),
        "thickness" : 1 * millimeter });
    fillet(context, id + "Corner fillets", {''')


class FakeLLM:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, prompt, image=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def block(code):
    return f"```featurescript\n{code}\n```"


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.setattr(gen, "render", lambda *a, **k: None)  # skip matplotlib in tests

    def _run(replies, **kw):
        fake = FakeLLM(replies)
        monkeypatch.setattr(gen, "make_llm", lambda *a, **k: fake)
        res = gen.generate_native("a plate", tmp_path / "out", name="Plate", paste=True, **kw)
        return res, fake
    return _run


def test_paste_mode_rejects_unpasteable_features_early_and_records_assumptions(run, tmp_path):
    res, fake = run([
        "Assumptions:\n- plate is 80 x 50 mm\n- holes 5.5 mm\n\n" + block(THICKEN),
        "thicken is not pasteable, removed.\n" + block(PLATE),
        "Looks right.\nLGTM",
    ])
    assert res["ok"] and res["verified"] and res["api_calls"] == 0
    assert [h[0] for h in res["history"]] == ["repair-mode"]
    assert "Paste mode cannot use: thicken" in fake.prompts[1]
    assert "Use ONLY these standard features" in fake.prompts[0]
    assert (tmp_path / "out/assumptions.md").read_text().count("- ") == 2
    assert (tmp_path / "out/paste_feature.fs").exists()


def test_reviewer_fix_gets_a_follow_up_review(run):
    thicker = PLATE.replace('"thickness", 6', '"thickness", 8')
    res, fake = run([
        "Assumptions:\n- none\n\n" + block(PLATE),
        "The plate should be 8 mm thick.\n" + block(thicker),
        "Fixed.\nLGTM",
    ])
    assert res["ok"] and res["reviews"] == 2
    assert "follow-up review" in fake.prompts[2] and "8 mm thick" in fake.prompts[2]


def test_partial_build_is_flagged_to_the_reviewer():
    from fsgen.native.local import build_local
    from fsgen.native.prompts import review_prompt
    from fsgen.native.script import trace
    tr = trace(THICKEN)
    loc = build_local(tr)
    unbuilt = gen.not_built_features(tr, loc)
    assert unbuilt == ['"Skin" (thicken)']
    prompt = review_prompt("a plate", THICKEN, loc.report(), unbuilt)
    assert "NOT built locally" in prompt and '"Skin" (thicken)' in prompt
