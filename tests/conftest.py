import pytest


@pytest.fixture(autouse=True)
def isolated_user_files(tmp_path_factory, monkeypatch):
    """Keep tests away from the real per-user config (rules.md, .env, API ledger)."""
    import fsgen.onshape
    import fsgen.usage

    home = tmp_path_factory.mktemp("fsgen-home")
    monkeypatch.setenv("FSGEN_HOME", str(home))
    monkeypatch.setattr(fsgen.onshape, "LEDGER", home / "api_ledger.jsonl")
    monkeypatch.setattr(fsgen.usage, "LEDGER", home / "api_ledger.jsonl")
