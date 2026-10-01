"""onshape_emit.py — emit a featuretree IR as a native Onshape feature tree.

Each IR feature becomes a native Part Studio feature (Sketch, Extrude, Fillet, Revolve, Plane)
posted through Onshape's REST API (v6 feature JSON), named after the IR feature. Every geometric
reference is written as a FeatureScript QUERY STRING that Onshape re-evaluates on each
regeneration: the sketch plane of a face-attached sketch is "the Z-normal planar face farthest
along +Z", a profile is "the sketch region containing this interior point", a fillet's edges are
"the circular edges containing these points". No transient or deterministic ids are stored, so
the tree stays editable in Onshape the way the FreeCAD tree does.

    python onshape_emit.py part.ir.json                # new Part Studio in the verification doc
    python onshape_emit.py part.ir.json --verify       # + export STEP and run the IoU gate
    python onshape_emit.py part.ir.json --trace        # + volume after every feature
    python onshape_emit.py --read <eid> part.ir.json   # read edited dimensions back into the IR

Coverage: sketches (circles / rects / polygons with bulge arcs) on XY, XZ or a top/bottom face;
pad (blind, symmetric, taper), pocket (through, blind, taper), fillet (top circles), revolve
about Z, prism_cut on axis-aligned planes. Not yet: polar_pocket and prism_cut on oblique planes
(they need rotated construction planes); these raise NotImplementedError. Uses the Onshape key
pair (onshape_client.py). Free plans make documents public.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "targets"))
import _geom as G  # noqa: E402
import onshape_client as oc  # noqa: E402

M = 0.001                                     # IR mm -> Onshape metres
PLANES = {"Top": 'qCreatedBy(makeId("Top"), EntityType.FACE)',
          "Front": 'qCreatedBy(makeId("Front"), EntityType.FACE)',
          "Right": 'qCreatedBy(makeId("Right"), EntityType.FACE)'}
SOLIDS = "qAllModifiableSolidBodies()"
EDGES = f"qOwnedByBody({SOLIDS}, EntityType.EDGE)"
CIRCLE_EDGES = f"qUnion([qGeometry({EDGES}, GeometryType.CIRCLE), qGeometry({EDGES}, GeometryType.ARC)])"
# Onshape's draft pull direction for an IR taper that SHRINKS the profile along the extrusion.
# Pinned by tests/test_onshape_emit.py (live) against build123d's volume.
DRAFT_PULL_FOR_SHRINK = True


def face_query(side):
    s = 1 if side == "top" else -1
    return (f"qNthElement(qFarthestAlong(qParallelPlanes(qOwnedByBody({SOLIDS}, EntityType.FACE), "
            f"vector(0, 0, 1), true), vector(0, 0, {s})), 0)")


def _vec(w):
    return f"vector({w[0] * M!r}, {w[1] * M!r}, {w[2] * M!r}) * meter"


def _qlist(pid, expr):
    return {"btType": "BTMParameterQueryList-148", "parameterId": pid,
            "queries": [{"btType": "BTMIndividualQuery-138", "queryString": f"query={expr};"}]}


def _enum(pid, enum, value):
    return {"btType": "BTMParameterEnum-145", "parameterId": pid, "enumName": enum, "value": value}


def _qty(pid, expr):
    return {"btType": "BTMParameterQuantity-147", "parameterId": pid, "expression": expr}


def _bool(pid, v):
    return {"btType": "BTMParameterBoolean-144", "parameterId": pid, "value": bool(v)}


def _flat(v):
    if isinstance(v, dict):
        if "value" in v:
            return _flat(v["value"])
        if "message" in v:
            return _flat(v["message"])
    if isinstance(v, list):
        return [_flat(x) for x in v]
    return v


def _interior_points(fr, loops):
    """One world-space point strictly inside the material region each INCLUDED loop bounds (the
    loop minus every loop nested in it). Onshape's sketch regions are then picked by containment."""
    from shapely.geometry import Polygon

    polys = [Polygon(G.loop_points(lp)).buffer(0) for lp in loops]
    out = []
    for i, lp in enumerate(loops):
        if not lp["include"]:
            continue
        region = polys[i]
        for j, other in enumerate(polys):
            if j != i and other.area < polys[i].area and polys[i].contains(other.representative_point()):
                region = region.difference(other)
        p = region.representative_point()
        out.append(G.to_world(fr, p.x, p.y))
    return out


_arc = G.arc_center


