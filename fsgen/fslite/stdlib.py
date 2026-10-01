"""FS-Lite standard library: the subset of Onshape's std that the local engine implements."""
from __future__ import annotations

import math

import numpy as np

from . import geom as G
from .geom import Body, Context, Ent, Sketch
from .interp import Builtin, FeatureFn, Function
from .values import (UNITS, Box3d, EnumType, EnumVal, FSError, Id, Line, Plane, Q, Transform, Vec, as_angle, as_dir3,
                     as_length, as_point2, as_point3, default_x, dims_of, fmt, mag, mk, type_name)

ENUMS = {
    "EntityType": ["BODY", "FACE", "EDGE", "VERTEX"],
    "BoundingType": ["BLIND", "THROUGH_ALL", "SYMMETRIC", "UP_TO_NEXT", "UP_TO_SURFACE", "UP_TO_BODY", "UP_TO_VERTEX"],
    "BooleanOperationType": ["UNION", "SUBTRACTION", "INTERSECTION", "SUBTRACT_COMPLEMENT"],
    "ChamferType": ["EQUAL_OFFSETS", "TWO_OFFSETS", "OFFSET_ANGLE"],
    "GeometryType": ["LINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "OTHER_CURVE", "PLANE", "CYLINDER", "CONE",
                     "SPHERE", "TORUS", "OTHER_SURFACE", "MESH"],
    "BodyType": ["SOLID", "SHEET", "WIRE", "POINT", "MATE_CONNECTOR", "COMPOSITE"],
    "AdjacencyType": ["EDGE", "VERTEX"],
    "CapType": ["START", "END", "EITHER"],
    "NewBodyOperationType": ["NEW", "ADD", "REMOVE", "INTERSECT"],
    "ToleranceType": ["NONE"],
    "ToolBodyType": ["SOLID", "SURFACE"],
    "ProfileControlMode": ["NONE", "KEEP_ORIENTATION", "LOCK_FACES", "LOCK_DIRECTION"],
}


# ------------------------------------------------------------------ argument helpers
def need_ctx(c):
    if not isinstance(c, Context):
        raise FSError(f"first argument must be the context, got {type_name(c)}")
    return c


def need_id(i, fname):
    if not isinstance(i, Id):
        raise FSError(f"{fname}: second argument must be an Id (e.g. id + \"name\"), got {type_name(i)}")
    if len(i) == 0:
        raise FSError(f"{fname}: Id must not be empty (use id + \"name\")")
    return i


def need_map(m, fname):
    if not isinstance(m, dict):
        raise FSError(f"{fname}: expected a definition map, got {type_name(m)}")
    return m


def req(m, key, fname):
    if m.get(key) is None:
        raise FSError(f"{fname}: missing required parameter \"{key}\"")
    return m[key]


def enum_name(v, enum, fname, key):
    if not isinstance(v, EnumVal) or v.enum != enum:
        raise FSError(f"{fname}: \"{key}\" must be a {enum} value, got {fmt(v)}")
    return v.name


# ------------------------------------------------------------------ queries
class Query:
    def eval(self, ctx: Context) -> list[Ent]:
        raise NotImplementedError

    def __repr__(self):
        return f"Query({type(self).__name__})"


def need_query(q, fname, key="entities"):
    if not isinstance(q, Query):
        raise FSError(f"{fname}: \"{key}\" must be a Query (e.g. qCreatedBy(...)), got {type_name(q)}")
    return q


def dedupe(ents):
    seen, out = set(), []
    for e in ents:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _etype(t, fname):
    if t is None:
        return None
    return enum_name(t, "EntityType", fname, "entityType")


class QCreatedBy(Query):
    def __init__(self, fid, etype=None):
        self.fid = need_id(fid, "qCreatedBy")
        self.etype = _etype(etype, "qCreatedBy")

    def eval(self, ctx):
        n = len(self.fid)
        match = lambda cid: cid is not None and tuple(cid[:n]) == tuple(self.fid)
        out = []
        for b in ctx.bodies:
            if b.kind in ("sketch", "wire"):
                if not match(b.creator):
                    continue
                if self.etype in (None, "BODY"):
                    out.append(Ent("BODY", b.shape, b))
                if self.etype in (None, "FACE"):
                    out += [Ent("FACE", f, b) for f in G.subshapes(b.shape, G.TopAbs_FACE)]
                if self.etype in (None, "EDGE"):
                    out += [Ent("EDGE", e, b) for e in G.subshapes(b.shape, G.TopAbs_EDGE)]
                continue
            if self.etype in (None, "BODY") and match(b.creator):
                out.append(Ent("BODY", b.shape, b))
            if self.etype != "BODY":
                for k in ([self.etype] if self.etype else ["FACE", "EDGE", "VERTEX"]):
                    for s in G.subshapes(b.shape, G._KIND[k]):
                        if match(ctx.tag_of(s, b)):
                            out.append(Ent(k, s, b))
        return dedupe(out)


class QSketchRegion(Query):
    def __init__(self, fid, filter_inner=False):
        self.fid = need_id(fid, "qSketchRegion")
        if not isinstance(filter_inner, bool):
            raise FSError("qSketchRegion: filterInnerLoops must be a boolean")
        self.filter_inner = filter_inner

    def eval(self, ctx):
        n = len(self.fid)
        out = []
        for sid, sk in ctx.sketches.items():
            if tuple(sid[:n]) != tuple(self.fid) or not sk.solved or sk.body not in ctx.bodies:
                continue
            for f, inner in zip(sk.regions, sk.inner_regions):
                if self.filter_inner and inner:
                    continue
                out.append(Ent("FACE", f, sk.body))
        return out


class QSketchEntity(Query):
    """sketchEntityQuery(sketchId, EntityType.EDGE, "entityId"): edges of one named sketch entity."""

    def __init__(self, fid, etype, ename):
        self.fid = need_id(fid, "sketchEntityQuery")
        self.etype = _etype(etype, "sketchEntityQuery")
        if self.etype not in (None, "EDGE"):
            raise FSError("sketchEntityQuery: FS-Lite supports EntityType.EDGE only")
        if not isinstance(ename, str):
            raise FSError("sketchEntityQuery: sketchEntityId must be a string")
        self.ename = ename

    def eval(self, ctx):
        sk = ctx.sketches.get(tuple(self.fid))
        if sk is None or sk.body not in ctx.bodies:
            return []
        return [Ent("EDGE", e, sk.body) for name, edges, _c in sk.entities
                if name == self.ename or name.startswith(f"{self.ename}.") for e in edges]


class QList(Query):
    def __init__(self, ents):
        self.ents = ents

    def eval(self, ctx):
        return list(self.ents)


class QUnion(Query):
    def __init__(self, qs):
        if not isinstance(qs, list):
            raise FSError("qUnion expects an array of queries")
        self.qs = [need_query(q, "qUnion", "subqueries") for q in qs]

    def eval(self, ctx):
        return dedupe([e for q in self.qs for e in q.eval(ctx)])


