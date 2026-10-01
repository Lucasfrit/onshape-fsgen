"""Build fsgen/native/featurespecs.json from Onshape's Part Studio feature specs.

`fsgen features --refresh` downloads GET /partstudios/.../featurespecs (all standard features with
parameter ids, types, enum options and UI defaults) and compacts it. Every parameter keeps the JSON the
Onshape UI would send by default ("defaultJson"); array parameters keep their item sub-parameters.
"""
from __future__ import annotations

import json
from pathlib import Path

SPECS_PATH = Path(__file__).with_name("featurespecs.json")
STRIP = ("nodeId", "libraryRelationType", "namespace", "parameterName")


def _qty_default(p):
    rs = p.get("ranges") or []
    pref = [r for r in rs if r.get("units") in ("meter", "degree")] or rs
    if not pref or pref[0].get("defaultValue") is None:
        return None
    v, u = pref[0]["defaultValue"], pref[0].get("units")
    if p.get("quantityType") == "LENGTH" or u == "meter":
        expr = f"{v * 1000:.10g} mm"
    elif p.get("quantityType") == "ANGLE" or u == "degree":
        expr = f"{v:.10g} deg"
    else:
        expr = f"{v:.10g}"
    return {"btType": "BTMParameterQuantity-147", "parameterId": p["parameterId"], "expression": expr}


def compact_param(p) -> dict:
    t = p["btType"].split("-")[0].replace("BTParameterSpec", "")
    e = {"type": t}
    dv = p.get("defaultValue")
    if t == "Enum":
        e["enum"] = p.get("enumName")
        e["options"] = [o["option"] if isinstance(o, dict) else o for o in p.get("options", [])]
        e["default"] = dv.get("value") if isinstance(dv, dict) else dv
    elif t == "Quantity":
        e["quantity"] = p.get("quantityType")
    elif t == "Boolean":
        e["default"] = dv.get("value") if isinstance(dv, dict) else dv
    elif t == "Array":
        e["items"] = {q["parameterId"]: compact_param(q) for q in p.get("parameters", [])}
    dj = _qty_default(p) if t == "Quantity" else (
        {k: v for k, v in dv.items() if k not in STRIP} if isinstance(dv, dict) else None)
    if dj:
        e["defaultJson"] = dj
    hints = p.get("uiHints") or []
    if "ALWAYS_HIDDEN" in hints:
        e["hidden"] = True
    if p.get("parameterName"):
        e["label"] = p["parameterName"]
    return e


def compact(raw: dict) -> dict:
    out = {}
    for s in raw["featureSpecs"]:
        out[s["featureType"]] = {"name": s.get("featureTypeName"), "nameTemplate": s.get("featureNameTemplate", ""),
                                 "params": {p["parameterId"]: compact_param(p) for p in s["parameters"]}}
    return out


def refresh(o, did: str, wid: str, eid: str) -> int:
    raw = o.get(f"partstudios/d/{did}/w/{wid}/e/{eid}/featurespecs")
    data = compact(raw)
    SPECS_PATH.write_text(json.dumps(data, separators=(",", ":")))
    return len(data)


if __name__ == "__main__":
    raw = json.loads(Path("ref/partstudio_featurespecs.json").read_text())
    data = compact(raw)
    SPECS_PATH.write_text(json.dumps(data, separators=(",", ":")))
    print(len(data), "feature types,", SPECS_PATH.stat().st_size, "bytes")
