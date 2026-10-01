"""OpenCascade-backed modeling context for FS-Lite.

Mirrors the parts of Onshape's Context that FS-Lite exposes: bodies, sketches,
queries with provenance ("created by feature id"), and op* modeling operations.

Provenance: every face/edge/vertex of every body is tagged with the Id of the
feature that created it. After each modeling op we push the tags through the
OCCT operation history (Modified/IsDeleted); untagged survivors are new and get
the current op's Id. This approximates Onshape's qCreatedBy semantics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo, BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeVertex,
                                BRepBuilderAPI_MakeWire, BRepBuilderAPI_Transform)
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.BRepOffset import BRepOffset_Skin
from OCP.GeomAbs import GeomAbs_Intersection
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol, BRepPrimAPI_MakeSphere)
from OCP.BRepTools import BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GC import GC_MakeArcOfCircle
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import (GeomAbs_BSplineCurve, GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Ellipse,
                         GeomAbs_Line, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus)
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as TopTools_IndexedMapOfShape
from OCP.OCP.collections import List_TopoDS_Shape as TopTools_ListOfShape
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.BRep import BRep_Builder
from OCP.gp import gp_Ax1, gp_Ax2, gp_Circ, gp_Dir, gp_Elips, gp_Pnt, gp_Trsf, gp_Vec
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

from .values import FSError, Id, Plane

LIN_TOL = 1e-7  # meters
BIG = 10.0  # "through all" extent in meters

# ------------------------------------------------------------------ shape helpers
_KIND = {"BODY": TopAbs_SOLID, "FACE": TopAbs_FACE, "EDGE": TopAbs_EDGE, "VERTEX": TopAbs_VERTEX}


def subshapes(shape: TopoDS_Shape, kind) -> list[TopoDS_Shape]:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def downcast(s: TopoDS_Shape):
    t = s.ShapeType()
    return {TopAbs_FACE: TopoDS.Face, TopAbs_EDGE: TopoDS.Edge, TopAbs_VERTEX: TopoDS.Vertex,
            TopAbs_SOLID: TopoDS.Solid, TopAbs_WIRE: TopoDS.Wire}.get(t, lambda x: x)(s)


def pnt(p) -> gp_Pnt:
    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]))


def gdir(d) -> gp_Dir:
    return gp_Dir(float(d[0]), float(d[1]), float(d[2]))


def compound(shapes) -> TopoDS_Compound:
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def props(shape, kind: str) -> GProp_GProps:
    g = GProp_GProps()
    if kind == "volume":
        BRepGProp.VolumeProperties_s(shape, g)
    elif kind == "area":
        BRepGProp.SurfaceProperties_s(shape, g)
    else:
        BRepGProp.LinearProperties_s(shape, g)
    return g


def bbox(shapes) -> tuple[np.ndarray, np.ndarray]:
    b = Bnd_Box()
    for s in shapes:
        BRepBndLib.AddOptimal_s(s, b, False, False)
    if b.IsVoid():
        raise FSError("cannot compute bounding box of empty selection")
    lo, hi = b.CornerMin(), b.CornerMax()
    return np.array([lo.X(), lo.Y(), lo.Z()]), np.array([hi.X(), hi.Y(), hi.Z()])


def distance(a: TopoDS_Shape, b: TopoDS_Shape) -> float:
    d = BRepExtrema_DistShapeShape(a, b)
    return d.Value() if d.IsDone() else math.inf


def to_trsf(m: np.ndarray) -> gp_Trsf:
    t = gp_Trsf()
    r = m[:3, :3]
    det = np.linalg.det(r)
    if det < 0:
        raise FSError("internal: mirror transforms must use mirror_trsf")
    t.SetValues(*[float(v) for v in np.hstack([r, m[:3, 3:4]]).ravel()])
    return t


def transform_shape(shape, m: np.ndarray):
    """Returns (new_shape, history_builder)."""
    r = m[:3, :3]
    if np.linalg.det(r) < 0:
        # Mirror: M = Rigid * Reflection(across plane through origin with normal n).
        # Pick n as eigenvector of r with eigenvalue -1 for pure reflections; for general case
        # decompose as (M * S) * S where S reflects across XY.
        s = np.diag([1.0, 1.0, -1.0, 1.0])
        rigid = m @ s
        t1 = gp_Trsf()
        t1.SetMirror(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)))
        b1 = BRepBuilderAPI_Transform(shape, t1, True)
        b2 = BRepBuilderAPI_Transform(b1.Shape(), to_trsf(rigid), True)
        return b2.Shape(), _ChainHistory([b1, b2])
    b = BRepBuilderAPI_Transform(shape, to_trsf(m), True)
    return b.Shape(), b


class _ChainHistory:
    def __init__(self, builders):
        self.builders = builders

    def Modified(self, s):
        cur = [s]
        for b in self.builders:
            nxt = []
            for c in cur:
                mods = list(b.Modified(c))
                nxt.extend(mods if mods else [c])
            cur = nxt
        out = TopTools_ListOfShape()
        for c in cur:
            out.Append(c)
        return out

    def IsDeleted(self, s):
        return False


# ------------------------------------------------------------------ entities
class Ent:
    """A topological entity handle: body, face, edge or vertex."""

    __slots__ = ("kind", "shape", "body", "_h")

    def __init__(self, kind: str, shape: TopoDS_Shape, body: "Body | None" = None):
        self.kind = kind
        self.shape = shape
        self.body = body
        self._h = hash(shape) if kind != "BODY" else id(body)

    def __hash__(self):
        return self._h

    def __eq__(self, other):
        if not isinstance(other, Ent) or self.kind != other.kind:
            return False
        if self.kind == "BODY":
            return self.body is other.body
        return self.shape.IsSame(other.shape)

    def __repr__(self):
        return f"Ent({self.kind})"


@dataclass(eq=False)
class Body:
    shape: TopoDS_Shape
    creator: Id
    kind: str = "solid"  # solid | sketch | wire (curves such as a helix)
    helical: bool = False  # wire body made by opHelix: sweeps along it use the Frenet frame


@dataclass(eq=False)
class Sketch:
    id: Id
    plane: Plane
    entities: list = field(default_factory=list)  # (name, [edges], construction)
    solved: bool = False
    body: Body | None = None
    regions: list = field(default_factory=list)
    inner_regions: list = field(default_factory=list)


class Context:
    def __init__(self):
        self.bodies: list[Body] = []
        self.sketches: dict[Id, Sketch] = {}
        # per-body provenance: id(body) -> {hash(subshape): [(subshape, creator Id)]}
        self.tags: dict[int, dict[int, list[tuple[TopoDS_Shape, Id]]]] = {}
        self.log: list[str] = []
        self.op_ids: list[Id] = []

    def register_op(self, oid: Id, fname: str):
        """Onshape rules: ids are unique, and all operations under one parent id must be contiguous
        ("Parent Id X used at two non-contiguous points in operation history")."""
        from .values import FSError
        t = tuple(oid)
        if any(tuple(o) == t for o in self.op_ids):
            raise FSError(f"{fname}: duplicate id {'.'.join(t)}: each operation needs a unique id")
        last = tuple(self.op_ids[-1]) if self.op_ids else ()
        for k in range(2, len(t)):
            parent = t[:k]
            used = any(tuple(o[:k]) == parent for o in self.op_ids)
            if used and last[:k] != parent:
                raise FSError(f"{fname}: Parent Id {'.'.join(parent)} used at two non-contiguous points in operation "
                              f"history (other operations ran in between); give {'.'.join(t)} its own parent id")
        self.op_ids.append(oid)

    # -- provenance --------------------------------------------------------
    @staticmethod
    def _lookup(table, s):
        for shp, cid in table.get(hash(s), ()):
            if shp.IsSame(s):
                return cid
        return None

    def tag_of(self, s: TopoDS_Shape, body: Body | None = None) -> Id | None:
        if body is not None:
            return self._lookup(self.tags.get(id(body), {}), s)
        for t in self.tags.values():
            cid = self._lookup(t, s)
            if cid is not None:
                return cid
        return None

    def commit(self, op_id: Id, history=None, new_bodies=(), removed_bodies=()):
        """Update body list and provenance after an operation.

        Only removed bodies' tags are pushed through `history` (an object with
        Modified(shape)/IsDeleted(shape)); new bodies inherit matching tags and
        everything else they contain is tagged with op_id. Untouched bodies keep
        their tags as-is, which keeps this cheap.
        """
        carry: dict = {}
        # Subshapes that survive unchanged are the same TShape, so a hash lookup finds them;
        # only query the (slow, ~ms per call) OCCT history for shapes that disappeared.
        survivors: dict = {}
        for b in new_bodies:
            if b.kind == "solid":
                for k in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
                    for sub in subshapes(b.shape, k):
                        survivors.setdefault(hash(sub), []).append((sub, None))
        for b in removed_bodies:
            for bucket in self.tags.get(id(b), {}).values():
                for sh, cid in bucket:
                    if any(x.IsSame(sh) for x, _ in survivors.get(hash(sh), ())):
                        carry.setdefault(hash(sh), []).append((sh, cid))
                        continue
                    mods = []
                    if history is not None:
                        try:
                            mods = list(history.Modified(sh))
                        except Exception:
                            mods = []
                    if mods:
                        for m in mods:
                            carry.setdefault(hash(m), []).append((m, cid))
                    else:
                        deleted = False
                        if history is not None:
                            try:
                                deleted = history.IsDeleted(sh)
                            except Exception:
                                pass
                        if not deleted:
                            carry.setdefault(hash(sh), []).append((sh, cid))
        for b in removed_bodies:
            if b in self.bodies:
                self.bodies.remove(b)
            self.tags.pop(id(b), None)
        for b in new_bodies:
            self.bodies.append(b)
            if b.kind != "solid":
                continue
            table: dict = {}
            for k in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
                for sub in subshapes(b.shape, k):
                    cid = self._lookup(carry, sub)
                    table.setdefault(hash(sub), []).append((sub, cid if cid is not None else op_id))
            self.tags[id(b)] = table

    # -- queries -----------------------------------------------------------
    def all_entities(self, kind: str | None = None, include_sketch=True) -> list[Ent]:
        out = []
        for b in self.bodies:
            if b.kind == "sketch" and not include_sketch:
                continue
            if kind in (None, "BODY"):
                out.append(Ent("BODY", b.shape, b))
            if kind != "BODY":
                kinds = [kind] if kind else ["FACE", "EDGE", "VERTEX"]
                for k in kinds:
                    for s in subshapes(b.shape, _KIND[k]):
                        out.append(Ent(k, s, b))
        return out


# ------------------------------------------------------------------ sketch solving
def _wire_from_edges(edges):
    mk = BRepBuilderAPI_MakeWire()
    for e in edges:
        mk.Add(e)
    if not mk.IsDone():
        return None
    return mk.Wire()


def _chain_loops(edges, tol=1e-7):
    """Group open edges into closed loops by matching endpoints."""
    def ends(e):
        a = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(e))
        b = BRep_Tool.Pnt_s(TopExp.LastVertex_s(e))
        return np.array([a.X(), a.Y(), a.Z()]), np.array([b.X(), b.Y(), b.Z()])

    closed, open_ = [], []
    for e in edges:
        a, b = ends(e)
        (closed if np.linalg.norm(a - b) < tol else open_).append(e)
    loops = [[e] for e in closed]
    remaining = list(open_)
    dangling = []
    while remaining:
        chain = [remaining.pop(0)]
        start, end = ends(chain[0])
        grew = True
        while grew and np.linalg.norm(start - end) > tol:
            grew = False
            for i, e in enumerate(remaining):
                a, b = ends(e)
                if np.linalg.norm(a - end) < tol:
                    end = b
                elif np.linalg.norm(b - end) < tol:
                    end = a
                elif np.linalg.norm(b - start) < tol:
                    start = a
                elif np.linalg.norm(a - start) < tol:
                    start = b
                else:
                    continue
                chain.append(remaining.pop(i))
                grew = True
                break
        if np.linalg.norm(start - end) < tol:
            loops.append(chain)
        else:
            dangling.extend(chain)
    return loops, dangling


def solve_sketch(ctx: Context, sk: Sketch):
    edges = [e for (_, es, constr) in sk.entities if not constr for e in es]
    loops, dangling = _chain_loops(edges)
    faces = []
    for loop in loops:
        w = _wire_from_edges(loop)
        if w is None:
            continue
        mf = BRepBuilderAPI_MakeFace(w, True)
        if mf.IsDone():
            f = mf.Face()
            if props(f, "area").Mass() > 1e-14:
                faces.append(f)
    regions = []
    if len(faces) == 1:
        regions = faces
    elif faces:
        # General fuse splits overlapping loops into disjoint regions sharing edges.
        gf = BRepAlgoAPI_BuilderAlgo()
        args = TopTools_ListOfShape()
        for f in faces:
            args.Append(f)
        gf.SetArguments(args)
        gf.Build()
        regions = [TopoDS.Face(f) for f in subshapes(gf.Shape(), TopAbs_FACE)]
    # inner regions: outer wire made entirely of edges that are holes of another region
    hole_edges = set()
    for f in regions:
        ow = BRepTools.OuterWire_s(f)
        for w in subshapes(f, TopAbs_WIRE):
            if not w.IsSame(ow):
                for e in subshapes(w, TopAbs_EDGE):
                    hole_edges.add(hash(e))
    inner = []
    for f in regions:
        ow_edges = subshapes(BRepTools.OuterWire_s(f), TopAbs_EDGE)
        inner.append(bool(ow_edges) and all(hash(e) in hole_edges for e in ow_edges))
    sk.regions = regions
    sk.inner_regions = inner
    sk.solved = True
    body_shape = compound(list(regions) + list(edges))
    sk.body = Body(body_shape, sk.id, kind="sketch")
    ctx.bodies.append(sk.body)
    if dangling:
        ctx.log.append(f"sketch {'.'.join(sk.id)}: {len(dangling)} edge(s) not part of a closed loop")


def sketch_edge_line(plane: Plane, a2, b2):
    a, b = plane.to_world(a2), plane.to_world(b2)
    if np.linalg.norm(a - b) < LIN_TOL:
        raise FSError("line segment has zero length")
    return BRepBuilderAPI_MakeEdge(pnt(a), pnt(b)).Edge()


def sketch_edge_circle(plane: Plane, c2, r):
    if r <= 0:
        raise FSError("circle radius must be positive")
    c = plane.to_world(c2)
    circ = gp_Circ(gp_Ax2(pnt(c), gdir(plane.normal), gdir(plane.x)), r)
    return BRepBuilderAPI_MakeEdge(circ).Edge()


def sketch_edge_ellipse(plane: Plane, c2, major_dir2, rmaj, rmin):
    c = plane.to_world(c2)
    xd = major_dir2[0] * plane.x + major_dir2[1] * plane.y
    el = gp_Elips(gp_Ax2(pnt(c), gdir(plane.normal), gdir(xd)), rmaj, rmin)
    return BRepBuilderAPI_MakeEdge(el).Edge()


def sketch_edge_arc3(plane: Plane, s2, m2, e2):
    s, m, e = (plane.to_world(p) for p in (s2, m2, e2))
    mk = GC_MakeArcOfCircle(pnt(s), pnt(m), pnt(e))
    if not mk.IsDone():
        raise FSError("skArc: start, mid and end points are collinear or coincident")
    return BRepBuilderAPI_MakeEdge(mk.Value()).Edge()


# ------------------------------------------------------------------ solids
def make_prism(face, vec) -> TopoDS_Shape:
    return BRepPrimAPI_MakePrism(face, gp_Vec(*[float(v) for v in vec])).Shape()


def translate(shape, vec):
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*[float(v) for v in vec]))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def make_revol(face, axis_origin, axis_dir, angle_back, angle_fwd):
    """Onshape semantics (measured, see compat suite): the sweep starts at +angleBack and runs
    in the positive sense to angleForward, wrapping through 360 degrees."""
    ax = gp_Ax1(pnt(axis_origin), gdir(axis_dir))
    sweep = angle_fwd - angle_back
    if angle_back:
        sweep %= 2 * math.pi
        t = gp_Trsf()
        t.SetRotation(ax, angle_back)
        face = BRepBuilderAPI_Transform(face, t, True).Shape()
    if sweep <= 1e-12:
        raise FSError("opRevolve: revolve angle must be positive")
    if sweep >= 2 * math.pi - 1e-9:
        return BRepPrimAPI_MakeRevol(face, ax).Shape()
    return BRepPrimAPI_MakeRevol(face, ax, sweep).Shape()


def solids_of(shape) -> list[TopoDS_Shape]:
    return subshapes(shape, TopAbs_SOLID)


def fuse_all(shapes):
    if len(shapes) == 1:
        return shapes[0], None
    args = TopTools_ListOfShape()
    tools = TopTools_ListOfShape()
    args.Append(shapes[0])
    for s in shapes[1:]:
        tools.Append(s)
    op = BRepAlgoAPI_Fuse()
    op.SetArguments(args)
    op.SetTools(tools)
    op.Build()
    if not op.IsDone():
        raise FSError("boolean union failed")
    return op.Shape(), op


def boolean(kind: str, targets: list, tools: list):
    op = {"UNION": BRepAlgoAPI_Fuse, "SUBTRACTION": BRepAlgoAPI_Cut, "INTERSECTION": BRepAlgoAPI_Common}[kind]()
    a = TopTools_ListOfShape()
    t = TopTools_ListOfShape()
    for s in targets:
        a.Append(s)
    for s in tools:
        t.Append(s)
    op.SetArguments(a)
    op.SetTools(t)
    op.Build()
    if not op.IsDone():
        raise FSError(f"boolean {kind.lower()} failed")
    return op.Shape(), op


def classify_point(solid, p) -> str:
    c = BRepClass3d_SolidClassifier(solid, pnt(p), 1e-7)
    st = c.State()
    return "in" if st == TopAbs_IN else "on" if st == TopAbs_ON else "out"


def vertex(p):
    return BRepBuilderAPI_MakeVertex(pnt(p)).Vertex()


def edge_geom(e) -> str:
    t = BRepAdaptor_Curve(TopoDS.Edge(e)).GetType()
    if t == GeomAbs_Line:
        return "LINE"
    if t == GeomAbs_Circle:
        c = BRepAdaptor_Curve(TopoDS.Edge(e))
        closed = abs((c.LastParameter() - c.FirstParameter()) - 2 * math.pi) < 1e-9
        return "CIRCLE" if closed else "ARC"
    if t == GeomAbs_Ellipse:
        return "ELLIPSE"
    if t == GeomAbs_BSplineCurve:
        return "SPLINE"
    return "OTHER"


def face_geom(f) -> str:
    t = BRepAdaptor_Surface(TopoDS.Face(f)).GetType()
    return {GeomAbs_Plane: "PLANE", GeomAbs_Cylinder: "CYLINDER", GeomAbs_Cone: "CONE",
            GeomAbs_Sphere: "SPHERE", GeomAbs_Torus: "TORUS"}.get(t, "OTHER")


def face_plane(f):
    s = BRepAdaptor_Surface(TopoDS.Face(f))
    if s.GetType() != GeomAbs_Plane:
        return None
    pl = s.Plane()
    o, n, x = pl.Location(), pl.Axis().Direction(), pl.XAxis().Direction()
    normal = np.array([n.X(), n.Y(), n.Z()])
    if TopoDS.Face(f).Orientation() == 1:  # REVERSED
        normal = -normal
    return np.array([o.X(), o.Y(), o.Z()]), normal, np.array([x.X(), x.Y(), x.Z()])


def edge_direction(e):
    c = BRepAdaptor_Curve(TopoDS.Edge(e))
    if c.GetType() != GeomAbs_Line:
        return None
    d = c.Line().Direction()
    return np.array([d.X(), d.Y(), d.Z()])


def fillet(body_shape, edges, radius):
    mk = BRepFilletAPI_MakeFillet(body_shape)
    for e in edges:
        mk.Add(radius, TopoDS.Edge(e))
    mk.Build()
    if not mk.IsDone():
        raise FSError("fillet failed (radius too large or edges not filletable)")
    return mk.Shape(), mk


def chamfer(body_shape, edges, width):
    mk = BRepFilletAPI_MakeChamfer(body_shape)
    for e in edges:
        mk.Add(width, TopoDS.Edge(e))
    mk.Build()
    if not mk.IsDone():
        raise FSError("chamfer failed (width too large or edges not chamferable)")
    return mk.Shape(), mk


def shell(body_shape, faces, thickness):
    lst = TopTools_ListOfShape()
    for f in faces:
        lst.Append(f)
    mk = BRepOffsetAPI_MakeThickSolid()
    # Onshape: positive thickness offsets outward with sharp (intersected) corners
    mk.MakeThickSolidByJoin(body_shape, lst, thickness, 1e-6, BRepOffset_Skin, False, False, GeomAbs_Intersection)
    mk.Build()
    if not mk.IsDone():
        raise FSError("shell failed")
    return mk.Shape(), mk


def cuboid(c1, c2):
    lo, hi = np.minimum(c1, c2), np.maximum(c1, c2)
    d = hi - lo
    if np.any(d < LIN_TOL):
        raise FSError("fCuboid corners must differ in all three coordinates")
    return BRepPrimAPI_MakeBox(pnt(lo), float(d[0]), float(d[1]), float(d[2])).Shape()


def cylinder(bottom, top, r):
    axis = top - bottom
    h = np.linalg.norm(axis)
    if h < LIN_TOL or r <= 0:
        raise FSError("fCylinder needs distinct centers and positive radius")
    return BRepPrimAPI_MakeCylinder(gp_Ax2(pnt(bottom), gdir(axis / h)), r, h).Shape()


def cone(bottom, top, rb, rt):
    axis = top - bottom
    h = np.linalg.norm(axis)
    if h < LIN_TOL:
        raise FSError("fCone needs distinct centers")
    return BRepPrimAPI_MakeCone(gp_Ax2(pnt(bottom), gdir(axis / h)), rb, rt, h).Shape()


def sphere(c, r):
    if r <= 0:
        raise FSError("fSphere radius must be positive")
    return BRepPrimAPI_MakeSphere(pnt(c), r).Shape()


def clean(shape):
    u = ShapeUpgrade_UnifySameDomain(shape, True, True, True)
    u.Build()
    return u.Shape(), u


# ------------------------------------------------------------------ helix and sweep
SWEEP_TOL = 1e-7  # meters; OCCT's default (1e-4) is 0.1 mm here and gave ~0.5 % volume errors on threads


def helix_edge(axis_origin, axis_dir, start_point, interval, pitch, clockwise=True):
    """Cylindrical helix like opHelix: axis through axis_origin along axis_dir, passing through start_point at
    revolution 0, running over `interval` = (t0, t1) revolutions and rising `pitch` per revolution along
    axis_dir. clockwise = right-handed (clockwise when viewed along axis_dir)."""
    from OCP.BRepLib import BRepLib
    from OCP.Geom import Geom_CylindricalSurface
    from OCP.Geom2d import Geom2d_Line
    from OCP.GeomAbs import GeomAbs_C2
    from OCP.gp import gp_Ax3, gp_Dir2d, gp_Pnt2d

    d = np.asarray(axis_dir, float) / np.linalg.norm(axis_dir)
    o, sp = np.asarray(axis_origin, float), np.asarray(start_point, float)
    h0 = float(np.dot(sp - o, d))
    radial = sp - (o + h0 * d)
    r = float(np.linalg.norm(radial))
    if r < LIN_TOL:
        raise FSError("helix: start point lies on the axis")
    if pitch <= LIN_TOL:
        raise FSError("helix: pitch must be positive")
    t0, t1 = float(interval[0]), float(interval[1])
    if t1 - t0 <= 1e-9:
        raise FSError("helix: interval must be increasing")
    surf = Geom_CylindricalSurface(gp_Ax3(pnt(o + h0 * d), gdir(d), gdir(radial / r)), r)
    s = 1.0 if clockwise else -1.0
    line = Geom2d_Line(gp_Pnt2d(s * 2 * math.pi * t0, pitch * t0), gp_Dir2d(s * 2 * math.pi, pitch))
    e = BRepBuilderAPI_MakeEdge(line, surf, 0.0, (t1 - t0) * math.hypot(2 * math.pi, pitch)).Edge()
    BRepLib.BuildCurves3d_s(e, 1e-9, GeomAbs_C2, 14, 2000)
    return e


def sweep_solid(face, path_edges, frenet=False):
    """Sweep a planar face along connected path edges into a solid. The profile is carried from the start of
    the path (its first vertex) with a constant relation to the moving frame: Frenet for helices (an exact
    screw motion, so a thread profile stays in the axial plane), corrected Frenet otherwise."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell

    mkw = BRepBuilderAPI_MakeWire()
    for e in path_edges:
        mkw.Add(TopoDS.Edge(e))
    if not mkw.IsDone():
        raise FSError("sweep: path edges do not form one connected path")
    spine = mkw.Wire()
    start = TopExp.FirstVertex_s(TopoDS.Edge(path_edges[0]), True) if len(path_edges) == 1 else None

    def pipe(wire):
        mk = BRepOffsetAPI_MakePipeShell(spine)
        if frenet:
            mk.SetMode(True)
        mk.SetTolerance(SWEEP_TOL, SWEEP_TOL, 1e-4)
        if start is not None:
            mk.Add(wire, start, False, False)
        else:
            mk.Add(wire, False, False)
        mk.Build()
        if not mk.IsDone() or not mk.MakeSolid():
            raise FSError("sweep failed")
        return mk.Shape()

    f = TopoDS.Face(face)
    outer = BRepTools.OuterWire_s(f)
    solid = pipe(outer)
    holes = [w for w in subshapes(f, TopAbs_WIRE) if not w.IsSame(outer)]
    if holes:
        solid, _ = boolean("SUBTRACTION", [solid], [pipe(TopoDS.Wire(w)) for w in holes])
    if props(solid, "volume").Mass() < 0:  # inside-out result
        solid.Reverse()
    return solid