class QSubtraction(Query):
    def __init__(self, a, b):
        self.a, self.b = need_query(a, "qSubtraction", "query1"), need_query(b, "qSubtraction", "query2")

    def eval(self, ctx):
        rm = set(self.b.eval(ctx))
        return [e for e in self.a.eval(ctx) if e not in rm]


class QIntersection(Query):
    def __init__(self, qs):
        if not isinstance(qs, list) or not qs:
            raise FSError("qIntersection expects a non-empty array of queries")
        self.qs = [need_query(q, "qIntersection", "subqueries") for q in qs]

    def eval(self, ctx):
        sets = [set(q.eval(ctx)) for q in self.qs[1:]]
        return [e for e in self.qs[0].eval(ctx) if all(e in s for s in sets)]


class QFilter(Query):
    def __init__(self, base, pred):
        self.base, self.pred = base, pred

    def eval(self, ctx):
        return [e for e in self.base.eval(ctx) if self.pred(ctx, e)]


class QEverything(Query):
    def __init__(self, etype=None, solids_only=False):
        self.etype = _etype(etype, "qEverything")
        self.solids_only = solids_only

    def eval(self, ctx):
        ents = ctx.all_entities(self.etype, include_sketch=not self.solids_only)
        return ents


class QOwnedBy(Query):
    def __init__(self, bodies, etype):
        self.bodies, self.etype = need_query(bodies, "qOwnedByBody", "body"), _etype(etype, "qOwnedByBody")

    def eval(self, ctx):
        out = []
        for e in self.bodies.eval(ctx):
            if e.kind != "BODY":
                continue
            for k in ([self.etype] if self.etype else ["FACE", "EDGE", "VERTEX"]):
                if k == "BODY":
                    out.append(e)
                    continue
                out += [Ent(k, s, e.body) for s in G.subshapes(e.body.shape, G._KIND[k])]
        return dedupe(out)


class QOwnerBody(Query):
    def __init__(self, q):
        self.q = need_query(q, "qOwnerBody", "query")

    def eval(self, ctx):
        return dedupe([Ent("BODY", e.body.shape, e.body) for e in self.q.eval(ctx) if e.body is not None])


class QAdjacent(Query):
    def __init__(self, q, adj, etype):
        self.q = need_query(q, "qAdjacent", "seed")
        self.adj = enum_name(adj, "AdjacencyType", "qAdjacent", "adjacencyType")
        self.etype = _etype(etype, "qAdjacent")

    def eval(self, ctx):
        out = []
        for seed in self.q.eval(ctx):
            if seed.body is None or seed.kind == "BODY":
                continue
            share_kind = G.TopAbs_EDGE if self.adj == "EDGE" else G.TopAbs_VERTEX
            seed_sub = {hash(s): s for s in G.subshapes(seed.shape, share_kind)} if seed.kind != "VERTEX" or self.adj == "EDGE" else {hash(seed.shape): seed.shape}
            if seed.kind == "EDGE" and self.adj == "EDGE":
                seed_sub = {hash(seed.shape): seed.shape}
            for cand in G.subshapes(seed.body.shape, G._KIND[self.etype]):
                if cand.IsSame(seed.shape):
                    continue
                subs = G.subshapes(cand, share_kind) if self.etype != ("EDGE" if self.adj == "EDGE" else "VERTEX") else [cand]
                if any(hash(s) in seed_sub and seed_sub[hash(s)].IsSame(s) for s in subs):
                    out.append(Ent(self.etype, cand, seed.body))
        return dedupe(out)


class QSorted(Query):
    def __init__(self, q, key, pick):
        self.q, self.key, self.pick = q, key, pick

    def eval(self, ctx):
        ents = self.q.eval(ctx)
        if not ents:
            return []
        vals = [self.key(e) for e in ents]
        best = self.pick(vals)
        return [e for e, v in zip(ents, vals) if abs(v - best) <= 1e-9 * max(1.0, abs(best))]


def ent_measure(e: Ent) -> float:
    if e.kind == "BODY":
        return G.props(e.shape, "volume").Mass()
    if e.kind == "FACE":
        return G.props(e.shape, "area").Mass()
    if e.kind == "EDGE":
        return G.props(e.shape, "length").Mass()
    return 0.0