class Emitter:
    def __init__(self, did, wid, eid, trace=False):
        self.did, self.wid, self.eid, self.trace = did, wid, eid, trace
        self.base = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}"
        self.sketches = {}                    # IR name -> {"id", "frame", "z0", "side", "points"}
        self.ids = {}                         # IR name -> Onshape featureId
        self.has_solid = False
        self.rows = []
        self._fixed = {}

    # -- API helpers -----------------------------------------------------------------------------
    def fs(self, body):
        script = "function(context is Context, queries) {\n" + body + "\n}"
        r = oc.request("POST", self.base + "/featurescript", body={"script": script, "queries": {}})
        return _flat(r.get("result"))

    def frame(self, plane_expr):
        """The plane's frame (o, x, y, n) in mm, evaluated now, to map IR coords into sketch space.
        The three default planes never move, so theirs are cached."""
        if plane_expr in self._fixed:
            return self._fixed[plane_expr]
        fr = self._frame(plane_expr)
        if plane_expr in PLANES.values():
            self._fixed[plane_expr] = fr
        return fr

    def _frame(self, plane_expr):
        v = self.fs(f"var p = evPlane(context, {{\"face\": {plane_expr}}});\n"
                    "return [p.origin[0]/meter, p.origin[1]/meter, p.origin[2]/meter, "
                    "p.normal[0], p.normal[1], p.normal[2], p.x[0], p.x[1], p.x[2]];")
        o = (v[0] / M, v[1] / M, v[2] / M)
        return G.frame(o, v[3:6], v[6:9])

    def top_z(self):
        v = self.fs(f"var b = evBox3d(context, {{\"topology\": {SOLIDS}}});\nreturn b.maxCorner[2] / meter;")
        return v / M

    def add(self, feature):
        feature.setdefault("suppressed", False)
        r = oc.request("POST", self.base + "/features", body={"feature": feature})
        st = (r.get("featureState") or {}).get("featureStatus")
        if st not in ("OK", "INFO"):             # INFO: built, with an informational notice
            raise RuntimeError(f"Onshape feature '{feature['name']}' regenerated with status {st}")
        return r["feature"]["featureId"]

    def volume(self):
        mp = oc.request("GET", self.base + "/massproperties")
        b = (mp.get("bodies") or {}).get("-all-")
        return None if not b else b["volume"][0] / M ** 3

    # -- sketches --------------------------------------------------------------------------------
    @staticmethod
    def _local(fr, w):
        o, x, y, _ = fr
        d = (w[0] - o[0], w[1] - o[1], w[2] - o[2])
        return (G._dot(d, x) * M, G._dot(d, y) * M)

    def _entities(self, fr, loops, construction_z_axis=False):
        ents, k = [], 0
        for loop in loops:
            for s in loop["segments"]:
                k += 1
                eid = f"e{k}"
                if s[0] == "circle":
                    c = self._local(fr, G.to_world(fr, *s[1]))
                    ents.append({"btType": "BTMSketchCurve-4", "entityId": eid, "centerId": eid + ".c",
                                 "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": s[2] * M,
                                              "xCenter": c[0], "yCenter": c[1], "xDir": 1.0, "yDir": 0.0,
                                              "clockwise": False}})
                elif s[0] == "line":
                    a = self._local(fr, G.to_world(fr, *s[1]))
                    b = self._local(fr, G.to_world(fr, *s[2]))
                    L = math.dist(a, b)
                    ents.append({"btType": "BTMSketchCurveSegment-155", "entityId": eid,
                                 "startPointId": eid + ".s", "endPointId": eid + ".e", "startParam": 0.0, "endParam": L,
                                 "geometry": {"btType": "BTCurveGeometryLine-117", "pntX": a[0], "pntY": a[1],
                                              "dirX": (b[0] - a[0]) / L, "dirY": (b[1] - a[1]) / L}})
                else:
                    p1, mid, p2 = (self._local(fr, G.to_world(fr, *p)) for p in s[1:])
                    c, r, a1, sweep = _arc(p1, mid, p2)
                    start, end = (a1, a1 + sweep) if sweep > 0 else (a1 + sweep, a1)
                    ents.append({"btType": "BTMSketchCurveSegment-155", "entityId": eid, "centerId": eid + ".c",
                                 "startPointId": eid + ".s", "endPointId": eid + ".e",
                                 "startParam": start, "endParam": end,
                                 "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": r,
                                              "xCenter": c[0], "yCenter": c[1], "xDir": 1.0, "yDir": 0.0,
                                              "clockwise": False}})
        if construction_z_axis:                         # the revolve axis: model Z, 2 m long
            a, b = self._local(fr, (0, 0, -1000.0)), self._local(fr, (0, 0, 1000.0))
            L = math.dist(a, b)
            ents.append({"btType": "BTMSketchCurveSegment-155", "entityId": "zaxis", "isConstruction": True,
                         "startPointId": "zaxis.s", "endPointId": "zaxis.e", "startParam": 0.0, "endParam": L,
                         "geometry": {"btType": "BTCurveGeometryLine-117", "pntX": a[0], "pntY": a[1],
                                      "dirX": (b[0] - a[0]) / L, "dirY": (b[1] - a[1]) / L}})
        return ents

    def sketch(self, name, plane_expr, fr, loops, z0=0.0, side=None, axis=False):
        fid = self.add({"btType": "BTMSketch-151", "featureType": "newSketch", "name": name,
                        "parameters": [_qlist("sketchPlane", plane_expr), _bool("disableImprinting", True)],
                        "entities": self._entities(fr, loops, axis), "constraints": []})
        self.ids[name] = fid
        self.sketches[name] = {"id": fid, "frame": fr, "z0": z0, "side": side,
                               "points": _interior_points(fr, loops)}
        return fid

    def regions(self, sname):
        s = self.sketches[sname]
        qs = [f'qContainsPoint(qSketchRegion(makeId("{s["id"]}")), {_vec(p)})' for p in s["points"]]
        return qs[0] if len(qs) == 1 else "qUnion([" + ", ".join(qs) + "])"

    # -- solids ----------------------------------------------------------------------------------
    def extrude(self, name, sname, op, depth=None, world_dir=(0, 0, 1), through=False, symmetric=False, taper=0.0):
        n = self.sketches[sname]["frame"][3]
        params = [_enum("bodyType", "ExtendedToolBodyType", "SOLID"),
                  _enum("operationType", "NewBodyOperationType", op),
                  _qlist("entities", self.regions(sname))]
        if through:
            params += [_enum("endBound", "BoundingType", "THROUGH_ALL"), _bool("hasSecondDirection", True),
                       _enum("secondDirectionBound", "BoundingType", "THROUGH_ALL")]
        else:
            params += [_enum("endBound", "BoundingType", "BLIND"), _qty("depth", f"{depth!r} mm")]
            if symmetric:
                params.append(_bool("symmetric", True))
            else:
                params.append(_bool("oppositeDirection", G._dot(n, world_dir) < 0))
            if taper:
                params += [_bool("hasDraft", True), _qty("draftAngle", f"{abs(taper)!r} deg"),
                           _bool("draftPullDirection", DRAFT_PULL_FOR_SHRINK if taper > 0 else not DRAFT_PULL_FOR_SHRINK)]
        self.ids[name] = self.add({"btType": "BTMFeature-134", "featureType": "extrude", "name": name,
                                   "parameters": params})

    def solid_op(self):
        op = "ADD" if self.has_solid else "NEW"
        self.has_solid = True
        return op

    def placed_plane(self, name, fr):
        o, _, _, n = fr
        axis = max(range(3), key=lambda i: abs(n[i]))
        if abs(abs(n[axis]) - 1.0) > 1e-9:
            raise NotImplementedError(f"'{name}': a cut on an oblique plane is not in the Onshape backend yet")
        base = {2: "Top", 1: "Front", 0: "Right"}[axis]
        bfr = self.frame(PLANES[base])
        off = G._dot((o[0] - bfr[0][0], o[1] - bfr[0][1], o[2] - bfr[0][2]), bfr[3])
        fid = self.add({"btType": "BTMFeature-134", "featureType": "cPlane", "name": name + "_plane",
                        "parameters": [_qlist("entities", PLANES[base]), _enum("cplaneType", "CPlaneType", "OFFSET"),
                                       _qty("offset", f"{abs(off)!r} mm"), _bool("oppositeDirection", off < 0)]})
        return f'qCreatedBy(makeId("{fid}"), EntityType.FACE)'

    # -- IR features -----------------------------------------------------------------------------
    def f_sketch(self, f):
        loops = G.sketch_loops(f.get("circles", []), f.get("rects", []), f.get("polys", []))
        on = f.get("on")
        if on:
            side = on.get("side", "top")
            q = face_query(side)
            # IR coords are global XY at the face's height; map them through the face's own frame
            face = self.frame(q)
            ir = G.offset_xy(face[0][2])
            glob = [{"segments": [(s[0],) + tuple(self._w2(ir, face, p) for p in s[1:]) if s[0] != "circle"
                                  else ("circle", self._w2(ir, face, s[1]), s[2]) for s in lp["segments"]],
                     "include": lp["include"]} for lp in loops]
            self.sketch(f["name"], q, face, glob, z0=face[0][2], side=side)
        elif f.get("plane") == "XZ":
            self.sketch(f["name"], PLANES["Front"], self.frame(PLANES["Front"]), loops, axis=True)
        else:
            self.sketch(f["name"], PLANES["Top"], self.frame(PLANES["Top"]), loops)

    def f_pad(self, f):
        s = self.sketches[f["sketch"]]
        d = (0, 0, -1) if s["side"] == "bottom" else (0, 0, 1)
        self.extrude(f["name"], f["sketch"], self.solid_op(), depth=f["length"], world_dir=d,
                     symmetric=f.get("symmetric", False), taper=f.get("taper", 0.0))

    def f_pocket(self, f):
        s = self.sketches[f["sketch"]]
        if f["through"]:
            self.extrude(f["name"], f["sketch"], "REMOVE", through=True)
        else:
            d = (0, 0, -1) if s["z0"] >= self.top_z() - 1e-6 else (0, 0, 1)
            self.extrude(f["name"], f["sketch"], "REMOVE", depth=f["length"], world_dir=d, taper=f.get("taper", 0.0))

    def f_fillet(self, f):
        # The IR's rule (as b3d_emit / fc_common): circular edges whose centre is at the top of the
        # part; "top_outer" = the largest. Resolved here, stored as point-containment queries.
        v = self.fs(f"var zt = evBox3d(context, {{\"topology\": {SOLIDS}}}).maxCorner[2];\n"
                    f"var out = [];\nfor (var e in evaluateQuery(context, {CIRCLE_EDGES})) {{\n"
                    "  var c = evCurveDefinition(context, {\"edge\": e});\n"
                    "  if (abs(c.coordSystem.origin[2] - zt) < 1e-9 * meter) {\n"
                    "    var p = evEdgeTangentLine(context, {\"edge\": e, \"parameter\": 0.5}).origin;\n"
                    "    out = append(out, [c.radius/meter, p[0]/meter, p[1]/meter, p[2]/meter]);\n  }\n}\nreturn out;")
        cands = sorted(v or [], key=lambda t: -t[0])
        if f["select"].get("circles") == "top_outer":
            cands = cands[:1]
        if not cands:
            raise RuntimeError(f"fillet '{f['name']}' selected no edges")
        qs = [f"qContainsPoint({CIRCLE_EDGES}, {_vec((c[1] / M, c[2] / M, c[3] / M))})" for c in cands]
        q = qs[0] if len(qs) == 1 else "qUnion([" + ", ".join(qs) + "])"
        self.ids[f["name"]] = self.add({"btType": "BTMFeature-134", "featureType": "fillet", "name": f["name"],
                                        "parameters": [_qlist("entities", q), _qty("radius", f"{f['radius']!r} mm"),
                                                       _bool("tangentPropagation", False)]})

    def f_revolve(self, f):
        s = self.sketches[f["sketch"]]
        axis = f'qContainsPoint(qCreatedBy(makeId("{s["id"]}"), EntityType.EDGE), {_vec((0, 0, 900.0))})'
        ang = f.get("angle", 360.0)
        params = [_enum("bodyType", "ExtendedToolBodyType", "SOLID"),
                  _enum("operationType", "NewBodyOperationType", self.solid_op()),
                  _qlist("entities", self.regions(f["sketch"])), _qlist("axis", axis)]
        params += [_bool("fullRevolve", True)] if abs(ang - 360.0) < 1e-9 else \
            [_bool("fullRevolve", False), _qty("angle", f"{ang!r} deg")]
        self.ids[f["name"]] = self.add({"btType": "BTMFeature-134", "featureType": "revolve", "name": f["name"],
                                        "parameters": params})

    def f_prism_cut(self, f):
        fr = G.frame(f["origin"], f["normal"], f["xdir"])
        q = self.placed_plane(f["name"], fr)
        loops = G.sketch_loops(polys=f["polys"])
        # draw in the construction plane's OWN frame; loop coords are in the IR's (xdir, n×xdir) frame,
        # so map loop -> world with fr, then world -> sketch with the plane's frame
        pfr = self.frame(q)
        world = [{"segments": [(s[0],) + tuple(self._w2(fr, pfr, p) for p in s[1:]) if s[0] != "circle"
                               else ("circle", self._w2(fr, pfr, s[1]), s[2]) for s in lp["segments"]],
                  "include": lp["include"]} for lp in loops]
        self.sketch(f["name"] + "_sketch", q, pfr, world)
        self.extrude(f["name"], f["name"] + "_sketch", "REMOVE", depth=f["depth"], world_dir=fr[3],
                     taper=f.get("taper", 0.0))

    @staticmethod
    def _w2(fr, pfr, p):
        """IR plane coords (u, v) in frame fr -> the same point's coords in plane frame pfr."""
        w = G.to_world(fr, p[0], p[1])
        o, x, y, _ = pfr
        d = (w[0] - o[0], w[1] - o[1], w[2] - o[2])
        return (G._dot(d, x), G._dot(d, y))

    def f_polar_pocket(self, f):
        raise NotImplementedError(f"'{f['name']}': polar_pocket is not in the Onshape backend yet")

    def build(self, spec):
        for f in spec["features"]:
            row = {"name": f["name"], "kind": f["kind"]}
            try:
                getattr(self, "f_" + f["kind"])(f)
                row["ok"] = True
            except (RuntimeError, NotImplementedError) as e:
                row["ok"], row["error"] = False, f"{type(e).__name__}: {e}"
            if self.trace and f["kind"] != "sketch":
                row["volume"] = self.volume()
            self.rows.append(row)
            print(f"  {f['name']:28} {f['kind']:12} {'ok' if row['ok'] else row['error']}", flush=True)
        return self.rows


