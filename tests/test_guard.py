"""API guard: daily cap across runs, Keychain keys read only when a call is made, `fsgen keys store`."""
import json
import time
from unittest import mock

import pytest

import fsgen.onshape as O
from fsgen.costmodel import fake_onshape


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY", "FSGEN_DAILY_BUDGET", "ONSHAPE_RUN_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(O, "load_dotenv", lambda *a, **k: None)


def _ledger(path, n_today, n_yesterday=0):
    today, old = time.strftime("%Y-%m-%d"), "2000-01-01"
    rows = [{"t": f"{d}T10:00:00", "run": "r", "purpose": "", "call": "GET x", "status": 200, "counted": True}
            for d in [today] * n_today + [old] * n_yesterday]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_daily_cap_counts_calls_from_other_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.setenv("FSGEN_DAILY_BUDGET", "5")
    ledger = tmp_path / "ledger.jsonl"
    _ledger(ledger, n_today=3, n_yesterday=50)
    with fake_onshape(), mock.patch.object(O, "LEDGER", ledger):
        o = O.Onshape()
        o.post(f"partstudios/d/{'a' * 24}/w/{'1' * 24}", {"name": "x"})
        o.post(f"partstudios/d/{'a' * 24}/w/{'1' * 24}", {"name": "y"})
        with pytest.raises(O.BudgetExceeded, match="daily cap of 5"):
            o.post(f"partstudios/d/{'a' * 24}/w/{'1' * 24}", {"name": "z"})
    assert O.calls_today() == 0  # LEDGER patch is gone again; nothing written to the working directory


def test_keychain_keys_are_read_at_the_first_call_only(monkeypatch):
    reads = []
    monkeypatch.setattr(O, "keychain_has_keys", lambda: True)
    monkeypatch.setattr(O, "keychain_keys", lambda: reads.append(1) or ("ka", "kb"))
    with fake_onshape():
        o = O.Onshape()
        assert reads == []  # constructing the client does not ask macOS for the keys
        o.post(f"partstudios/d/{'a' * 24}/w/{'1' * 24}", {"name": "x"})
        o.post(f"partstudios/d/{'a' * 24}/w/{'1' * 24}", {"name": "y"})
    assert reads == [1] and o.auth == ("ka", "kb")


def test_missing_keys_name_both_options(monkeypatch):
    monkeypatch.setattr(O, "keychain_has_keys", lambda: False)
    with pytest.raises(O.OnshapeError, match="fsgen keys store"):
        O.Onshape()


def test_keys_store_moves_keys_out_of_env(tmp_path, monkeypatch):
    from fsgen import cli
    (tmp_path / ".env").write_text("ONSHAPE_ACCESS_KEY=abc\nONSHAPE_SECRET_KEY=def\nONSHAPE_API_ALLOCATION=2500\n")
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "abc")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "def")
    stored = []
    monkeypatch.setattr(O, "keychain_store", lambda a, s: stored.append((a, s)))
    with pytest.raises(SystemExit) as e:
        cli.main(["keys", "store"])
    assert e.value.code == 0
    text = (tmp_path / ".env").read_text()
    assert stored == [("abc", "def")] and "abc" not in text and "def\n" not in text
    assert "ONSHAPE_API_ALLOCATION=2500" in text