# ------------------------------------------------------------------ builder
class StdLib:
    def __init__(self):
        self.b: dict = {}
        self._register()

    def fn(self, name):
        def deco(f):
            self.b[name] = Builtin(name, f)
            return f
        return deco

    def _register(self):
        b, fn = self.b, self.fn
        for k, v in UNITS.items():
            b[k] = v
        for name, members in ENUMS.items():
            b[name] = EnumType(name, members)
        b["PI"] = math.pi
        b["unitless"] = 1.0
        mmb = lambda lo, d, hi: {UNITS["meter"]: [lo, d, hi]}
        b["LENGTH_BOUNDS"] = mmb(-500, 0.025, 500)
        b["NONNEGATIVE_LENGTH_BOUNDS"] = mmb(0, 0.025, 500)
        b["NONNEGATIVE_ZERO_DEFAULT_LENGTH_BOUNDS"] = mmb(0, 0, 500)
        b["ZERO_DEFAULT_LENGTH_BOUNDS"] = mmb(-500, 0, 500)
        b["POSITIVE_COUNT_BOUNDS"] = {1.0: [1, 2, 1e9]}
        b["ANGLE_360_BOUNDS"] = {UNITS["degree"]: [0, 30, 360]}
        b["inf"] = math.inf
        b["WORLD_ORIGIN"] = Vec([0, 0, 0], (1, 0))
        b["X_DIRECTION"] = Vec([1, 0, 0])
        b["Y_DIRECTION"] = Vec([0, 1, 0])
        b["Z_DIRECTION"] = Vec([0, 0, 1])
        b["XY_PLANE"] = Plane(np.zeros(3), np.array([0, 0, 1.0]), np.array([1.0, 0, 0]))
        b["YZ_PLANE"] = Plane(np.zeros(3), np.array([1.0, 0, 0]), np.array([0, 1.0, 0]))
        b["XZ_PLANE"] = Plane(np.zeros(3), np.array([0, 1.0, 0]), np.array([0, 0, 1.0]))

        # ---------------- language helpers
        @fn("defineFeature")
        def _define_feature(f, defaults=None):
            if not isinstance(f, Function):
                raise FSError("defineFeature expects a function(context is Context, id is Id, definition is map)")
            return FeatureFn(f)

        @fn("newId")
        def _new_id():
            return Id(())

        @fn("println")
        def _println(*args):
            self.interp.output.append("".join(fmt(a) for a in args))

        @fn("print")
        def _print(*args):
            self.interp.output.append("".join(fmt(a) for a in args))

        @fn("debug")
        def _debug(ctx, v, *rest):
            self.interp.output.append("debug: " + fmt(v))

        @fn("regenError")
        def _regen_error(msg, *rest):
            return {"message": msg}

        @fn("toString")
        def _to_string(v):
            return fmt(v)

        @fn("size")
        def _size(v):
            if isinstance(v, (list, dict, str, Vec)):
                return float(len(v))
            raise FSError(f"size() expects an array, map or string, got {type_name(v)}")

        @fn("append")
        def _append(arr, v):
            if not isinstance(arr, list):
                raise FSError(f"append() expects an array, got {type_name(arr)}")
            return arr + [v]

        @fn("concatenateArrays")
        def _concat(arrs):
            if not isinstance(arrs, list) or not all(isinstance(a, list) for a in arrs):
                raise FSError("concatenateArrays expects an array of arrays")
            return [x for a in arrs for x in a]

        @fn("range")
        def _range(a, b_, step=None):
            # Onshape: range(from, to) inclusive; range(from, to, count)?? keep the documented 2-arg form
            if step is not None:
                raise FSError("range(): only range(from, to) is supported in FS-Lite")
            if dims_of(a) != dims_of(b_):
                raise FSError("range(): bounds must have the same units")
            lo, hi = mag(a), mag(b_)
            out, v = [], lo
            while v <= hi + 1e-9:
                out.append(mk(v, dims_of(a)))
                v += 1
            return out

        @fn("mapArray")
        def _map_array(arr, f):
            return [self.interp.call(f, [x], None) for x in arr]

        @fn("isUndefinedOrEmptyString")
        def _undef_or_empty(v):
            return v is None or v == ""

        @fn("abs")
        def _abs(x):
            if isinstance(x, Q):
                return Q(abs(x.v), x.dims)
            return abs(mag(x))

        def _minmax(pick, args):
            if len(args) == 1 and isinstance(args[0], list):
                args = args[0]
            if not args:
                raise FSError("min/max of nothing")
            d = dims_of(args[0])
            if any(dims_of(a) != d for a in args):
                raise FSError("min/max arguments must have the same units")
            return mk(pick(mag(a) for a in args), d)

        b["min"] = Builtin("min", lambda *a: _minmax(min, list(a)))
        b["max"] = Builtin("max", lambda *a: _minmax(max, list(a)))

        def _unitless_or_rad(x, fname):
            if isinstance(x, Q):
                return as_angle(x, fname + " argument")
            return mag(x)

        for name, f in (("sin", math.sin), ("cos", math.cos), ("tan", math.tan)):
            b[name] = Builtin(name, (lambda f, name: lambda x: f(_unitless_or_rad(x, name)))(f, name))
        for name, f in (("asin", math.asin), ("acos", math.acos), ("atan", math.atan)):
            b[name] = Builtin(name, (lambda f: lambda x: Q(f(mag(x)), (0, 1)))(f))

        @fn("atan2")
        def _atan2(y, x):
            if dims_of(y) != dims_of(x):
                raise FSError("atan2 arguments must have the same units")
            return Q(math.atan2(mag(y), mag(x)), (0, 1))

        @fn("sqrt")
        def _sqrt(x):
            d = dims_of(x)
            if d[0] % 2 or d[1] % 2:
                raise FSError("sqrt of a value with odd unit powers")
            if mag(x) < 0:
                raise FSError("sqrt of a negative value")
            return mk(math.sqrt(mag(x)), (d[0] // 2, d[1] // 2))

        for name, f in (("floor", math.floor), ("ceil", math.ceil), ("round", lambda v: math.floor(v + 0.5))):
            b[name] = Builtin(name, (lambda f, name: lambda x: float(f(_need_unitless(x, name))))(f, name))

        @fn("exp")
        def _exp(x):
            return math.exp(_need_unitless(x, "exp"))

        @fn("log")
        def _log(x):
            return math.log(_need_unitless(x, "log"))

        @fn("roundToPrecision")
        def _rtp(x, digits):
            return round(_need_unitless(x, "roundToPrecision"), int(digits))

        # ---------------- vectors
        @fn("vector")
        def _vector(*args):
            if len(args) == 1 and isinstance(args[0], list):
                args = args[0]
            return Vec.of(list(args))

        @fn("norm")
        def _norm(v):
            _need_vec(v, "norm")
            return mk(float(np.linalg.norm(v.a)), v.dims)

        @fn("squaredNorm")
        def _sqnorm(v):
            _need_vec(v, "squaredNorm")
            return mk(float(v.a @ v.a), (v.dims[0] * 2, v.dims[1] * 2))

        @fn("normalize")
        def _normalize(v):
            _need_vec(v, "normalize")
            n = np.linalg.norm(v.a)
            if n < 1e-15:
                raise FSError("cannot normalize a zero vector")
            return Vec(v.a / n)

        @fn("dot")
        def _dot(a, c):
            _need_vec(a, "dot"), _need_vec(c, "dot")
            return mk(float(a.a @ c.a), (a.dims[0] + c.dims[0], a.dims[1] + c.dims[1]))

        @fn("cross")
        def _cross(a, c):
            _need_vec(a, "cross"), _need_vec(c, "cross")
            return Vec(np.cross(a.a, c.a), (a.dims[0] + c.dims[0], a.dims[1] + c.dims[1]))

        @fn("perpendicularVector")
        def _perp(v):
            return Vec(default_x(as_dir3(v)))

        # ---------------- planes, lines, transforms
        @fn("plane")
        def _plane(origin, normal, x=None):
            o = as_point3(origin, "plane origin")
            n = as_dir3(normal, "plane normal")
            if x is None:
                xd = default_x(n)
            else:
                xd = as_dir3(x, "plane x direction")
                xd = xd - (xd @ n) * n
                if np.linalg.norm(xd) < 1e-9:
                    raise FSError("plane x direction must not be parallel to the normal")
                xd /= np.linalg.norm(xd)
            return Plane(o, n, xd)

        @fn("line")
        def _line(origin, direction):
            return Line(as_point3(origin, "line origin"), as_dir3(direction, "line direction"))

        @fn("transform")
        def _transform(t):
            m = np.eye(4)
            m[:3, 3] = as_point3(t, "translation")
            return Transform(m)

        @fn("identityTransform")
        def _ident():
            return Transform(np.eye(4))

        @fn("rotationAround")
        def _rot(axis, angle):
            if not isinstance(axis, Line):
                raise FSError("rotationAround: first argument must be a line(origin, direction)")
            a = as_angle(angle, "rotationAround angle")
            k = axis.direction
            K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            R = np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * (K @ K)
            m = np.eye(4)
            m[:3, :3] = R
            m[:3, 3] = axis.origin - R @ axis.origin
            return Transform(m)

        @fn("mirrorAcross")
        def _mirror(pl):
            if not isinstance(pl, Plane):
                raise FSError("mirrorAcross expects a Plane")
            n = pl.normal
            R = np.eye(3) - 2 * np.outer(n, n)
            m = np.eye(4)
            m[:3, :3] = R
            m[:3, 3] = 2 * (pl.origin @ n) * n
            return Transform(m)

        @fn("scaleUniformly")
        def _scale(s, *rest):
            m = np.eye(4)
            m[:3, :3] *= _need_unitless(s, "scaleUniformly")
            return Transform(m)

        # ---------------- queries
        b["qCreatedBy"] = Builtin("qCreatedBy", lambda fid, et=None: QCreatedBy(fid, et))
        b["qSketchRegion"] = Builtin("qSketchRegion", lambda fid, fil=False: QSketchRegion(fid, fil))
        b["sketchEntityQuery"] = Builtin("sketchEntityQuery", lambda fid, et, name: QSketchEntity(fid, et, name))
        b["qUnion"] = Builtin("qUnion", lambda qs: QUnion(qs))
        b["qSubtraction"] = Builtin("qSubtraction", lambda a, c: QSubtraction(a, c))
        b["qIntersection"] = Builtin("qIntersection", lambda qs: QIntersection(qs))
        b["qNothing"] = Builtin("qNothing", lambda: QList([]))
        b["qEverything"] = Builtin("qEverything", lambda et=None: QEverything(et))
        b["qAllModifiableSolidBodies"] = Builtin("qAllModifiableSolidBodies", lambda: QFilter(QEverything(None), lambda c, e: e.kind == "BODY" and e.body.kind == "solid"))
        b["qAllNonMeshSolidBodies"] = b["qAllModifiableSolidBodies"]
        b["qOwnedByBody"] = Builtin("qOwnedByBody", lambda q, et=None: QOwnedBy(q, et))
        b["qOwnerBody"] = Builtin("qOwnerBody", lambda q: QOwnerBody(q))
        b["qAdjacent"] = Builtin("qAdjacent", lambda q, adj, et: QAdjacent(q, adj, et))

        @fn("qEntityFilter")
        def _qef(q, et):
            k = _etype(et, "qEntityFilter")
            return QFilter(need_query(q, "qEntityFilter", "query"), lambda c, e: e.kind == k)

        @fn("qBodyType")
        def _qbt(q, bt):
            names = [enum_name(x, "BodyType", "qBodyType", "bodyType") for x in (bt if isinstance(bt, list) else [bt])]

            def pred(c, e):
                if e.body is None:
                    return False
                kind = "SOLID" if e.body.kind == "solid" else "WIRE"
                return kind in names or (e.body.kind == "sketch" and "SHEET" in names and e.kind == "FACE")
            return QFilter(need_query(q, "qBodyType", "query"), pred)

        @fn("qGeometry")
        def _qgeom(q, gt):
            g = enum_name(gt, "GeometryType", "qGeometry", "geometryType")

            def pred(c, e):
                if e.kind == "EDGE":
                    k = G.edge_geom(e.shape)
                    return k == g or (g == "OTHER_CURVE" and k == "OTHER")
                if e.kind == "FACE":
                    k = G.face_geom(e.shape)
                    return k == g or (g == "OTHER_SURFACE" and k == "OTHER")
                return False
            return QFilter(need_query(q, "qGeometry", "query"), pred)

        @fn("qCoincidesWithPlane")
        def _qcwp(q, pl):
            if not isinstance(pl, Plane):
                raise FSError("qCoincidesWithPlane: second argument must be a Plane")

            def pred(c, e):
                if e.kind == "BODY":
                    return False
                if e.kind == "FACE":
                    fp = G.face_plane(e.shape)
                    if fp is None:
                        return False
                    o, n, _ = fp
                    return abs(abs(n @ pl.normal) - 1) < 1e-9 and abs((o - pl.origin) @ pl.normal) < 1e-7
                lo, hi = G.bbox([e.shape])
                corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
                # cheap reject then exact check on sample points
                pts = _sample_points(e)
                return all(abs((p - pl.origin) @ pl.normal) < 1e-7 for p in pts)
            return QFilter(need_query(q, "qCoincidesWithPlane", "query"), pred)

        @fn("qContainsPoint")
        def _qcp(q, p):
            pt = as_point3(p, "qContainsPoint point")
            v = G.vertex(pt)

            def pred(c, e):
                if e.kind == "BODY" and e.body.kind == "solid":
                    return G.classify_point(e.shape, pt) in ("in", "on")
                return G.distance(e.shape, v) < 1e-7
            return QFilter(need_query(q, "qContainsPoint", "query"), pred)

        @fn("qClosestTo")
        def _qct(q, p):
            pt = as_point3(p, "qClosestTo point")
            v = G.vertex(pt)
            return QSorted(need_query(q, "qClosestTo", "query"), lambda e: G.distance(e.shape, v), min)

        @fn("qFarthestAlong")
        def _qfa(q, d):
            dv = as_dir3(d, "qFarthestAlong direction")

            def key(e):
                lo, hi = G.bbox([e.shape])
                return max(dv @ np.array([x, y, z]) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2]))
            return QSorted(need_query(q, "qFarthestAlong", "query"), key, max)

        b["qLargest"] = Builtin("qLargest", lambda q: QSorted(need_query(q, "qLargest", "query"), ent_measure, max))
        b["qSmallest"] = Builtin("qSmallest", lambda q: QSorted(need_query(q, "qSmallest", "query"), ent_measure, min))

        @fn("qParallelEdges")
        def _qpe(q, ref):
            if isinstance(ref, Vec):
                dref = as_dir3(ref, "qParallelEdges direction")
            else:
                raise FSError("qParallelEdges: FS-Lite supports qParallelEdges(query, directionVector) only")

            def pred(c, e):
                if e.kind != "EDGE":
                    return False
                d = G.edge_direction(e.shape)
                return d is not None and abs(abs(d @ dref) - 1) < 1e-9
            return QFilter(need_query(q, "qParallelEdges", "query"), pred)

        @fn("qNthElement")
        def _qnth(q, n):
            idx = int(_need_unitless(n, "qNthElement"))

            class _Q(Query):
                def eval(self_, ctx):
                    ents = need_query(q, "qNthElement", "query").eval(ctx)
                    return [ents[idx]] if -len(ents) <= idx < len(ents) else []
            return _Q()

        # ---------------- evaluation
        @fn("evaluateQuery")
        def _evq(ctx, q):
            return [QList([e]) for e in need_query(q, "evaluateQuery", "query").eval(need_ctx(ctx))]

        @fn("evaluateQueryCount")
        def _evqc(ctx, q):
            return float(len(need_query(q, "evaluateQueryCount", "query").eval(need_ctx(ctx))))

        @fn("isQueryEmpty")
        def _isqe(ctx, q):
            return len(need_query(q, "isQueryEmpty", "query").eval(need_ctx(ctx))) == 0

        def _ents(ctx, m, key, fname):
            return need_query(req(need_map(m, fname), key, fname), fname, key).eval(need_ctx(ctx))

        @fn("evVolume")
        def _evvol(ctx, m):
            ents = [e for e in _ents(ctx, m, "entities", "evVolume") if e.kind == "BODY" and e.body.kind == "solid"]
            return Q(sum(G.props(e.shape, "volume").Mass() for e in ents), (3, 0))

        @fn("evArea")
        def _evarea(ctx, m):
            ents = [e for e in _ents(ctx, m, "entities", "evArea") if e.kind == "FACE"]
            return Q(sum(G.props(e.shape, "area").Mass() for e in ents), (2, 0))

        @fn("evLength")
        def _evlen(ctx, m):
            ents = [e for e in _ents(ctx, m, "entities", "evLength") if e.kind == "EDGE"]
            return Q(sum(G.props(e.shape, "length").Mass() for e in ents), (1, 0))

        @fn("evBox3d")
        def _evbox(ctx, m):
            ents = _ents(ctx, m, "topology", "evBox3d")
            if not ents:
                raise FSError("evBox3d: topology query resolved to nothing")
            lo, hi = G.bbox([e.shape for e in ents])
            return Box3d(lo, hi)

        @fn("evCurveDefinition")
        def _evcurve(ctx, m):
            from OCP.BRepAdaptor import BRepAdaptor_Curve
            ents = [e for e in _ents(ctx, m, "edge", "evCurveDefinition") if e.kind == "EDGE"]
            if len(ents) != 1:
                raise FSError(f"evCurveDefinition: expected exactly one edge, got {len(ents)}")
            c = BRepAdaptor_Curve(G.TopoDS.Edge(ents[0].shape))
            if c.GetType() == G.GeomAbs_Circle:
                circ = c.Circle()
                ax = circ.Position()
                v = lambda d: np.array([d.X(), d.Y(), d.Z()])
                return {"coordSystem": {"origin": Vec(v(ax.Location()), (1, 0)), "xAxis": Vec(v(ax.XDirection())),
                                        "zAxis": Vec(v(ax.Direction()))}, "radius": Q(circ.Radius(), (1, 0))}
            if c.GetType() == G.GeomAbs_Line:
                ln = c.Line()
                p, d = ln.Location(), ln.Direction()
                return Line(np.array([p.X(), p.Y(), p.Z()]), np.array([d.X(), d.Y(), d.Z()]))
            raise FSError("evCurveDefinition: FS-Lite supports lines and circles only")

        @fn("evAxis")
        def _evaxis(ctx, m):
            for e in _ents(ctx, m, "axis", "evAxis"):
                if e.kind == "EDGE":
                    d = G.edge_direction(e.shape)
                    if d is not None:
                        p = G.BRep_Tool.Pnt_s(G.TopExp.FirstVertex_s(G.TopoDS.Edge(e.shape)))
                        return Line(np.array([p.X(), p.Y(), p.Z()]), d)
                if e.kind == "FACE" and G.face_geom(e.shape) == "CYLINDER":
                    ax = G.BRepAdaptor_Surface(G.TopoDS.Face(e.shape)).Cylinder().Axis()
                    o, d = ax.Location(), ax.Direction()
                    return Line(np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()]))
            raise FSError("evAxis: \"axis\" must be a straight edge or a cylindrical face")

        @fn("evPlane")
        def _evplane(ctx, m):
            ents = [e for e in _ents(ctx, m, "face", "evPlane") if e.kind == "FACE"]
            if len(ents) != 1:
                raise FSError(f"evPlane: expected exactly one face, got {len(ents)}")
            fp = G.face_plane(ents[0].shape)
            if fp is None:
                raise FSError("evPlane: face is not planar")
            o, n, x = fp
            c = G.props(ents[0].shape, "area").CentreOfMass()
            return Plane(np.array([c.X(), c.Y(), c.Z()]), n, x)

        # ---------------- sketches
        @fn("newSketchOnPlane")
        def _new_sketch(ctx, sid, m):
            ctx = need_ctx(ctx)
            sid = need_id(sid, "newSketchOnPlane")
            pl = req(need_map(m, "newSketchOnPlane"), "sketchPlane", "newSketchOnPlane")
            if not isinstance(pl, Plane):
                raise FSError("newSketchOnPlane: \"sketchPlane\" must be a Plane, e.g. plane(origin, normal) or XY_PLANE")
            ctx.register_op(sid, "newSketchOnPlane")
            sk = Sketch(sid, pl)
            sk.ctx = ctx
            ctx.sketches[sid] = sk
            return sk

        @fn("newSketch")
        def _new_sketch_q(ctx, sid, m):
            ctx = need_ctx(ctx)
            q = req(need_map(m, "newSketch"), "sketchPlane", "newSketch")
            faces = [e for e in need_query(q, "newSketch", "sketchPlane").eval(ctx) if e.kind == "FACE"]
            if len(faces) != 1:
                raise FSError(f"newSketch: sketchPlane must resolve to exactly one planar face, got {len(faces)}")
            pl = _evplane(ctx, {"face": QList(faces)})
            return _new_sketch(ctx, sid, {"sketchPlane": pl})

        def _sk(sk, name, fname):
            if not isinstance(sk, Sketch):
                raise FSError(f"{fname}: first argument must be a sketch from newSketchOnPlane")
            if sk.solved:
                raise FSError(f"{fname}: sketch already solved")
            if not isinstance(name, str) or not name:
                raise FSError(f"{fname}: second argument must be a unique non-empty string id")
            if any(n == name for n, _, _ in sk.entities):
                raise FSError(f"{fname}: duplicate sketch entity id \"{name}\"")
            return sk

        def _constr(m):
            c = m.get("construction", False)
            return bool(c)

        @fn("skLineSegment")
        def _skline(sk, name, m):
            sk = _sk(sk, name, "skLineSegment")
            m = need_map(m, "skLineSegment")
            a = as_point2(req(m, "start", "skLineSegment"), "skLineSegment start")
            c = as_point2(req(m, "end", "skLineSegment"), "skLineSegment end")
            sk.entities.append((name, [G.sketch_edge_line(sk.plane, a, c)], _constr(m)))

        @fn("skCircle")
        def _skcircle(sk, name, m):
            sk = _sk(sk, name, "skCircle")
            m = need_map(m, "skCircle")
            c = as_point2(req(m, "center", "skCircle"), "skCircle center")
            r = as_length(req(m, "radius", "skCircle"), "skCircle radius")
            sk.entities.append((name, [G.sketch_edge_circle(sk.plane, c, r)], _constr(m)))

        @fn("skEllipse")
        def _skellipse(sk, name, m):
            sk = _sk(sk, name, "skEllipse")
            m = need_map(m, "skEllipse")
            c = as_point2(req(m, "center", "skEllipse"), "skEllipse center")
            rmaj = as_length(req(m, "majorRadius", "skEllipse"), "majorRadius")
            rmin = as_length(req(m, "minorRadius", "skEllipse"), "minorRadius")
            ax = m.get("majorAxis")
            axd = np.array([1.0, 0.0]) if ax is None else ax.a / np.linalg.norm(ax.a)
            if rmin > rmaj:
                raise FSError("skEllipse: minorRadius must not exceed majorRadius")
            sk.entities.append((name, [G.sketch_edge_ellipse(sk.plane, c, axd, rmaj, rmin)], _constr(m)))

        @fn("skArc")
        def _skarc(sk, name, m):
            sk = _sk(sk, name, "skArc")
            m = need_map(m, "skArc")
            s = as_point2(req(m, "start", "skArc"), "skArc start")
            mid = as_point2(req(m, "mid", "skArc"), "skArc mid")
            e = as_point2(req(m, "end", "skArc"), "skArc end")
            sk.entities.append((name, [G.sketch_edge_arc3(sk.plane, s, mid, e)], _constr(m)))

        @fn("skRectangle")
        def _skrect(sk, name, m):
            sk = _sk(sk, name, "skRectangle")
            m = need_map(m, "skRectangle")
            p0 = as_point2(req(m, "firstCorner", "skRectangle"), "skRectangle firstCorner")
            p1 = as_point2(req(m, "secondCorner", "skRectangle"), "skRectangle secondCorner")
            if abs(p0[0] - p1[0]) < G.LIN_TOL or abs(p0[1] - p1[1]) < G.LIN_TOL:
                raise FSError("skRectangle: corners must differ in both x and y")
            pts = [p0, np.array([p1[0], p0[1]]), p1, np.array([p0[0], p1[1]])]
            edges = [G.sketch_edge_line(sk.plane, pts[i], pts[(i + 1) % 4]) for i in range(4)]
            sk.entities.append((name, edges, _constr(m)))

        @fn("skPolyline")
        def _skpoly(sk, name, m):
            sk = _sk(sk, name, "skPolyline")
            m = need_map(m, "skPolyline")
            pts = req(m, "points", "skPolyline")
            if not isinstance(pts, list) or len(pts) < 2:
                raise FSError("skPolyline: \"points\" must be an array of at least 2 points")
            p2 = [as_point2(p, "skPolyline point") for p in pts]
            edges = [G.sketch_edge_line(sk.plane, p2[i], p2[i + 1]) for i in range(len(p2) - 1)]
            sk.entities.append((name, edges, _constr(m)))

        @fn("skRegularPolygon")
        def _skpolygon(sk, name, m):
            sk = _sk(sk, name, "skRegularPolygon")
            m = need_map(m, "skRegularPolygon")
            c = as_point2(req(m, "center", "skRegularPolygon"), "skRegularPolygon center")
            v0 = as_point2(req(m, "firstVertex", "skRegularPolygon"), "skRegularPolygon firstVertex")
            n = int(_need_unitless(req(m, "sides", "skRegularPolygon"), "sides"))
            if n < 3:
                raise FSError("skRegularPolygon: sides must be >= 3")
            r = v0 - c
            pts = []
            for i in range(n):
                a = 2 * math.pi * i / n
                rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
                pts.append(c + rot @ r)
            edges = [G.sketch_edge_line(sk.plane, pts[i], pts[(i + 1) % n]) for i in range(n)]
            sk.entities.append((name, edges, _constr(m)))

        @fn("skSolve")
        def _sksolve(sk):
            if not isinstance(sk, Sketch):
                raise FSError("skSolve expects a sketch")
            G.solve_sketch(sk.ctx, sk)

        # ---------------- modeling ops
        def _op_start(ctx, oid, m, fname):
            ctx = need_ctx(ctx)
            oid = need_id(oid, fname)
            m = need_map(m, fname)
            ctx.register_op(oid, fname)
            return ctx, oid, m

        def _new_solids(ctx, oid, shapes, fname):
            solids = [s for sh in shapes for s in G.solids_of(sh)]
            if not solids:
                raise FSError(f"{fname}: operation produced no solid")
            bodies = [Body(s, oid) for s in solids]
            ctx.commit(oid, None, new_bodies=bodies)
            return bodies

        def _faces(ctx, m, key, fname):
            ents = need_query(req(m, key, fname), fname, key).eval(ctx)
            faces = [e.shape for e in ents if e.kind == "FACE"]
            if not faces:
                raise FSError(f"{fname}: \"{key}\" resolved to no faces (did you call skSolve and use the right sketch id?)")
            return faces

        def _bodies(ctx, m, key, fname, required=True):
            q = m.get(key)
            if q is None:
                if required:
                    raise FSError(f"{fname}: missing required parameter \"{key}\"")
                return []
            ents = need_query(q, fname, key).eval(ctx)
            bodies = []
            for e in ents:
                if e.kind == "BODY" and e.body.kind == "solid" and e.body not in bodies:
                    bodies.append(e.body)
            if required and not bodies:
                raise FSError(f"{fname}: \"{key}\" resolved to no solid bodies")
            return bodies

        @fn("opExtrude")
        def _op_extrude(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opExtrude")
            faces = _faces(ctx, m, "entities", "opExtrude")
            d = as_dir3(req(m, "direction", "opExtrude"), "opExtrude direction")
            end = enum_name(req(m, "endBound", "opExtrude"), "BoundingType", "opExtrude", "endBound")

            def extent(bound, key):
                if bound == "BLIND":
                    return as_length(req(m, key, "opExtrude"), f"opExtrude {key}")
                if bound == "THROUGH_ALL":
                    return G.BIG
                raise FSError(f"opExtrude: bound {bound} is not supported in FS-Lite (use BLIND or THROUGH_ALL)")
            e1 = extent(end, "endDepth")
            e0 = 0.0
            if m.get("startBound") is not None:
                start = enum_name(m["startBound"], "BoundingType", "opExtrude", "startBound")
                e0 = extent(start, "startDepth")
            if e1 + e0 <= G.LIN_TOL:
                raise FSError("opExtrude: total extrude depth must be positive")
            shapes = []
            for f in faces:
                f0 = G.translate(f, -e0 * d) if e0 else f
                shapes.append(G.make_prism(f0, (e1 + e0) * d))
            fused, _ = G.fuse_all(shapes)
            _new_solids(ctx, oid, [fused], "opExtrude")

        @fn("opRevolve")
        def _op_revolve(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opRevolve")
            faces = _faces(ctx, m, "entities", "opRevolve")
            axis = req(m, "axis", "opRevolve")
            if not isinstance(axis, Line):
                raise FSError("opRevolve: \"axis\" must be a line(origin, direction)")
            fwd = as_angle(req(m, "angleForward", "opRevolve"), "angleForward")
            back = as_angle(m["angleBack"], "angleBack") if m.get("angleBack") is not None else 0.0
            shapes = [G.make_revol(f, axis.origin, axis.direction, back, fwd) for f in faces]
            fused, _ = G.fuse_all(shapes)
            _new_solids(ctx, oid, [fused], "opRevolve")

        @fn("opBoolean")
        def _op_boolean(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opBoolean")
            kind = enum_name(req(m, "operationType", "opBoolean"), "BooleanOperationType", "opBoolean", "operationType")
            tools = _bodies(ctx, m, "tools", "opBoolean")
            keep = bool(m.get("keepTools", False))
            if kind == "UNION":
                # Onshape: "targets" only take part when targetsAndToolsNeedGrouping is true;
                # otherwise only the tools are merged with each other (verified against Onshape).
                targets = []
                if m.get("targetsAndToolsNeedGrouping"):
                    targets = _bodies(ctx, m, "targets", "opBoolean", required=False)
                elif m.get("targets") is not None:
                    ctx.log.append(f"warning: {'.'.join(oid)}: opBoolean UNION ignores \"targets\" unless "
                                   "\"targetsAndToolsNeedGrouping\" : true (Onshape behaviour); put all bodies in \"tools\"")
                allb = targets + [t for t in tools if t not in targets]
                if len(allb) < 2:
                    ctx.commit(oid, None)
                    return
                shape, hist = G.fuse_all([b.shape for b in allb])
                creator = allb[0].creator
                removed = allb if not keep else targets
                new = [Body(s, creator) for s in G.solids_of(shape)]
                ctx.commit(oid, hist, new_bodies=new, removed_bodies=removed)
                return
            if kind == "INTERSECTION":
                if m.get("targets") is not None:
                    raise FSError("opBoolean: BOOLEAN_BAD_INPUT - INTERSECTION takes \"tools\" only (no \"targets\")")
                if len(tools) < 2:
                    raise FSError("opBoolean: INTERSECTION needs at least two tool bodies")
                shape = tools[0].shape
                hists = []
                for t in tools[1:]:
                    shape, h = G.boolean("INTERSECTION", [shape], [t.shape])
                    hists.append(h)
                new = [Body(s, tools[0].creator) for s in G.solids_of(shape)]
                if not new:
                    raise FSError("opBoolean: intersection is empty")
                ctx.commit(oid, _ChainedHistory(hists), new_bodies=new, removed_bodies=tools)
                return
            if kind not in ("SUBTRACTION", "INTERSECTION"):
                raise FSError(f"opBoolean: {kind} not supported")
            targets = _bodies(ctx, m, "targets", "opBoolean")
            tools = [t for t in tools if t not in targets]
            if not tools:
                raise FSError("opBoolean: tools and targets must be different bodies")
            new, removed = [], list(targets)
            hists = []
            for t in targets:
                if kind == "INTERSECTION":
                    # Onshape intersects the target with all tools together
                    shape, hist = G.boolean("INTERSECTION", [t.shape], [x.shape for x in tools])
                else:
                    shape, hist = G.boolean("SUBTRACTION", [t.shape], [x.shape for x in tools])
                hists.append(hist)
                new += [Body(s, t.creator) for s in G.solids_of(shape)]
            if not keep:
                removed += tools
            ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=removed)

        def _edges_by_body(ctx, m, fname):
            ents = need_query(req(m, "entities", fname), fname, "entities").eval(ctx)
            groups: dict = {}
            for e in ents:
                if e.body is None or e.body.kind != "solid":
                    continue
                if e.kind == "EDGE":
                    groups.setdefault(id(e.body), (e.body, []))[1].append(e.shape)
                elif e.kind == "FACE":
                    groups.setdefault(id(e.body), (e.body, []))[1].extend(G.subshapes(e.shape, G.TopAbs_EDGE))
            if not groups:
                raise FSError(f"{fname}: \"entities\" resolved to no edges or faces of solid bodies")
            return list(groups.values())

        def _modify_bodies(ctx, oid, groups, fn_):
            hists, new, removed = [], [], []
            for body, items in groups:
                shape, hist = fn_(body.shape, items)
                hists.append(hist)
                removed.append(body)
                new += [Body(s, body.creator) for s in G.solids_of(shape)]
            ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=removed)

        @fn("opFillet")
        def _op_fillet(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opFillet")
            r = as_length(req(m, "radius", "opFillet"), "opFillet radius")
            _modify_bodies(ctx, oid, _edges_by_body(ctx, m, "opFillet"), lambda s, es: G.fillet(s, es, r))

        @fn("opChamfer")
        def _op_chamfer(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opChamfer")
            ct = enum_name(req(m, "chamferType", "opChamfer"), "ChamferType", "opChamfer", "chamferType")
            if ct != "EQUAL_OFFSETS":
                raise FSError("opChamfer: FS-Lite supports ChamferType.EQUAL_OFFSETS only")
            w = as_length(req(m, "width", "opChamfer"), "opChamfer width")
            _modify_bodies(ctx, oid, _edges_by_body(ctx, m, "opChamfer"), lambda s, es: G.chamfer(s, es, w))

        @fn("opShell")
        def _op_shell(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opShell")
            t = as_length(req(m, "thickness", "opShell"), "opShell thickness")
            ents = need_query(req(m, "entities", "opShell"), "opShell", "entities").eval(ctx)
            groups: dict = {}
            for e in ents:
                if e.kind == "FACE" and e.body is not None and e.body.kind == "solid":
                    groups.setdefault(id(e.body), (e.body, []))[1].append(e.shape)
            if not groups:
                raise FSError("opShell: \"entities\" must resolve to faces to remove")
            _modify_bodies(ctx, oid, list(groups.values()), lambda s, fs: G.shell(s, fs, t))

        @fn("opTransform")
        def _op_transform(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opTransform")
            bodies = _bodies(ctx, m, "bodies", "opTransform")
            tr = req(m, "transform", "opTransform")
            if not isinstance(tr, Transform):
                raise FSError("opTransform: \"transform\" must be a Transform")
            hists, new = [], []
            for body in bodies:
                shape, hist = G.transform_shape(body.shape, tr.m)
                hists.append(hist)
                new.append(Body(shape, body.creator))
            ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=bodies)

        @fn("opPattern")
        def _op_pattern(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opPattern")
            bodies = _bodies(ctx, m, "entities", "opPattern")
            trs = req(m, "transforms", "opPattern")
            names = req(m, "instanceNames", "opPattern")
            if not isinstance(trs, list) or not isinstance(names, list) or len(trs) != len(names):
                raise FSError("opPattern: \"transforms\" and \"instanceNames\" must be arrays of equal length")
            if len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
                raise FSError("opPattern: instanceNames must be unique non-empty strings")
            for tr, nm in zip(trs, names):
                if not isinstance(tr, Transform):
                    raise FSError("opPattern: every entry of \"transforms\" must be a Transform")
                new = []
                for body in bodies:
                    shape, _ = G.transform_shape(body.shape, tr.m)
                    new += [Body(s, Id(tuple(oid) + (nm,))) for s in G.solids_of(shape)]
                ctx.commit(Id(tuple(oid) + (nm,)), None, new_bodies=new)

        @fn("opLoft")
        def _op_loft(ctx, oid, m):
            from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
            from OCP.BRepTools import BRepTools
            ctx, oid, m = _op_start(ctx, oid, m, "opLoft")
            subs = req(m, "profileSubqueries", "opLoft")
            if not isinstance(subs, list) or len(subs) < 2:
                raise FSError("opLoft: \"profileSubqueries\" must be an array of at least two queries")
            mk = BRepOffsetAPI_ThruSections(True, False)
            for q in subs:
                faces = [e.shape for e in need_query(q, "opLoft", "profileSubqueries").eval(ctx) if e.kind == "FACE"]
                if not faces:
                    raise FSError("opLoft: a profile query selects no faces")
                mk.AddWire(BRepTools.OuterWire_s(G.TopoDS.Face(faces[0])))
            mk.Build()
            if not mk.IsDone():
                raise FSError("opLoft: loft failed")
            _new_solids(ctx, oid, [mk.Shape()], "opLoft")

        @fn("opHelix")
        def _op_helix(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opHelix")
            d = as_dir3(req(m, "direction", "opHelix"), "opHelix direction")
            o = as_point3(req(m, "axisStart", "opHelix"), "opHelix axisStart")
            sp = as_point3(req(m, "startPoint", "opHelix"), "opHelix startPoint")
            iv = req(m, "interval", "opHelix")
            if not isinstance(iv, list) or len(iv) != 2:
                raise FSError("opHelix: \"interval\" must be [startRevolution, endRevolution]")
            iv = [_need_unitless(t, "opHelix interval") for t in iv]
            cw = req(m, "clockwise", "opHelix")
            if not isinstance(cw, bool):
                raise FSError("opHelix: \"clockwise\" must be a boolean")
            pitch = as_length(req(m, "helicalPitch", "opHelix"), "opHelix helicalPitch")
            spiral = m.get("spiralPitch")
            if spiral is not None and abs(as_length(spiral, "opHelix spiralPitch")) > G.LIN_TOL:
                raise FSError("opHelix: spiral helices (spiralPitch != 0) are not supported in FS-Lite")
            edge = G.helix_edge(o, d, sp, iv, pitch, cw)
            ctx.commit(oid, None, new_bodies=[Body(edge, oid, kind="wire", helical=True)])

        @fn("opSweep")
        def _op_sweep(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opSweep")
            faces = _faces(ctx, m, "profiles", "opSweep")
            path = [e for e in need_query(req(m, "path", "opSweep"), "opSweep", "path").eval(ctx) if e.kind == "EDGE"]
            if not path:
                raise FSError("opSweep: \"path\" resolved to no edges")
            if m.get("profileControl") is not None and enum_name(
                    m["profileControl"], "ProfileControlMode", "opSweep", "profileControl") != "NONE":
                raise FSError("opSweep: FS-Lite supports ProfileControlMode.NONE only")
            if m.get("keepProfileOrientation"):
                raise FSError("opSweep: keepProfileOrientation is not supported in FS-Lite")
            frenet = all(e.body is not None and e.body.helical for e in path)
            _new_solids(ctx, oid, [G.sweep_solid(f, [e.shape for e in path], frenet) for f in faces], "opSweep")

        @fn("opDeleteBodies")
        def _op_delete(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "opDeleteBodies")
            ents = need_query(req(m, "entities", "opDeleteBodies"), "opDeleteBodies", "entities").eval(ctx)
            bodies = [e.body for e in ents if e.kind == "BODY"]
            ctx.commit(oid, None, removed_bodies=bodies)

        @fn("fCuboid")
        def _fcuboid(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "fCuboid")
            c1 = as_point3(req(m, "corner1", "fCuboid"), "fCuboid corner1")
            c2 = as_point3(req(m, "corner2", "fCuboid"), "fCuboid corner2")
            _new_solids(ctx, oid, [G.cuboid(c1, c2)], "fCuboid")

        @fn("fCylinder")
        def _fcyl(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "fCylinder")
            b0 = as_point3(req(m, "bottomCenter", "fCylinder"), "fCylinder bottomCenter")
            t0 = as_point3(req(m, "topCenter", "fCylinder"), "fCylinder topCenter")
            r = as_length(req(m, "radius", "fCylinder"), "fCylinder radius")
            _new_solids(ctx, oid, [G.cylinder(b0, t0, r)], "fCylinder")

        @fn("fCone")
        def _fcone(ctx, oid, m):
            ctx, oid, m = _op_start(ctx, oid, m, "fCone")
            b0 = as_point3(req(m, "bottomCenter", "fCone"), "fCone bottomCenter")
            t0 = as_point3(req(m, "topCenter", "fCone"), "fCone topCenter")
            rb = as_length(req(m, "bottomRadius", "fCone"), "fCone bottomRadius")
            rt = as_length(req(m, "topRadius", "fCone"), "fCone topRadius")
            _new_solids(ctx, oid, [G.cone(b0, t0, rb, rt)], "fCone")

        @fn("fSphere")
        def _fsphere(ctx, oid, m):
            raise FSError("fSphere is not supported in FS-Lite (in Onshape its center must be a vertex Query); "
                          "revolve a half-disc sketch (skArc + skLineSegment) 360 degrees instead")

        def _sample_points(e):
            from OCP.BRepAdaptor import BRepAdaptor_Curve
            if e.kind == "VERTEX":
                p = G.BRep_Tool.Pnt_s(G.TopoDS.Vertex(e.shape))
                return [np.array([p.X(), p.Y(), p.Z()])]
            if e.kind == "EDGE":
                c = BRepAdaptor_Curve(G.TopoDS.Edge(e.shape))
                t0, t1 = c.FirstParameter(), c.LastParameter()
                return [np.array([(p := c.Value(t0 + (t1 - t0) * k / 6)).X(), p.Y(), p.Z()]) for k in range(7)]
            return []

        def _need_vec(v, fname):
            if not isinstance(v, Vec):
                raise FSError(f"{fname} expects a vector, got {type_name(v)}")

        def _need_unitless(x, fname):
            if isinstance(x, Q) and x.dims == (0, 0):
                return float(x.v)
            if isinstance(x, Q) or isinstance(x, bool) or not isinstance(x, (int, float)):
                raise FSError(f"{fname} expects a unitless number, got {fmt(x)}")
            return float(x)


class _ChainedHistory(G._ChainHistory):
    pass


class _MultiHistory:
    def __init__(self, hists):
        self.hists = [h for h in hists if h is not None]

    def Modified(self, s):
        out = G.TopTools_ListOfShape()
        for h in self.hists:
            try:
                for x in h.Modified(s):
                    out.Append(x)
            except Exception:
                pass
        return out

    def IsDeleted(self, s):
        return any(_safe_deleted(h, s) for h in self.hists)


def _safe_deleted(h, s):
    try:
        return h.IsDeleted(s)
    except Exception:
        return False
