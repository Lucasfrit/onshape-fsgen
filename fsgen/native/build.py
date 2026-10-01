"""Translate traced Part Studio scripts into native Onshape features and sync them incrementally.

Each FeatureRec becomes one feature in the Part Studio (Variable, Sketch, Extrude, Fillet, ...).
Sketches get geometry plus constraints: coincidences, horizontal/vertical, and driving dimensions whose
values are Onshape expressions (so `#width` edits propagate). State is kept locally so re-pushing an
edited script only rebuilds from the first changed feature (listing features is rate limited).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..fslite.values import EnumVal, FSError, Id, Q, Vec, expr_of, has_expr, literal_expr, mag
from ..onshape import Onshape, OnshapeError, RateLimited
from .script import DEFAULT_FEATURES, SPECS, FeatureRec, QText, SketchRec, TraceResult, trace

STATE = Path(".fsgen_native.json")
ORIGIN_Q = 'query=qCreatedBy(makeId("Origin"), EntityType.VERTEX);'


# ------------------------------------------------------------------ parameter JSON
def _q(text: str) -> dict:
    return {"btType": "BTMIndividualQuery-138", "queryString": f"query={text};"}


def _quantity_expr(v) -> str:
    if isinstance(v, Q) or isinstance(v, (int, float)):
        return expr_of(v)
    raise FSError(f"not a quantity: {v!r}")


def param_json(ft: str, pid: str, v, ids: dict[str, str]) -> dict:
    return _param_json_spec(SPECS[ft]["params"][pid], pid, v, ids, ft)


def _param_json_spec(spec: dict, pid: str, v, ids: dict[str, str], ft: str = "") -> dict:
    t = spec["type"]
    if t == "Enum":
        return {"btType": "BTMParameterEnum-145", "parameterId": pid, "enumName": spec["enum"], "value": v.name}
    if t == "Quantity":
        return {"btType": "BTMParameterQuantity-147", "parameterId": pid, "expression": _quantity_expr(v)}
    if t == "Boolean":
        return {"btType": "BTMParameterBoolean-144", "parameterId": pid, "value": bool(v)}
    if t == "String":
        return {"btType": "BTMParameterString-149", "parameterId": pid, "value": v}
    if t == "Query":
        return {"btType": "BTMParameterQueryList-148", "parameterId": pid, "queries": [_q(v.resolve(ids))]}
    if t == "FeatureList":
        return {"btType": "BTMParameterFeatureList-1749", "parameterId": pid,
                "featureIds": [ids[".".join(x)] for x in v]}
    if t == "Array":
        items = []
        for item in v:
            given = {k: _param_json_spec(spec["items"][k], k, x, ids) for k, x in item.items()}
            params = [given.get(k) or dict(p["defaultJson"]) for k, p in spec["items"].items()
                      if k in given or "defaultJson" in p]
            items.append({"btType": "BTMArrayParameterItem-1843", "parameters": params})
        return {"btType": "BTMParameterArray-2025", "parameterId": pid, "items": items}
    raise FSError(f"{ft}.{pid}: parameter type {t} is not supported yet")


def onshape_name(rec: FeatureRec) -> str:
    # Variables use the UI's name template ("#width = 60 mm" in the tree); a literal "#width" would be read
    # as a reference to a parameter called "width" and show as "?".
    if rec.feature_type == "assignVariable":
        return SPECS["assignVariable"].get("nameTemplate") or "###name = #value"
    return rec.name


# ------------------------------------------------------------------ sketches
def _pos_expr(v) -> str:
    """Unsigned distance expression for a signed coordinate value."""
    if mag(v) >= 0:
        return expr_of(v)
    e = expr_of(v)
    if has_expr(v):
        if e.startswith("-(") and e.endswith(")"):
            return e[2:-1]
        if e.startswith("-") and " " not in e:
            return e[1:]
        return f"-({e})"
    return literal_expr(Q(-v.v, v.dims))


def _S(pid, v):
    return {"btType": "BTMParameterString-149", "parameterId": pid, "value": v}


def _C(cid, ctype, *params):
    return {"btType": "BTMSketchConstraint-2", "constraintType": ctype, "entityId": cid, "parameters": list(params)}


def sketch_json(sk: SketchRec, ids: dict[str, str], with_dims: bool = True) -> dict:
    ents, points = [], []  # points: (point id, x, y)
    for e in sk.entities:
        g = e.geom
        if e.kind == "line":
            x0, y0 = g["p0"].a
            x1, y1 = g["p1"].a
            L = math.hypot(x1 - x0, y1 - y0)
            ents.append({"btType": "BTMSketchCurveSegment-155", "entityId": e.name, "isConstruction": e.construction,
                         "startPointId": f"{e.name}.start", "endPointId": f"{e.name}.end",
                         "startParam": 0.0, "endParam": L,
                         "geometry": {"btType": "BTCurveGeometryLine-117", "pntX": x0, "pntY": y0,
                                      "dirX": (x1 - x0) / L, "dirY": (y1 - y0) / L}})
            points += [(f"{e.name}.start", x0, y0), (f"{e.name}.end", x1, y1)]
        elif e.kind == "circle":
            cx, cy = g["c"].a
            ents.append({"btType": "BTMSketchCurve-4", "entityId": e.name, "centerId": f"{e.name}.center",
                         "isConstruction": e.construction,
                         "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": g["r"].v, "xCenter": cx,
                                      "yCenter": cy, "xDir": 1.0, "yDir": 0.0, "clockwise": False}})
            points.append((f"{e.name}.center", cx, cy))
        elif e.kind == "arc":
            ents.append({"btType": "BTMSketchCurveSegment-155", "entityId": e.name, "isConstruction": e.construction,
                         "startPointId": f"{e.name}.start", "endPointId": f"{e.name}.end", "centerId": f"{e.name}.center",
                         "startParam": g["t0"], "endParam": g["t1"],
                         "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": g["r"], "xCenter": g["cx"],
                                      "yCenter": g["cy"], "xDir": 1.0, "yDir": 0.0, "clockwise": False}})
            for pid, t in ((f"{e.name}.start", g["t0"]), (f"{e.name}.end", g["t1"])):
                points.append((pid, g["cx"] + g["r"] * math.cos(t), g["cy"] + g["r"] * math.sin(t)))
            points.append((f"{e.name}.center", g["cx"], g["cy"]))
    cons = []
    # coincident points -> COINCIDENT chains
    groups: list[list] = []
    for p in points:
        for gr in groups:
            if math.hypot(gr[0][1] - p[1], gr[0][2] - p[2]) < 1e-9:
                gr.append(p)
                break
        else:
            groups.append([p])
    group_of = {}
    for gi, gr in enumerate(groups):
        for a, b in zip(gr, gr[1:]):
            cons.append(_C(f"co_{a[0]}_{b[0]}", "COINCIDENT", _S("localFirst", a[0]), _S("localSecond", b[0])))
        for p in gr:
            group_of[p[0]] = gi
    on_default = sk.plane_name is not None
    origin = {"btType": "BTMParameterQueryList-148", "parameterId": "externalSecond", "queries": [{
        "btType": "BTMIndividualQuery-138", "queryString": ORIGIN_Q}]}
    fixed_groups = set()
    if on_default:
        for gi, gr in enumerate(groups):
            if math.hypot(gr[0][1], gr[0][2]) < 1e-9:
                cons.append(_C(f"origin_{gr[0][0]}", "COINCIDENT", _S("localFirst", gr[0][0]), origin))
                fixed_groups.add(gi)
    if with_dims:
        rect_lines = {d["entity"] for d in sk.dims if d["type"] in ("HORIZONTAL", "VERTICAL")}
        for e in sk.entities:  # axis-aligned free lines: add H/V
            if e.kind == "line" and e.name not in rect_lines:
                dx, dy = (e.geom["p1"].a - e.geom["p0"].a)
                if abs(dy) < 1e-12:
                    cons.append(_C(f"h_{e.name}", "HORIZONTAL", _S("localFirst", e.name)))
                elif abs(dx) < 1e-12:
                    cons.append(_C(f"v_{e.name}", "VERTICAL", _S("localFirst", e.name)))
        for i, d in enumerate(sk.dims):
            t = d["type"]
            if t in ("HORIZONTAL", "VERTICAL"):
                cons.append(_C(f"{t[0].lower()}_{d['entity']}", t, _S("localFirst", d["entity"])))
            elif t == "DISTANCE":
                cons.append(_C(f"dim{i}", "DISTANCE", _S("localFirst", d["a"]), _S("localSecond", d["b"]),
                               {"btType": "BTMParameterEnum-145", "parameterId": "direction",
                                "enumName": "DimensionDirection", "value": d["dir"]},
                               {"btType": "BTMParameterQuantity-147", "parameterId": "length",
                                "expression": _pos_expr(d["value"])}))
            elif t == "DIAMETER":
                cons.append(_C(f"dim{i}", "DIAMETER", _S("localFirst", d["entity"]),
                               {"btType": "BTMParameterQuantity-147", "parameterId": "length",
                                "expression": expr_of(d["value"])}))
            elif t == "POSITION" and on_default:
                gi = group_of.get(d["point"])
                if gi is None or gi in fixed_groups:
                    continue
                fixed_groups.add(gi)
                for axis, v, dirname, align in (("x", d["x"], "HORIZONTAL", "VERTICAL"),
                                                ("y", d["y"], "VERTICAL", "HORIZONTAL")):
                    if abs(mag(v)) < 1e-12 and not has_expr(v):
                        # on the axis: align with the origin instead of a zero dimension
                        cons.append(_C(f"pos{i}{axis}", align, _S("localFirst", d["point"]), origin))
                    else:
                        cons.append(_C(f"pos{i}{axis}", "DISTANCE", _S("localFirst", d["point"]), origin,
                                       {"btType": "BTMParameterEnum-145", "parameterId": "direction",
                                        "enumName": "DimensionDirection", "value": dirname},
                                       {"btType": "BTMParameterQuantity-147", "parameterId": "length",
                                        "expression": _pos_expr(v)}))
    # entities whose geometry is fully determined by generated dimensions: Onshape re-solves them when a
    # variable changes, so their numeric geometry need not trigger a (billable) re-send of the sketch
    driven = []
    if with_dims and on_default:
        dim_ents = {d.get("entity") for d in sk.dims if d["type"] == "DIAMETER"}
        for e in sk.entities:
            if e.kind == "circle" and e.name in dim_ents and group_of.get(f"{e.name}.center") in fixed_groups:
                driven.append(e.name)
            elif e.kind == "line" and "." in e.name:
                rect = e.name.rsplit(".", 1)[0]
                if group_of.get(f"{rect}.bottom.start") in fixed_groups and any(
                        d.get("a") == f"{rect}.bottom.start" for d in sk.dims):
                    driven.append(e.name)
    return {"_driven": driven, "btType": "BTMSketch-151", "featureType": "newSketch", "name": sk.name,
            "parameters": [{"btType": "BTMParameterQueryList-148", "parameterId": "sketchPlane",
                            "queries": [_q(sk.plane.resolve(ids))]}],
            "entities": ents, "constraints": cons}


def feature_json(rec: FeatureRec, ids: dict[str, str], with_dims: bool = True) -> dict:
    if rec.sketch is not None:
        return sketch_json(rec.sketch, ids, with_dims)
    # Send the complete parameter set like the Onshape UI does: some features (e.g. cPlane without
    # "cplaneType") fail to regenerate when a parameter is omitted, even if it has a default.
    spec = SPECS[rec.feature_type]["params"]
    given = {k: param_json(rec.feature_type, k, v, ids) for k, v in rec.params.items()}
    params = [given.get(k) or dict(p["defaultJson"]) for k, p in spec.items() if k in given or "defaultJson" in p]
    return {"btType": "BTMFeature-134", "featureType": rec.feature_type, "name": onshape_name(rec), "parameters": params}


def feature_hash(rec: FeatureRec) -> str:
    j = feature_json(rec, _Placeholders())
    driven = set(j.pop("_driven", []))
    for e in j.get("entities", []):
        if e["entityId"] in driven:
            for k in ("geometry", "startParam", "endParam"):
                e.pop(k, None)
    return hashlib.sha1(json.dumps(j, sort_keys=True).encode()).hexdigest()[:16]


class _Placeholders(dict):
    def get(self, k, default=None):
        return f"<{k}>"

    def __getitem__(self, k):
        return f"<{k}>"


# ------------------------------------------------------------------ Onshape sync
@dataclass
class StudioState:
    did: str
    wid: str
    eid: str
    name: str
    features: list[dict] = field(default_factory=list)  # {name, hash, fid, status}
    vs_eid: str | None = None  # Variable Studio element holding this Part Studio's variables
    vs_hash: str | None = None
    vs_referenced: bool = False

    def url(self, base="https://cad.onshape.com"):
        return f"{base}/documents/{self.did}/w/{self.wid}/e/{self.eid}"


def load_states(path: Path = STATE) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(st: StudioState, path: Path = STATE):
    states = load_states(path)
    states[st.name] = st.__dict__
    path.write_text(json.dumps(states, indent=2))


def get_studio(o: Onshape, name: str, did: str, wid: str, fresh: bool = False) -> StudioState:
    states = load_states()
    if name in states and not fresh:
        return StudioState(**states[name])
    eid = o.post(f"partstudios/d/{did}/w/{wid}", {"name": name})["id"]
    st = StudioState(did, wid, eid, name)
    save_state(st)
    return st


@dataclass
class PushResult:
    ok: bool
    built: int = 0
    reused: int = 0
    failed: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    url: str = ""


def diagnose(o: Onshape, st: StudioState, fid: str, rec: FeatureRec, ids: dict) -> str:
    """One evalFeatureScript call: feature error + how many entities each query parameter matches."""
    qparts = []
    for k, v in rec.params.items():
        if isinstance(v, QText):
            qparts.append(f'try {{ out["{k}"] = size(evaluateQuery(context, {v.resolve(ids)})); }} '
                          f'catch (e) {{ out["{k}"] = "query error"; }}')
    script = ("function(context is Context, queries) { var out = {}; "
              f'out["__error"] = getFeatureError(context, makeId("{fid}")); '
              f'out["__status"] = toString(getFeatureStatus(context, makeId("{fid}"))); '
              + " ".join(qparts) + " return out; }")
    try:
        r = o.post(f"partstudios/d/{st.did}/w/{st.wid}/e/{st.eid}/featurescript", {"script": script})
    except RateLimited as e:
        return f"(no diagnostics: {e})"
    from ..cloud import _simplify
    out = _simplify(r.get("result")) or {}
    msg = [f"Onshape error: {out.get('__error')}", f"status: {out.get('__status')}"]
    counts = [f"{k} matched {int(v) if isinstance(v, float) else v} entities" for k, v in out.items()
              if not k.startswith("__")]
    if counts:
        msg.append("query check: " + "; ".join(counts))
    return "\n".join(msg)


def check_statuses(o: Onshape, st: StudioState, fids: list[str]) -> dict[str, str]:
    """Statuses of many features in ONE call (instead of listing features, which is rate limited)."""
    ids = ", ".join(f'"{f}"' for f in fids)
    script = ("function(context is Context, queries) { var out = {}; for (var f in [" + ids + "]) { "
              "out[f] = toString(getFeatureStatus(context, makeId(f)).statusType); } return out; }")
    r = o.post(f"partstudios/d/{st.did}/w/{st.wid}/e/{st.eid}/featurescript", {"script": script})
    from ..cloud import _simplify
    return {k: str(v) for k, v in (_simplify(r.get("result")) or {}).items()}


def sync_variable_studio(o: Onshape, st: StudioState, variables: list[dict], log=print) -> int:
    """Put all variables in one Variable Studio referenced by the Part Studio.

    First time: create (1) + set variables (1) + reference (1) = 3 calls for any number of variables.
    Later: 0 calls if unchanged, 1 call (set variables) if any value changed.
    """
    if not variables:
        return 0
    before = o.calls
    h = hashlib.sha1(json.dumps(variables, sort_keys=True).encode()).hexdigest()[:16]
    if st.vs_eid is None:
        st.vs_eid = o.post(f"variables/d/{st.did}/w/{st.wid}/variablestudio", {"name": f"{st.name} variables"})["id"]
        save_state(st)
    if st.vs_hash != h:
        o.post(f"variables/d/{st.did}/w/{st.wid}/e/{st.vs_eid}/variables", variables)
        st.vs_hash = h
        save_state(st)
        log(f"  OK      variableStudio   {len(variables)} variables")
    if not st.vs_referenced:
        # same document: omit referenceDocumentId (sending it gives a 500)
        o.post(f"variables/d/{st.did}/w/{st.wid}/e/{st.eid}/variablestudioreferences", {"references": [{
            "referenceElementId": st.vs_eid, "entireVariableStudio": True}]})
        st.vs_referenced = True
        save_state(st)
    return o.calls - before


def push_trace(o: Onshape, st: StudioState, tr: TraceResult, log=print, diagnose_errors: bool = True) -> PushResult:
    """Sync the traced features into the Part Studio with as few API calls as possible.

    Per position: identical feature (same hash, built OK) -> 0 calls; same type but changed -> 1 in-place
    update; type/order changed -> delete the old tail and add the rest (1 call each). Downstream
    features are not re-sent: they reference upstream features by id and Onshape regenerates them.
    """
    base = f"partstudios/d/{st.did}/w/{st.wid}/e/{st.eid}/features"
    vars_changed = st.vs_hash is not None and sync_variable_studio(o, st, tr.variable_list, log) > 0
    if st.vs_hash is None:
        sync_variable_studio(o, st, tr.variable_list, log)
    hashes = [feature_hash(f) for f in tr.features]
    old = list(st.features)
    # first position where the tree structure (feature type) diverges
    split = 0
    def old_type(e, rec):  # state written before "type" was recorded: infer from the matching name
        return e.get("type") or (rec.feature_type if e["name"] == rec.name else None)
    while split < min(len(old), len(hashes)) and old_type(old[split], tr.features[split]) == tr.features[split].feature_type:
        split += 1
    for gone in reversed(old[split:]):
        try:
            o.delete(f"{base}/featureid/{gone['fid']}")
        except RateLimited:
            raise
        except OnshapeError:
            pass
    st.features = old[:split]
    save_state(st)
    ids = {f["name"]: f["fid"] for f in st.features}
    for rec, entry in zip(tr.features, st.features):
        ids[rec.name] = entry["fid"]
    res = PushResult(False, url=st.url(o.base))
    updated_at = None
    reused_after_update: list[str] = []  # features kept as-is downstream of an in-place update

    def send(rec, fid=None, with_dims=True):
        body = feature_json(rec, ids, with_dims)
        body.pop("_driven", None)
        if fid:
            r = o.post(f"{base}/featureid/{fid}", {"feature": {**body, "featureId": fid}})
        else:
            r = o.post(base, {"feature": body})
        return r["feature"]["featureId"], r["featureState"]["featureStatus"]

    for i, (rec, h) in enumerate(zip(tr.features, hashes)):
        existing = st.features[i] if i < len(st.features) else None
        if existing and existing["hash"] == h and existing.get("status") in ("OK", "WARNING", "INFO"):
            res.reused += 1
            if updated_at is not None:
                reused_after_update.append(existing["fid"])
            continue
        fid, status = send(rec, existing["fid"] if existing else None)
        if existing:
            updated_at = i if updated_at is None else updated_at
        if status != "OK" and rec.sketch is not None:
            # keep the geometry, drop driving dimensions (in place: 1 call)
            fid, status2 = send(rec, fid, with_dims=False)
            res.warnings.append(f"sketch \"{rec.name}\": constraints gave {status}, kept geometry without "
                                f"dimensions (now {status2})")
            status = status2
        log(f"  {status:7s} {rec.feature_type:16s} {rec.name}" + ("  (updated)" if existing else ""))
        ids[rec.name] = fid
        entry = {"name": rec.name, "type": rec.feature_type, "hash": h, "fid": fid, "status": status}
        if existing:
            st.features[i] = entry
        else:
            st.features.append(entry)
        save_state(st)
        if status == "ERROR":
            # leave the failed feature in place: the next push updates it (1 call) instead of delete + add
            res.failed = rec.name
            res.error = f"feature \"{rec.name}\" ({rec.feature_type}, script line {rec.line}) failed in Onshape."
            if diagnose_errors:
                res.error += "\n" + diagnose(o, st, fid, rec, ids)
            return res
        if status != "OK":
            hint = ""
            if status == "INFO" and rec.params.get("operationType") is not None:
                hint = (" (Onshape INFO on a boolean feature usually means it did nothing: e.g. a REMOVE extrude "
                        "pointing away from the part, or a tool that does not touch the booleanScope)")
            res.warnings.append(f"feature \"{rec.name}\" (line {rec.line}) finished with status {status}{hint}")
        res.built += 1
    # an in-place update or a variable change can break reused features; check them all in one call
    if vars_changed and updated_at is None:
        reused_after_update = [e["fid"] for e in st.features[:res.reused]]
    if reused_after_update:
        if True:
            stats = check_statuses(o, st, reused_after_update)
            for e in st.features:
                if e["fid"] in stats and stats[e["fid"]] not in ("OK", "INFO", "WARNING"):
                    e["status"] = stats[e["fid"]]
                    res.failed = res.failed or e["name"]
                    res.error = (res.error or "") + f"feature \"{e['name']}\" is now {stats[e['fid']]} after an upstream change.\n"
            save_state(st)
            if res.failed:
                return res
    res.ok = True
    return res


def studio_metrics(o: Onshape, st: StudioState) -> dict:
    """Parts, volume and area in ONE call (massproperties per part)."""
    mp = o.get(f"partstudios/d/{st.did}/w/{st.wid}/e/{st.eid}/massproperties", params={"massAsGroup": "false"})
    bodies = mp.get("bodies") or {}
    parts = {k: v for k, v in bodies.items() if k != "-all-"}
    return {"parts": len(parts),
            "volume_mm3": round(sum(v.get("volume", [0])[0] for v in parts.values()) * 1e9, 3),
            "area_mm2": round(sum(v.get("periphery", [0])[0] for v in parts.values()) * 1e6, 3)}


def shaded_view(o: Onshape, st: StudioState, path: Path, size: int = 600) -> Path | None:
    """Onshape-rendered isometric image of the Part Studio (what the user will see)."""
    try:
        r = o.get(f"partstudios/d/{st.did}/w/{st.wid}/e/{st.eid}/shadedviews",
                  params={"viewMatrix": "isometric", "outputHeight": size, "outputWidth": size, "pixelSize": 0,
                          "edges": "show"})
    except OnshapeError:
        return None
    imgs = r.get("images") or []
    if not imgs:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(imgs[0]))
    return path


def export_studio(o: Onshape, st: StudioState, out_dir: Path, stem: str, formats=("step", "stl")) -> list[Path]:
    from ..cloud import Workspace, export
    ws = Workspace(st.did, st.wid, "", st.eid, "")
    return export(o, ws, out_dir, stem, formats=tuple(formats))
