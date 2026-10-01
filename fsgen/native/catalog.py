"""Feature catalog: which Onshape features are verified, learned on the way, or only available.

- verified:  used in a reference example in examples/native/ that builds cleanly in Onshape.
- learned:   built successfully in Onshape during a generate/push run; the working call is kept as an
             example and shown to the model next time.
- available: present in Onshape's feature specs; the script checker validates parameter ids/enums, but
             it has never been built. The model may use it; success promotes it to "learned".
Failures of non-verified features are recorded as notes so the model sees what went wrong before.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .script import SPECS, CHECKED_TYPES

CATALOG = Path(__file__).with_name("catalog.json")


def load() -> dict:
    return json.loads(CATALOG.read_text()) if CATALOG.exists() else {}


def save(cat: dict):
    CATALOG.write_text(json.dumps(dict(sorted(cat.items())), indent=2))


def status(ft: str, cat: dict | None = None) -> str:
    cat = load() if cat is None else cat
    return cat.get(ft, {}).get("status", "available")


def settable_params(ft: str) -> list[str]:
    return [k for k, p in SPECS[ft]["params"].items() if p["type"] in CHECKED_TYPES and not p.get("hidden")]


def call_snippet(src: str, line: int) -> str:
    """Source text of the statement starting at `line` (1-based), up to its closing ');'."""
    lines = src.splitlines()
    if not 1 <= line <= len(lines):
        return ""
    text = "\n".join(lines[line - 1:])
    depth, started = 0, False
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
            started = True
        elif ch == ")":
            depth -= 1
            if started and depth == 0:
                end = text.find(";", i)
                return text[: (end + 1) if end >= 0 else i + 1].strip()
    return lines[line - 1].strip()


def record_success(features, src: str, source: str):
    """Promote feature types that just built in Onshape to 'learned' (keeps one example call)."""
    cat = load()
    changed = []
    for f in features:
        if f.feature_type in ("assignVariable", "newSketch"):
            continue
        entry = cat.setdefault(f.feature_type, {"status": "available"})
        if entry["status"] in ("verified", "learned"):
            continue
        entry.update(status="learned", example=call_snippet(src, f.line), learnedFrom=source,
                     date=time.strftime("%Y-%m-%d"))
        changed.append(f.feature_type)
    if changed:
        save(cat)
    return changed


def record_failure(feature_type: str, error: str, source: str):
    cat = load()
    entry = cat.setdefault(feature_type, {"status": "available"})
    if entry["status"] == "verified":
        return
    notes = entry.setdefault("failures", [])
    first = error.strip().splitlines()
    notes.append(f"{time.strftime('%Y-%m-%d')} ({source[:60]}): {' | '.join(first[1:3]) or first[0] if first else ''}")
    entry["failures"] = notes[-5:]
    save(cat)


def prompt_section() -> str:
    """Catalog summary for the system prompt: learned examples + names of other available features."""
    cat = load()
    learned = [(ft, e) for ft, e in cat.items() if e.get("status") == "learned"]
    verified = sorted(ft for ft, e in cat.items() if e.get("status") == "verified")
    other = sorted(ft for ft in SPECS if ft not in cat or cat[ft].get("status") == "available")
    out = ["# Feature catalog",
           f"Verified (documented above): {', '.join(verified)}."]
    if learned:
        out.append("Learned (built successfully before; working calls):")
        for ft, e in learned:
            out.append(f"## {ft}\n```featurescript\n{e.get('example', '')}\n```")
    out.append("Other Onshape features you may use when the design needs them (call them by featureType with "
               "Onshape parameter ids; the checker lists valid parameter ids and enum values if you get one wrong): "
               + ", ".join(other) + ".")
    fails = [(ft, e["failures"][-1]) for ft, e in cat.items() if e.get("failures") and e.get("status") != "learned"]
    if fails:
        out.append("Previous failures to avoid: " + "; ".join(f"{ft}: {msg}" for ft, msg in fails))
    return "\n".join(out)


# What the local builder (fsgen/native/local.py) supports per feature type; "" = not implemented locally.
LOCAL_SUPPORT = {
    "assignVariable": "yes (values used directly)",
    "newSketch": "lines, arcs, circles, rectangles, polylines, polygons; Top/Front/Right, offset planes, planar faces*",
    "extrude": "NEW/ADD/REMOVE/INTERSECT; BLIND, THROUGH_ALL, symmetric, second direction; no UP_TO_*, draft",
    "revolve": "full or angle, symmetric, opposite direction",
    "fillet": "constant radius (OCCT may fail where Onshape succeeds -> warning)",
    "chamfer": "EQUAL_OFFSETS only",
    "cPlane": "OFFSET only",
    "linearPattern": "FEATURE (extrude/revolve/loft) and PART; one or two directions",
    "circularPattern": "FEATURE and PART; equal spacing / fixed angle",
    "mirror": "FEATURE and PART",
    "shell": "faces to remove, inward (or opposite direction)",
    "booleanBodies": "UNION / SUBTRACTION / INTERSECTION, keepTools",
    "deleteBodies": "yes",
    "loft": "solid between sheet profiles (outer wires); no guides/conditions",
    "helix": "axisType CIRCLE (sketch circle) or AXIS; TURNS / PITCH / TURNS_PITCH, handedness; no SURFACE, end point",
    "sweep": "solid NEW/ADD/REMOVE/INTERSECT along edges (helix: exact screw motion); no profile control, twist, scale",
}


def markdown_table() -> str:
    cat = load()
    rows = ["| feature (Onshape type) | status in Onshape | local builder | notes |", "|---|---|---|---|"]
    order = {"verified": 0, "learned": 1, "available": 2}
    shown = [ft for ft in SPECS if status(ft, cat) != "available" or ft in LOCAL_SUPPORT]
    for ft in sorted(shown, key=lambda f: (order[status(f, cat)], f)):
        e = cat.get(ft, {})
        note = "; ".join(e.get("notes", [])[:1]) or (e.get("failures") or [""])[-1]
        rows.append(f"| {SPECS[ft]['name']} (`{ft}`) | {status(ft, cat)} | {LOCAL_SUPPORT.get(ft, 'no')} | {note} |")
    rest = sorted(ft for ft in SPECS if ft not in shown)
    rows.append("")
    rows.append(f"Also available in Onshape but never tried, and not in the local builder ({len(rest)}): "
                + ", ".join(f"`{ft}`" for ft in rest) + ".")
    return "\n".join(rows)
