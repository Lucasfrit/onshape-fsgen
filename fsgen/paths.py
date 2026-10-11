"""Where fsgen finds its bundled data and the user's own files.

Bundled data (rules.md, examples/, third_party/featuretree) sits at the repo root in a checkout. An installed wheel
carries the same files under fsgen/data/ (pyproject.toml: force-include), with the same layout below it.
Per-user files (rules.md, .env, the API ledger) live in config_dir().
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

PKG = Path(__file__).resolve().parent
DATA = PKG / "data" if (PKG / "data").is_dir() else PKG.parent


def config_dir() -> Path:
    """$FSGEN_HOME, else $XDG_CONFIG_HOME/fsgen, else ~/.config/fsgen."""
    if os.environ.get("FSGEN_HOME"):
        return Path(os.environ["FSGEN_HOME"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "fsgen"


def load_env():
    """Load .env from the working directory (or the nearest parent that has one), then config_dir()/.env.

    Variables already set win over both files, and the working directory's .env wins over the user's.
    (`load_dotenv()` without a path searches from the calling module's folder, i.e. site-packages when installed.)
    """
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(config_dir() / ".env")


def ledger_path() -> Path:
    """The log of Onshape API calls. The allocation belongs to the Onshape account, so the log lives in config_dir();
    a .fsgen_api_ledger.jsonl in the working directory (older repo checkouts) is still used if it exists."""
    legacy = Path(".fsgen_api_ledger.jsonl")
    return legacy if legacy.exists() else config_dir() / "api_ledger.jsonl"