def new_partstudio(did, wid, name):
    return oc.request("POST", f"/api/partstudios/d/{did}/w/{wid}", body={"name": name})["id"]


def emit(spec, did=None, wid=None, trace=False):
    """Build `spec` as a new Part Studio. Returns (did, wid, eid, rows)."""
    if did is None:
        import onshape_translate
        did, wid = onshape_translate.verify_doc()
    eid = new_partstudio(did, wid, spec["name"])
    rows = Emitter(did, wid, eid, trace).build(spec)
    return did, wid, eid, rows


def read_params(did, wid, eid, spec):
    """Driving dimensions back out of the Part Studio, keyed by IR feature name."""
    feats = oc.request("GET", f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/features")["features"]
    names = {f["name"]: f for f in spec["features"]}
    keys = {"pad": ("depth", "length"), "pocket": ("depth", "length"), "prism_cut": ("depth", "depth"),
            "fillet": ("radius", "radius"), "revolve": ("angle", "angle")}
    out = {}
    for ft in feats:
        f = names.get(ft.get("name"))
        if not f or f["kind"] not in keys:
            continue
        pid, key = keys[f["kind"]]
        for p in ft.get("parameters", []):
            if p.get("parameterId") == pid and p.get("expression"):
                num, unit = p["expression"].split()[0], (p["expression"].split() + ["mm"])[1]
                val = float(num) * {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "deg": 1.0}.get(unit, 1.0)
                out[f["name"]] = {key: val}
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ir")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--read", metavar="EID")
    a = ap.parse_args()
    spec = json.loads(Path(a.ir).read_text())
    if a.read:
        import onshape_translate
        did, wid = onshape_translate.verify_doc()
        print(json.dumps(read_params(did, wid, a.read, spec), indent=1))
        return 0
    did, wid, eid, rows = emit(spec, trace=a.trace)
    if a.trace:
        import b3d_emit
        ref = b3d_emit.emit(spec)[1]["volumes"]
        for r, e in zip(rows, ref):
            if "volume" in r:
                ok = G.volume_check(r["volume"], e)
                print(f"  {r['name']:28} onshape {r['volume'] or 0:12.3f}  build123d {e or 0:12.3f}  "
                      f"{'' if ok else '<-- differs'}")
    url = f"https://cad.onshape.com/documents/{did}/w/{wid}/e/{eid}"
    n_ok = sum(r["ok"] for r in rows)
    print(f"{n_ok}/{len(rows)} features built: {url}")
    if a.verify:
        import cad_verify
        import onshape_translate
        step = onshape_translate.export_step(did, wid, eid, Path(a.ir).with_suffix(".onshape.step"))
        out = cad_verify.verify(step, spec, samples=8000)
        print(json.dumps(out, indent=1))
        return 0 if out["verdict"] == "VERIFIED" and n_ok == len(rows) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
