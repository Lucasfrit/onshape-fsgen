"""Onshape side of the pipeline: validate, push, measure and export FS-Lite parts.

Workspace layout (one Onshape document per fsgen workspace):
    Feature Studio "Gen"   - receives the generated source
    Part Studio  "Part"    - holds one instance of the generated feature
    Part Studio  "Eval"    - kept empty; used to run the feature body via evalFeatureScript,
                             which (unlike Feature Studios) returns error messages with line numbers.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .fslite.parser import parse
from .fslite.runner import feature_parameters
from .fslite.values import Q, fmt
from .onshape import Onshape, OnshapeError, RateLimited

STATE_FILE = Path(".fsgen_workspace.json")


@dataclass
class Workspace:
    did: str
    wid: str
    fs: str  # feature studio element id
    part: str  # part studio element id
    eval: str  # empty part studio for diagnostics
    feature_id: str | None = None  # instance of the generated feature in `part`
    feature_type: str | None = None
    state_file: str = str(STATE_FILE)

    def save(self):
        Path(self.state_file).write_text(json.dumps(self.__dict__, indent=2))

    def url(self, base="https://cad.onshape.com", eid=None):
        return f"{base}/documents/{self.did}/w/{self.wid}/e/{eid or self.part}"


def workspace_ids(o: Onshape, state_file: Path = STATE_FILE) -> dict:
    """{did, wid, ...} of the fsgen document: read from the state file (0 calls) or create it once (4 calls)."""
    if state_file.exists():
        return json.loads(state_file.read_text())
    return ensure_workspace(o, state_file=state_file).__dict__


def ensure_workspace(o: Onshape, name: str = "fsgen-workspace", state_file: Path = STATE_FILE,
                     fresh: bool = False) -> Workspace:
    if state_file.exists() and not fresh:
        ws = Workspace(**{**json.loads(state_file.read_text()), "state_file": str(state_file)})
        try:
            o.get(f"documents/{ws.did}")
            return ws
        except OnshapeError:
            pass
    doc = o.post("documents", {"name": name, "isPublic": False})
    did, wid = doc["id"], doc["defaultWorkspace"]["id"]
    els = o.get(f"documents/d/{did}/w/{wid}/elements")
    part = next(e["id"] for e in els if e["elementType"] == "PARTSTUDIO")
    ev = o.post(f"partstudios/d/{did}/w/{wid}", {"name": "Eval"})["id"]
    fs = o.post(f"featurestudios/d/{did}/w/{wid}", {"name": "Gen"})["id"]
    ws = Workspace(did, wid, fs, part, ev, state_file=str(state_file))
    ws.save()
    return ws


# ------------------------------------------------------------------ eval lambda
@dataclass
class EvalScript:
    script: str
    line_map: list[int]  # script line (0-based) -> source line (1-based, 0 = synthetic)

    def src_line(self, script_line: int) -> int:
        if 1 <= script_line <= len(self.line_map):
            return self.line_map[script_line - 1]
        return 0


def fs_literal(v) -> str:
    """FeatureScript source literal for a parameter value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Q):
        if v.dims == (1, 0):
            return f"({v.v * 1000!r} * millimeter)"
        if v.dims == (0, 1):
            return f"({v.v * 180 / 3.141592653589793!r} * degree)"
        raise ValueError(f"cannot express {fmt(v)} as a literal")
    if isinstance(v, (int, float)):
        return repr(float(v))
    if isinstance(v, str):
        return json.dumps(v)
    raise ValueError(f"unsupported parameter value {v!r}")


def onshape_parameters(params: list[dict], values: dict) -> list[dict]:
    """Feature-instance parameter JSON for the add/update feature API."""
    out = []
    for p in params:
        v = values[p["name"]]
        if p["kind"] == "boolean":
            out.append({"btType": "BTMParameterBoolean-144", "parameterId": p["name"], "value": bool(v)})
        elif p["kind"] == "string":
            out.append({"btType": "BTMParameterString-149", "parameterId": p["name"], "value": v})
        else:
            if isinstance(v, Q) and v.dims == (1, 0):
                expr = f"{v.v * 1000:.10g} mm"
            elif isinstance(v, Q) and v.dims == (0, 1):
                expr = f"{v.v * 180 / 3.141592653589793:.10g} deg"
            else:
                expr = f"{float(v):.10g}"
            out.append({"btType": "BTMParameterQuantity-147", "parameterId": p["name"], "expression": expr,
                        "isInteger": p["kind"] == "integer"})
    return out


