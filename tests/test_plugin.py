"""The Claude Code plugin and marketplace manifests agree with each other and with pyproject.toml."""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def _plugin_dir():
    (entry,) = MARKETPLACE["plugins"]
    return ROOT / entry["source"], entry


def test_marketplace_points_at_the_plugin():
    path, entry = _plugin_dir()
    manifest = json.loads((path / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == entry["name"] == MARKETPLACE["name"] == PROJECT["name"]
    assert manifest["version"] == PROJECT["version"]  # bump both together


def test_skill_frontmatter():
    path, _ = _plugin_dir()
    text = (path / "skills" / "make-part" / "SKILL.md").read_text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert fields["name"] == "make-part" and fields["description"]
    # only commands that cost no Onshape calls are pre-approved: paste and push must ask
    assert "push" not in fields["allowed-tools"] and "paste" not in fields["allowed-tools"]
    assert ".venv" not in text  # the skill runs the installed fsgen, not a repo checkout
