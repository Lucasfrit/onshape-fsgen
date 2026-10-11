"""Bundled data, per-user rules.md, .env and ledger locations (what makes fsgen work outside its repo)."""
from pathlib import Path

import pytest

from fsgen import cli, paths
from fsgen.native import prompts


def test_bundled_data_is_found():
    assert (paths.DATA / "rules.md").exists()
    for name in prompts.FEW_SHOT:
        assert (prompts.EXAMPLES / name).exists()
    assert (paths.DATA / "third_party" / "featuretree" / "step_recognize.py").exists()


def test_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("FSGEN_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / "fsgen"
    monkeypatch.setenv("FSGEN_HOME", str(tmp_path / "h"))
    assert paths.config_dir() == tmp_path / "h"


def test_user_rules_replace_the_default(capsys):
    assert prompts.rules_path() == prompts.DEFAULT_RULES
    with pytest.raises(SystemExit) as e:
        cli.main(["rules", "--init"])
    assert e.value.code == 0
    user = paths.config_dir() / "rules.md"
    assert user.read_text() == prompts.DEFAULT_RULES.read_text()
    user.write_text("- Every part is purple.\n")
    assert prompts.rules_path() == user
    assert "Every part is purple." in prompts.system_prompt()
    with pytest.raises(SystemExit) as e:
        cli.main(["rules", "--init"])  # never overwrites the user's file
    assert e.value.code == 1 and user.read_text() == "- Every part is purple.\n"


def test_env_from_working_directory_then_user_config(tmp_path, monkeypatch):
    for k in ("FSGEN_TEST_A", "FSGEN_TEST_B"):
        monkeypatch.delenv(k, raising=False)
    project = tmp_path / "project" / "sub"
    project.mkdir(parents=True)
    (tmp_path / "project" / ".env").write_text("FSGEN_TEST_A=project\n")
    (paths.config_dir() / ".env").write_text("FSGEN_TEST_A=user\nFSGEN_TEST_B=user\n")
    monkeypatch.chdir(project)
    paths.load_env()
    import os
    assert os.environ["FSGEN_TEST_A"] == "project" and os.environ["FSGEN_TEST_B"] == "user"
    monkeypatch.delenv("FSGEN_TEST_A")
    monkeypatch.delenv("FSGEN_TEST_B")


def test_ledger_in_user_config_unless_one_is_in_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.ledger_path() == paths.config_dir() / "api_ledger.jsonl"
    Path(".fsgen_api_ledger.jsonl").write_text("")
    assert paths.ledger_path() == Path(".fsgen_api_ledger.jsonl")


def test_set_official_writes_next_to_the_ledger(monkeypatch):
    from fsgen import usage
    usage.set_official(1234)
    assert "ONSHAPE_API_BASELINE=1234" in (paths.config_dir() / ".env").read_text()
    monkeypatch.delenv("ONSHAPE_API_BASELINE")
    monkeypatch.delenv("ONSHAPE_API_BASELINE_DATE")