def build_eval_script(src: str, feature: str | None = None, params: dict | None = None) -> EvalScript:
    """Turn a Feature Studio file into a single lambda for /featurescript evaluation.

    consts and helper functions become locals; the feature body runs inline with
    id = newId() + "F". Returns metrics so we can compare against the local engine.
    """
    prog = parse(src)
    chunks: list[tuple[str, int]] = [("function(context is Context, queries) {\n", 0)]

    def add_slice(text: str, start: int):
        line = src.count("\n", 0, start) + 1
        chunks.append((text, line))

    feat_body = None
    for d in prog.decls:
        if d.kind == "const":
            v = d.value
            is_feat = v.kind == "call" and v.fn.kind == "name" and v.fn.name == "defineFeature"
            if is_feat and (feature is None or d.name == feature) and feat_body is None:
                feat_body = v.args[0].body
                continue
            if is_feat:
                continue
            a, b = d.span
            add_slice(re.sub(r"^export\s+", "", src[a:b]) + "\n", a)
        elif d.kind == "fundecl":
            a, b = d.span
            text = re.sub(r"^(export\s+)?(function|predicate)\s+(\w+)\s*\(", r"const \3 = function(", src[a:b])
            add_slice(text + ";\n", a)
    if feat_body is None:
        raise ValueError("no defineFeature found")
    _, plist, values = feature_parameters(src, feature, params)
    definition = "{ " + ", ".join(f'"{k}" : {fs_literal(v)}' for k, v in values.items()) + " }"
    chunks.append((f'var id = newId() + "F";\nvar definition = {definition};\n', 0))
    add_slice(src[feat_body.start + 1:feat_body.end] + "\n", feat_body.start + 1)
    chunks.append((
        "var __bodies = qAllModifiableSolidBodies();\n"
        "if (isQueryEmpty(context, __bodies)) { return { \"bodies\" : 0 }; }\n"
        "var __box = evBox3d(context, { \"topology\" : __bodies, \"tight\" : true });\n"
        "return { \"bodies\" : size(evaluateQuery(context, __bodies)),\n"
        "  \"volume\" : evVolume(context, { \"entities\" : __bodies }),\n"
        "  \"area\" : evArea(context, { \"entities\" : qOwnedByBody(__bodies, EntityType.FACE) }),\n"
        "  \"lo\" : __box.minCorner, \"hi\" : __box.maxCorner };\n}\n", 0))
    script, line_map = "", []
    for text, line in chunks:
        script += text
        n = text.count("\n")
        # A slice may start mid-line in the source (feature body), so map line by line.
        line_map += [line + k if line else 0 for k in range(n)]
    return EvalScript(script, line_map)


def _simplify(v):
    if isinstance(v, dict):
        t = v.get("btType", "")
        if t.endswith("BTFSValueArray"):
            return [_simplify(x) for x in v["value"]]
        if t.endswith("BTFSValueMap"):
            return {_simplify(e["key"]): _simplify(e["value"]) for e in v["value"]}
        if "value" in v:
            return _simplify(v["value"])
    return v


@dataclass
class CloudResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    console: str = ""
    metrics: dict | None = None
    seconds: float = 0.0


