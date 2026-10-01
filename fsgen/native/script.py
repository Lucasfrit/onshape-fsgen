"""Part Studio scripts: FeatureScript that builds a *native* Onshape feature tree.

A script looks like Onshape's own Part Studio code: a `build(context, id)` function calling standard
features (newSketch, extrude, fillet, ...) with their real parameter ids, plus `variable(...)` and
`#name` references. Running it with the tracer does not build geometry; it records one entry per
feature. `#variables` flow through arithmetic, so a parameter computed as `#width / 2` stays a live
Onshape expression in the generated feature (and sketch dimensions are driven by the variables).

    FeatureScript 3083;
    import(path : "onshape/std/common.fs", version : "3083.0");

    export function build(context is Context, id is Id)
    {
        variable(context, "width", 60 * millimeter);
        var sk = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
        skRectangle(sk, "outline", { "firstCorner" : vector(0, 0) * millimeter, "secondCorner" : vector(#width, 40 * millimeter) });
        skSolve(sk);
        extrude(context, id + "Base plate", { "entities" : qSketchRegion(id + "Base sketch"),
                "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter });
    }
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..fslite.interp import Builtin, Function, Interpreter
from ..fslite.lexer import FSSyntaxError
from ..fslite.stdlib import StdLib
from ..fslite.values import (EnumType, EnumVal, FSError, Id, Line, Plane, Q, Vec, dims_of, expr_of, fmt, has_expr, mag, mk,
                             type_name)

SPECS = json.loads((Path(__file__).with_name("featurespecs.json")).read_text())
DEFAULT_FEATURES = {"Origin", "Top", "Front", "Right"}
# sketch (u, v) axes of the default planes in world coordinates (measured in Onshape)
DEFAULT_PLANES = {"Top": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                  "Front": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
                  "Right": ((0, 1, 0), (0, 0, 1), (1, 0, 0))}
# Features the LLM may use. Anything in SPECS works in principle; this list is what we document/test.
SUPPORTED = ["assignVariable", "newSketch", "extrude", "revolve", "fillet", "chamfer", "cPlane", "linearPattern",
             "circularPattern", "mirror", "shell", "booleanBodies", "deleteBodies", "sweep", "draft", "thicken",
             "transform", "splitPart"]


CHECKED_TYPES = {"Enum", "Quantity", "Boolean", "Query", "String", "FeatureList", "Array"}


class QText:
    """A FeatureScript query expression kept as text; feature names are resolved to ids at push time."""

    def __init__(self, text: str, refs: set[str]):
        self.text, self.refs = text, refs

    def resolve(self, ids: dict[str, str]) -> str:
        out = self.text
        for name in self.refs:
            out = out.replace(f"@@{name}@@", ids.get(name, name))
        return out

    def __repr__(self):
        return f"QText({self.text})"


def fs_text(v, refs: set) -> str:
    """FeatureScript source text for a value used inside a query expression."""
    if isinstance(v, QText):
        refs |= v.refs
        return v.text
    if isinstance(v, Id):
        name = ".".join(v)
        if name not in DEFAULT_FEATURES:
            refs.add(name)
            return f'makeId("@@{name}@@")'
        return f'makeId("{name}")'
    if isinstance(v, EnumVal):
        return f"{v.enum}.{v.name}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (int, float)):
        return repr(float(v))
    if isinstance(v, Q):
        if v.dims == (1, 0):
            return f"{v.v * 1000!r} * millimeter"
        if v.dims == (0, 1):
            return f"{math.degrees(v.v)!r} * degree"
        if v.dims == (0, 0):
            return repr(v.v)
    if isinstance(v, Vec):
        comps = ", ".join(repr(float(x * (1000 if v.dims == (1, 0) else 1))) for x in v.a)
        return f"vector({comps})" + (" * millimeter" if v.dims == (1, 0) else "")
    if isinstance(v, list):
        return "[" + ", ".join(fs_text(x, refs) for x in v) + "]"
    if isinstance(v, Plane):
        return (f"plane({fs_text(Vec(v.origin, (1, 0)), refs)}, {fs_text(Vec(v.normal), refs)}, "
                f"{fs_text(Vec(v.x), refs)})")
    if isinstance(v, Line):
        return f"line({fs_text(Vec(v.origin, (1, 0)), refs)}, {fs_text(Vec(v.direction), refs)})"
    if v is None:
        return "undefined"
    raise FSError(f"cannot use {type_name(v)} inside a query")


@dataclass
class SketchEntity:
    kind: str  # line | circle | arc
    name: str
    geom: dict  # numeric, sketch coords in meters
    construction: bool = False


@dataclass
class SketchRec:
    name: str
    plane: QText
    plane_name: str | None  # default plane name when sketching on Top/Front/Right
    entities: list[SketchEntity] = field(default_factory=list)
    dims: list[dict] = field(default_factory=list)  # requested driving dimensions
    solved: bool = False


@dataclass
class FeatureRec:
    feature_type: str
    name: str
    params: dict  # parameterId -> python value (Q, EnumVal, QText, bool, str, list of Id ...)
    sketch: SketchRec | None = None
    line: int = 0


@dataclass
class TraceResult:
    ok: bool
    features: list[FeatureRec] = field(default_factory=list)
    variable_list: list[dict] = field(default_factory=list)  # Variable Studio mode
    variables: dict = field(default_factory=dict)
    error: str | None = None
    error_line: int = 0
    output: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ tracer
class Tracer:
    def __init__(self, variables_mode: str = "studio"):
        self.lib = StdLib()
        self.variables_mode = variables_mode  # "studio": one Variable Studio; "tree": Variable features
        self.variable_list: list[dict] = []
        self.features: list[FeatureRec] = []
        self.names: set[str] = set(DEFAULT_FEATURES)
        self.current_line = 0
        self._install()
        self.interp = Interpreter(self.lib.b)
        self.lib.interp = self.interp

    # -- helpers
    def feature_name(self, fid, fname) -> str:
        if not isinstance(fid, Id) or not fid:
            raise FSError(f"{fname}: second argument must be a feature id like id + \"Base plate\"")
        name = ".".join(fid)
        if "#" in name:
            raise FSError(f"{fname}: feature names must not contain '#' (Onshape reads #x in a name as a parameter)")
        if name in self.names:
            raise FSError(f"{fname}: feature name \"{name}\" is already used; every feature needs a unique name")
        return name

    def add(self, rec: FeatureRec):
        self.names.add(rec.name)
        self.features.append(rec)

    def _install(self):
        b = self.lib.b
        tr = self

        def fn(name):
            def deco(f):
                b[name] = Builtin(name, f)
                return f
            return deco

        # ids
        b["makeId"] = Builtin("makeId", lambda s: Id((s,)))

        # queries become text
        def make_query_fn(qname):
            def q(*args):
                refs: set = set()
                return QText(f"{qname}({', '.join(fs_text(a, refs) for a in args)})", refs)
            return q

        for qname in [k for k in list(b) if k.startswith("q")] + [
                "qCreatedBy", "qSketchRegion", "qUnion", "qSubtraction", "qIntersection", "qEverything",
                "qAllModifiableSolidBodies", "qOwnedByBody", "qOwnerBody", "qAdjacent", "qEntityFilter", "qBodyType",
                "qGeometry", "qCoincidesWithPlane", "qContainsPoint", "qClosestTo", "qFarthestAlong", "qLargest",
                "qSmallest", "qParallelEdges", "qNthElement", "qNothing", "qCapEntity", "qNonCapEntity",
                "qSketchFilter", "qConstructionFilter", "qVertexAdjacent", "qEdgeAdjacent", "qLoopEdges",
                "qTangentConnectedFaces", "qConvexConnectedFaces", "qConcaveConnectedFaces", "qFaceOrEdgeBoundedFaces",
                "sketchEntityQuery", "qSketchEntity"]:
            b[qname] = Builtin(qname, make_query_fn(qname))
        for name in ("CapType", "SketchObject", "ConstructionObject"):
            b[name] = EnumType(name, {"CapType": ["START", "END", "EITHER"], "SketchObject": ["YES", "NO"],
                                      "ConstructionObject": ["YES", "NO"]}[name])
        # enums referenced by standard features (incl. array item parameters)
        def add_enums(params):
            for p in params.values():
                if p["type"] == "Enum" and p.get("enum") and p["enum"] not in b:
                    b[p["enum"]] = EnumType(p["enum"], p["options"])
                if p["type"] == "Array":
                    add_enums(p["items"])
        for ft in SPECS:
            add_enums(SPECS[ft]["params"])

        @fn("variable")
        def _variable(ctx, name, value, description=""):
            if not isinstance(name, str) or not name.isidentifier():
                raise FSError("variable: name must be an identifier string like \"width\"")
            if name in tr.interp.variables:
                raise FSError(f"variable #{name} is already defined")
            d = dims_of(value)
            vtype = {(1, 0): "LENGTH", (0, 1): "ANGLE", (0, 0): "NUMBER"}.get(d)
            if vtype is None:
                raise FSError(f"variable #{name}: value must be a length, angle or number, got {fmt(value)}")
            fname = f"#{name}"
            if tr.variables_mode == "studio":
                # collected for one Variable Studio (3 API calls total) instead of one feature per variable
                tr.variable_list.append({"name": name, "expression": expr_of(value), "type": vtype,
                                         "description": description or ""})
                tr.names.add(fname)
                tr.interp.variables[name] = mk(mag(value), d)
                return mk(mag(value), d, f"#{name}")
            tr.add(FeatureRec("assignVariable", fname, {
                "mode": EnumVal("VariableMode", "ASSIGNED"),
                "variableType": EnumVal("VariableType", vtype), "name": name, "value": value,
                {"LENGTH": "lengthValue", "ANGLE": "angleValue", "NUMBER": "numberValue"}[vtype]: value,
                **({"description": description} if description else {})}, line=tr.current_line))
            tr.interp.variables[name] = mk(mag(value), d)
            return mk(mag(value), d, f"#{name}")

        @fn("newSketch")
        def _new_sketch(ctx, fid, m):
            name = tr.feature_name(fid, "newSketch")
            if not isinstance(m, dict) or not isinstance(m.get("sketchPlane"), QText):
                raise FSError("newSketch: \"sketchPlane\" must be a query, e.g. qCreatedBy(makeId(\"Top\"), EntityType.FACE) "
                              "or qCreatedBy(id + \"Offset plane\", EntityType.FACE)")
            unknown = set(m) - {"sketchPlane", "disableImprinting"}
            if unknown:
                raise FSError(f"newSketch: unknown parameter(s) {sorted(unknown)}")
            pq = m["sketchPlane"]
            plane_name = None
            for p in DEFAULT_PLANES:
                if pq.text == f'qCreatedBy(makeId("{p}"), EntityType.FACE)':
                    plane_name = p
            sk = SketchRec(name, pq, plane_name)
            rec = FeatureRec("newSketch", name, {"sketchPlane": pq}, sketch=sk, line=tr.current_line)
            if "disableImprinting" in m:
                rec.params["disableImprinting"] = m["disableImprinting"]
            tr.add(rec)
            return sk

        def _sk(sk, ename, fname):
            if not isinstance(sk, SketchRec):
                raise FSError(f"{fname}: first argument must be a sketch returned by newSketch")
            if sk.solved:
                raise FSError(f"{fname}: sketch \"{sk.name}\" was already solved")
            if not isinstance(ename, str) or not ename.replace("_", "").isalnum():
                raise FSError(f"{fname}: entity id must be an alphanumeric string like \"outline\"")
            if any(e.name == ename or e.name.startswith(ename + ".") for e in sk.entities):
                raise FSError(f"{fname}: duplicate sketch entity id \"{ename}\"")
            return sk

        def pt2(v, what):
            if not isinstance(v, Vec) or len(v) != 2 or v.dims != (1, 0):
                raise FSError(f"{what} must be a 2D point like vector(x, y) * millimeter, got {fmt(v)}")
            return v

        def length(v, what):
            if not isinstance(v, Q) or v.dims != (1, 0):
                raise FSError(f"{what} must be a length, got {fmt(v)}")
            return v

        def constr(m):
            return bool(m.get("construction", False))

        @fn("skLineSegment")
        def _line(sk, ename, m):
            sk = _sk(sk, ename, "skLineSegment")
            a, c = pt2(m.get("start"), "skLineSegment start"), pt2(m.get("end"), "skLineSegment end")
            if np.linalg.norm(a.a - c.a) < 1e-9:
                raise FSError("skLineSegment: zero length")
            sk.entities.append(SketchEntity("line", ename, {"p0": a, "p1": c}, constr(m)))

        @fn("skRectangle")
        def _rect(sk, ename, m):
            sk = _sk(sk, ename, "skRectangle")
            a, c = pt2(m.get("firstCorner"), "firstCorner"), pt2(m.get("secondCorner"), "secondCorner")
            x0, y0 = a.item(0), a.item(1)
            x1, y1 = c.item(0), c.item(1)
            if abs(mag(x0) - mag(x1)) < 1e-9 or abs(mag(y0) - mag(y1)) < 1e-9:
                raise FSError("skRectangle: corners must differ in x and y")
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            for i in range(4):
                p, q_ = pts[i], pts[(i + 1) % 4]
                sk.entities.append(SketchEntity("line", f"{ename}.{'bottom right top left'.split()[i]}",
                                                {"p0": Vec.of(list(p)), "p1": Vec.of(list(q_))}, constr(m)))
            names = [f"{ename}.{s}" for s in ("bottom", "right", "top", "left")]
            sk.dims += [{"type": "HORIZONTAL", "entity": names[0]}, {"type": "HORIZONTAL", "entity": names[2]},
                        {"type": "VERTICAL", "entity": names[1]}, {"type": "VERTICAL", "entity": names[3]},
                        {"type": "DISTANCE", "a": names[0] + ".start", "b": names[0] + ".end", "dir": "HORIZONTAL",
                         "value": _absdiff(x1, x0)},
                        {"type": "DISTANCE", "a": names[1] + ".start", "b": names[1] + ".end", "dir": "VERTICAL",
                         "value": _absdiff(y1, y0)},
                        {"type": "POSITION", "point": names[0] + ".start", "x": x0, "y": y0}]

        @fn("skCircle")
        def _circle(sk, ename, m):
            sk = _sk(sk, ename, "skCircle")
            c = pt2(m.get("center"), "skCircle center")
            r = length(m.get("radius"), "skCircle radius")
            if mag(r) <= 0:
                raise FSError("skCircle: radius must be positive")
            sk.entities.append(SketchEntity("circle", ename, {"c": c, "r": r}, constr(m)))
            from ..fslite.interp import mul
            sk.dims += [{"type": "DIAMETER", "entity": ename, "value": mul(2.0, r, None)},
                        {"type": "POSITION", "point": ename + ".center", "x": c.item(0), "y": c.item(1)}]

        @fn("skArc")
        def _arc(sk, ename, m):
            sk = _sk(sk, ename, "skArc")
            s_, mid, e_ = (pt2(m.get(k), f"skArc {k}") for k in ("start", "mid", "end"))
            geo = _arc_geom(s_.a, mid.a, e_.a)
            if geo is None:
                raise FSError("skArc: start, mid and end are collinear")
            sk.entities.append(SketchEntity("arc", ename, {**geo, "p0": s_, "p1": e_}, constr(m)))

        @fn("skPolyline")
        def _poly(sk, ename, m):
            sk = _sk(sk, ename, "skPolyline")
            pts = m.get("points")
            if not isinstance(pts, list) or len(pts) < 2:
                raise FSError("skPolyline: \"points\" must be an array of at least 2 points")
            pts = [pt2(p, "skPolyline point") for p in pts]
            for i in range(len(pts) - 1):
                sk.entities.append(SketchEntity("line", f"{ename}.{i}", {"p0": pts[i], "p1": pts[i + 1]}, constr(m)))

        @fn("skRegularPolygon")
        def _polygon(sk, ename, m):
            sk = _sk(sk, ename, "skRegularPolygon")
            c = pt2(m.get("center"), "center").a
            v0 = pt2(m.get("firstVertex"), "firstVertex").a
            n = int(mag(m.get("sides", 6)))
            pts = []
            for i in range(n):
                a = 2 * math.pi * i / n
                rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
                pts.append(Vec(c + rot @ (v0 - c), (1, 0)))
            for i in range(n):
                sk.entities.append(SketchEntity("line", f"{ename}.{i}", {"p0": pts[i], "p1": pts[(i + 1) % n]}, constr(m)))

        @fn("skSolve")
        def _solve(sk):
            if not isinstance(sk, SketchRec):
                raise FSError("skSolve expects a sketch")
            if not sk.entities:
                raise FSError(f"skSolve: sketch \"{sk.name}\" is empty")
            sk.solved = True

        # standard features
        def make_feature_fn(ft):
            spec = SPECS[ft]["params"]

            def feature(ctx, fid, m):
                name = tr.feature_name(fid, ft)
                if not isinstance(m, dict):
                    raise FSError(f"{ft}: third argument must be a definition map")
                for k, v in m.items():
                    if k not in spec:
                        close = [p for p in spec if p.lower().startswith(str(k)[:4].lower())][:6]
                        raise FSError(f"{ft}: unknown parameter \"{k}\". " + (f"Did you mean one of {close}? " if close else "")
                                      + f"Valid parameters: {', '.join(spec)[:900]}")
                    if spec[k]["type"] not in CHECKED_TYPES:
                        raise FSError(f"{ft}: parameter \"{k}\" has type {spec[k]['type']}, which fsgen cannot set yet "
                                      f"(leave it at its default)")
                    _check_param(ft, k, spec[k], v)
                tr.add(FeatureRec(ft, name, dict(m), line=tr.current_line))
            return feature

        # every standard feature in the specs is callable; the catalog says which ones are verified
        for ft in SPECS:
            if ft in ("assignVariable", "newSketch"):
                continue
            feat = make_feature_fn(ft)
            prev = b.get(ft)
            if isinstance(prev, Builtin):  # e.g. transform(vector) vs. the Transform feature
                def both(*a, _f=feat, _p=prev):
                    return _f(*a) if len(a) == 3 and isinstance(a[1], Id) else _p.fn(*a)
                b[ft] = Builtin(ft, both)
            else:
                b[ft] = Builtin(ft, feat)

        # block the custom-feature op layer: native scripts must use standard features
        for k in list(b):
            if (k.startswith("op") and k[2:3].isupper()) or k in ("fCuboid", "fCylinder", "fCone", "fSphere",
                                                                   "newSketchOnPlane", "defineFeature"):
                b[k] = Builtin(k, (lambda k: lambda *a: (_ for _ in ()).throw(FSError(
                    f"{k} is a low-level operation; Part Studio scripts must use standard features "
                    f"(newSketch, extrude, revolve, fillet, ...)")))(k))

    def run(self, src: str) -> TraceResult:
        res = TraceResult(False)
        try:
            prog = self.interp.load(src)
            build = self.interp.globals.vars.get("build")
            if not isinstance(build, Function):
                raise FSError("script must define `export function build(context is Context, id is Id)`")
            # record the source line of each top-level feature call for error reporting
            orig_exec = self.interp.exec

            def exec_(st, scope):
                if st.line:
                    self.current_line = st.line
                return orig_exec(st, scope)
            self.interp.exec = exec_
            self.interp.call(build, [object(), Id(())], None)
            for f in self.features:
                if f.sketch is not None and not f.sketch.solved:
                    raise FSError(f"sketch \"{f.name}\" was never solved (call skSolve)")
            res.ok = True
        except (FSError, FSSyntaxError) as e:
            res.error, res.error_line = str(e), getattr(e, "line", 0)
        res.features = self.features
        res.variable_list = self.variable_list
        res.variables = dict(self.interp.variables)
        res.output = self.interp.output
        return res


def _absdiff(a, b):
    from ..fslite.interp import add
    d = add(a, b, None, sign=-1)
    if mag(d) < 0:
        d = add(b, a, None, sign=-1)
    return d


def _arc_geom(s, m, e):
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-15:
        return None
    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay) + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx) + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, am = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, e, m))
    # counter-clockwise from a0 to a1 must pass through am; otherwise swap direction
    ccw = lambda a: (a - a0) % (2 * math.pi)
    if ccw(am) <= ccw(a1):
        start, end = a0, a0 + ccw(a1)
        flipped = False
    else:
        start, end = a1, a1 + (a0 - a1) % (2 * math.pi)
        flipped = True
    return {"cx": ux, "cy": uy, "r": r, "t0": start, "t1": end, "flipped": flipped}


def _check_param(ft, key, spec, v):
    t = spec["type"]
    where = f"{ft}: parameter \"{key}\""
    if t == "Enum":
        if not isinstance(v, EnumVal) or v.name not in spec["options"]:
            raise FSError(f"{where} must be one of {spec['enum']}.{{{', '.join(spec['options'])}}}, got {fmt(v)}")
    elif t == "Quantity":
        if isinstance(v, bool) or not isinstance(v, (int, float, Q)):
            raise FSError(f"{where} must be a number/length/angle, got {type_name(v)}")
        want = {"LENGTH": (1, 0), "ANGLE": (0, 1), "INTEGER": (0, 0), "REAL": (0, 0), "COUNT": (0, 0)}.get(spec.get("quantity"))
        if want is not None and dims_of(v) != want:
            raise FSError(f"{where} expects a {spec['quantity'].lower()}, got {fmt(v)}")
    elif t == "Boolean":
        if not isinstance(v, bool):
            raise FSError(f"{where} must be true/false")
    elif t == "Query":
        if not isinstance(v, QText):
            raise FSError(f"{where} must be a query (e.g. qCreatedBy(id + \"Extrude 1\", EntityType.EDGE)), got {type_name(v)}")
    elif t == "String":
        if not isinstance(v, str):
            raise FSError(f"{where} must be a string")
    elif t == "Array":
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            raise FSError(f"{where} must be an array of maps, one per item, with keys {list(spec['items'])}")
        for item in v:
            for k2, v2 in item.items():
                if k2 not in spec["items"]:
                    raise FSError(f"{where}: unknown item key \"{k2}\"; valid: {list(spec['items'])}")
                _check_param(ft, f"{key}.{k2}", spec["items"][k2], v2)
    elif t == "FeatureList":
        if not isinstance(v, list) or not all(isinstance(x, Id) for x in v):
            raise FSError(f"{where} must be an array of feature ids like [id + \"Boss\", id + \"Hole\"]")


def trace(src: str, variables_mode: str | None = None) -> TraceResult:
    import os
    mode = variables_mode or os.environ.get("FSGEN_VARIABLES", "studio")
    return Tracer(mode).run(src)