def evaluate(o: Onshape, ws: Workspace, src: str, feature: str | None = None,
             params: dict | None = None) -> CloudResult:
    t0 = time.time()
    try:
        es = build_eval_script(src, feature, params)
    except Exception as e:  # our parser rejected it; report as-is
        return CloudResult(False, [f"local parse failed before cloud eval: {e}"])
    r = o.post(f"partstudios/d/{ws.did}/w/{ws.wid}/e/{ws.eval}/featurescript", {"script": es.script})
    errors = []
    for n in r.get("notices") or []:
        if n.get("level") != "ERROR":
            continue
        loc = ""
        for st in n.get("stackTrace") or []:
            if st.get("line") and not st.get("document", "").startswith("onshape/std"):
                sl = es.src_line(st["line"])
                if sl:
                    loc = f"line {sl}:{st.get('column', 0)}: "
                    break
        errors.append(f"{loc}{n.get('type', '')} {n.get('message', '')}".strip())
    res = CloudResult(not errors, errors, r.get("console", ""), seconds=time.time() - t0)
    out = _simplify(r.get("result"))
    if isinstance(out, dict) and not errors:
        if out.get("bodies"):
            lo, hi = out["lo"], out["hi"]
            res.metrics = {"bodies": int(out["bodies"]), "volume_mm3": round(out["volume"] * 1e9, 4),
                           "area_mm2": round(out["area"] * 1e6, 4),
                           "bbox_mm": [round(v * 1000, 4) for v in list(lo) + list(hi)]}
        else:
            res.metrics = {"bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
    return res


# ------------------------------------------------------------------ push + export
def push(o: Onshape, ws: Workspace, src: str, feature_name: str = "Generated part",
         params: dict | None = None) -> dict:
    """Upload source to the Feature Studio and (re)instantiate it in the Part Studio.

    Parameter values come from the precondition defaults, overridden by `params`.
    """
    fname, plist, values = feature_parameters(src, None, params)
    cur = o.get(f"featurestudios/d/{ws.did}/w/{ws.wid}/e/{ws.fs}")
    o.post(f"featurestudios/d/{ws.did}/w/{ws.wid}/e/{ws.fs}", {
        "contents": src, "serializationVersion": cur["serializationVersion"],
        "sourceMicroversion": cur["sourceMicroversion"], "rejectMicroversionSkew": False})
    specs = o.get(f"featurestudios/d/{ws.did}/w/{ws.wid}/e/{ws.fs}/featurespecs")["featureSpecs"]
    if not specs:
        return {"ok": False, "error": "Feature Studio did not compile (no feature specs). Run `evaluate` for messages."}
    spec = next((x for x in specs if x["featureType"] == fname), specs[0])
    feature = {"btType": "BTMFeature-134", "featureType": spec["featureType"], "name": feature_name,
               "namespace": spec["namespace"], "parameters": onshape_parameters(plist, values)}
    base = f"partstudios/d/{ws.did}/w/{ws.wid}/e/{ws.part}/features"
    r = None
    # In-document instances always track the latest Feature Studio code, so a stale instance
    # would keep generating geometry: update in place when the type matches, else replace it.
    if ws.feature_id and ws.feature_type == spec["featureType"]:
        try:
            r = o.post(f"{base}/featureid/{ws.feature_id}", {"feature": {**feature, "featureId": ws.feature_id}})
        except RateLimited:
            raise
        except OnshapeError:
            r = None
    if r is None:
        if ws.feature_id:
            try:
                o.delete(f"{base}/featureid/{ws.feature_id}")
            except RateLimited:
                raise
            except OnshapeError:
                pass
        r = o.post(base, {"feature": feature})
        ws.feature_id = r["feature"]["featureId"]
        ws.feature_type = spec["featureType"]
        ws.save()
    status = r["featureState"]["featureStatus"]
    return {"ok": status == "OK", "status": status, "featureId": ws.feature_id, "url": ws.url(o.base)}


def part_metrics(o: Onshape, ws: Workspace) -> dict:
    mp = o.get(f"partstudios/d/{ws.did}/w/{ws.wid}/e/{ws.part}/massproperties", params={"massAsGroup": "true"})
    parts = o.get(f"parts/d/{ws.did}/w/{ws.wid}/e/{ws.part}")
    bb = o.get(f"partstudios/d/{ws.did}/w/{ws.wid}/e/{ws.part}/boundingboxes")
    allb = mp["bodies"].get("-all-") or {}
    return {"bodies": len(parts), "volume_mm3": round(allb.get("volume", [0])[0] * 1e9, 4),
            "area_mm2": round(allb.get("periphery", [0])[0] * 1e6, 4),
            "bbox_mm": [round(bb[k] * 1000, 4) for k in ("lowX", "lowY", "lowZ", "highX", "highY", "highZ")]}


def export(o: Onshape, ws: Workspace, out_dir: Path, stem: str = "part", formats=("step", "stl")) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    base = f"partstudios/d/{ws.did}/w/{ws.wid}/e/{ws.part}"
    if "stl" in formats:
        r = o.get(f"{base}/stl", params={"units": "millimeter", "mode": "binary", "grouping": "true"},
                  raw=True, accept="*/*")
        p = out_dir / f"{stem}.stl"
        p.write_bytes(r.content)
        paths.append(p)
    for fmt_, ext in (("STEP", "step"), ("PARASOLID", "x_t")):
        if ext not in formats:
            continue
        t = o.post(f"{base}/translations", {"formatName": fmt_, "storeInDocument": False})
        # every status poll is a counted call: wait first, then back off (3, 6, 12, 24 s ...)
        delay = 3.0
        while True:
            time.sleep(delay)
            s = o.get(f"translations/{t['id']}")
            if s["requestState"] != "ACTIVE":
                break
            delay = min(delay * 2, 30.0)
        if s["requestState"] != "DONE":
            raise OnshapeError(f"{fmt_} translation failed: {s.get('failureReason')}")
        r = o.get(f"documents/d/{ws.did}/externaldata/{s['resultExternalDataIds'][0]}", raw=True, accept="*/*")
        p = out_dir / f"{stem}.{ext}"
        p.write_bytes(r.content)
        paths.append(p)
    return paths
