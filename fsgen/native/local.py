"""Local (offline) builder for Part Studio scripts: standard features on the OpenCascade engine.

Runs the traced feature list with the FS-Lite geometry layer (verified against Onshape in
examples/compat) so scripts can be checked, measured and rendered without API calls. Onshape then
receives the finished tree once.

Error policy (to avoid false alarms that would make the LLM "fix" correct code):
  - errors Onshape would also raise (query selects nothing, missing sketch, bad parameter) -> error
  - OpenCascade-only failures (a fillet/shell our kernel cannot compute) -> warning, feature skipped
  - features the local builder does not implement -> warning, result marked partial
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..fslite import geom as G
from ..fslite.geom import Body, Context, Ent, Sketch
from ..fslite.interp import Builtin, Function, Interpreter
from ..fslite.runner import RunResult, describe, render_png
from ..fslite.stdlib import QList, StdLib
from ..fslite.values import FSError, Id, Line, Plane, Q, Vec, mag
from .script import DEFAULT_PLANES, FeatureRec, QText, TraceResult

IMPLEMENTED = {"newSketch", "extrude", "revolve", "fillet", "chamfer", "cPlane", "linearPattern", "circularPattern",
               "mirror", "shell", "booleanBodies", "deleteBodies", "loft", "helix", "sweep", "assignVariable"}


class KernelFailure(Exception):
    """The local OpenCascade kernel could not do something Onshape probably can."""


@dataclass
class LocalResult:
    ok: bool
    error: str | None = None
    error_feature: str | None = None
    error_line: int = 0
    warnings: list[str] = field(default_factory=list)
    partial: bool = False  # some features could not be built locally
    run: RunResult | None = None
    seconds: float = 0.0

    def metrics(self) -> dict:
        m = self.run.metrics() if self.run else {}
        return {"parts": m.get("bodies", 0), "volume_mm3": m.get("volume_mm3"), "area_mm2": m.get("area_mm2"),
                "bbox_mm": m.get("bbox_mm")}

    def report(self) -> str:
        lines = [describe(self.run) if self.run else ""]
        if self.partial:
            lines.append("NOTE: some features were not built locally (see warnings); geometry is incomplete.")
        lines += [f"warning: {w}" for w in self.warnings]
        return "\n".join(l for l in lines if l)


def _arr(v) -> np.ndarray:
    return np.asarray(v.a if isinstance(v, Vec) else v, dtype=float)


class LocalBuilder:
    def __init__(self):
        self.lib = StdLib()
        self.interp = Interpreter(self.lib.b)
        self.lib.interp = self.interp
        self.ctx = Context()
        self.planes: dict[str, Plane] = {
            name: Plane(np.zeros(3), np.array(n, float), np.array(x, float))
            for name, (x, _y, n) in DEFAULT_PLANES.items()}
        self.tools: dict[str, dict] = {}  # feature name -> {shapes, op, scope, default_scope}
        self.sketch_planes: dict[str, Plane] = {}
        self.warnings: list[str] = []
        self.partial = False
        self._qcache: dict[str, Function] = {}
        b = self.interp.globals.vars
        b["makeId"] = Builtin("makeId", lambda s: Id((s,)))
        b["sketchEntityQuery"] = Builtin("sketchEntityQuery", self._sketch_entity_query)

    # ------------------------------------------------------------ queries
    def _sketch_entity_query(self, sid, etype=None, ename=None):
        sk = self.ctx.sketches.get(tuple(sid))
        if sk is None:
            raise FSError(f"sketchEntityQuery: no sketch named {'.'.join(sid)}")
        out = []
        for name, edges, _c in sk.entities:
            if ename is None or name == ename or name.startswith(f"{ename}."):
                out += [Ent("EDGE", e, sk.body) for e in edges]
        return QList(out)

    def query(self, qt: QText) -> list[Ent]:
        text = qt.text.replace("@@", '')
        fn = self._qcache.get(text)
        if fn is None:
            self.interp.load(f"function __q() {{ return {text}; }}")
            fn = self.interp.globals.vars.pop("__q")
            self._qcache[text] = fn
        q = self.interp.call(fn, [], None)
        return q.eval(self.ctx)

    def plane_of(self, qt: QText, what: str) -> Plane:
        t = qt.text.replace("@@", "")
        for name, pl in self.planes.items():
            if t == f'qCreatedBy(makeId("{name}"), EntityType.FACE)' or t == f'qCreatedBy(makeId("{name}"))':
                return pl
        faces = [e for e in self.query(qt) if e.kind == "FACE"]
        if len(faces) != 1:
            raise FSError(f"{what}: expected one planar face or plane, got {len(faces)} entities")
        fp = G.face_plane(faces[0].shape)
        if fp is None:
            raise FSError(f"{what}: face is not planar")
        o, n, x = fp
        c = G.props(faces[0].shape, "area").CentreOfMass()
        self.warnings.append(f"{what}: sketching on a part face; local sketch orientation may differ from Onshape "
                             "(prefer a cPlane offset from a default plane)")
        return Plane(np.array([c.X(), c.Y(), c.Z()]), n, x)

    def axis_of(self, qt: QText, what: str) -> Line:
        for e in self.query(qt):
            if e.kind == "EDGE":
                d = G.edge_direction(e.shape)
                if d is not None:
                    p = G.BRep_Tool.Pnt_s(G.TopExp.FirstVertex_s(G.TopoDS.Edge(e.shape)))
                    return Line(np.array([p.X(), p.Y(), p.Z()]), d)
            if e.kind == "FACE" and G.face_geom(e.shape) == "CYLINDER":
                ax = G.BRepAdaptor_Surface(G.TopoDS.Face(e.shape)).Cylinder().Axis()
                o, d = ax.Location(), ax.Direction()
                return Line(np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()]))
        raise FSError(f"{what}: query does not resolve to a straight edge or cylindrical face")

    def direction_of(self, qt: QText, what: str) -> np.ndarray:
        try:
            return self.axis_of(qt, what).direction
        except FSError:
            return self.plane_of(qt, what).normal

    def bodies(self, qt, what, required=True) -> list[Body]:
        if qt is None:
            return []
        out = []
        for e in self.query(qt):
            b = e.body
            if b is not None and b.kind == "solid" and b not in out:
                out.append(b)
        if required and not out:
            raise FSError(f"{what}: query selects no parts")
        return out

    # ------------------------------------------------------------ helpers
    def _commit_new(self, oid, shapes):
        bodies = [Body(s, oid) for sh in shapes for s in G.solids_of(sh)]
        if not bodies:
            raise KernelFailure("operation produced no solid")
        self.ctx.commit(oid, None, new_bodies=bodies)
        return bodies

    def apply_op(self, rec: FeatureRec, oid: Id, shapes: list, record_tool=True):
        """NEW / ADD / REMOVE / INTERSECT a tool shape like Onshape's extrude-style features."""
        p = rec.params
        op = p.get("operationType").name if p.get("operationType") is not None else "NEW"
        if record_tool:
            self.tools[rec.name] = [{"shapes": shapes, "op": op, "scope": p.get("booleanScope"),
                                     "default": bool(p.get("defaultScope", False))}]
        if op == "NEW":
            self._commit_new(oid, shapes)
            return
        if p.get("defaultScope", False):
            scope = [b for b in self.ctx.bodies if b.kind == "solid"]
        else:
            scope = self.bodies(p.get("booleanScope"), f"{rec.name}: booleanScope", required=False)
        tool, _ = G.fuse_all(shapes) if len(shapes) > 1 else (shapes[0], None)
        if not scope:
            self.warnings.append(f"\"{rec.name}\": {op} with an empty boolean scope does nothing "
                                 "(Onshape: INFO). Set \"defaultScope\" : false and \"booleanScope\" to the part.")
            return
        if op == "ADD":
            shape, hist = G.fuse_all([b.shape for b in scope] + [tool])
            new = [Body(s, scope[0].creator) for s in G.solids_of(shape)]
            self.ctx.commit(oid, hist, new_bodies=new, removed_bodies=scope)
            return
        kind = {"REMOVE": "SUBTRACTION", "INTERSECT": "INTERSECTION"}[op]
        new, hists, changed = [], [], False
        before = sum(G.props(b.shape, "volume").Mass() for b in scope)
        for b in scope:
            shape, h = G.boolean(kind, [b.shape], [tool])
            hists.append(h)
            new += [Body(s, b.creator) for s in G.solids_of(shape)]
        from ..fslite.stdlib import _MultiHistory
        self.ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=scope)
        after = sum(G.props(b.shape, "volume").Mass() for b in new)
        if op == "REMOVE" and abs(before - after) < 1e-15:
            self.warnings.append(f"\"{rec.name}\": REMOVE did not cut anything (Onshape: INFO) - check the extrude "
                                 "direction (oppositeDirection) and depth")

    # ------------------------------------------------------------ features
    def f_newSketch(self, rec: FeatureRec):
        sk_rec = rec.sketch
        pl = self.plane_of(sk_rec.plane, f"sketch \"{rec.name}\"")
        oid = Id((rec.name,))
        self.ctx.register_op(oid, "newSketch")
        sk = Sketch(oid, pl)
        sk.ctx = self.ctx
        self.ctx.sketches[oid] = sk
        self.sketch_planes[rec.name] = pl
        for e in sk_rec.entities:
            g = e.geom
            if e.kind == "line":
                edges = [G.sketch_edge_line(pl, _arr(g["p0"]), _arr(g["p1"]))]
            elif e.kind == "circle":
                edges = [G.sketch_edge_circle(pl, _arr(g["c"]), mag(g["r"]))]
            else:
                tm = (g["t0"] + g["t1"]) / 2
                pts = [np.array([g["cx"] + g["r"] * math.cos(t), g["cy"] + g["r"] * math.sin(t)])
                       for t in (g["t0"], tm, g["t1"])]
                edges = [G.sketch_edge_arc3(pl, *pts)]
            sk.entities.append((e.name, edges, e.construction))
        G.solve_sketch(self.ctx, sk)

    def _regions(self, qt, what):
        faces = [e.shape for e in self.query(qt) if e.kind == "FACE"]
        if not faces:
            raise FSError(f"{what}: \"entities\" selects no sketch regions or faces")
        return faces

    def _normal_of_face(self, f):
        fp = G.face_plane(f)
        if fp is None:
            raise FSError("extrude: entity is not planar")
        return fp[1]

    def f_extrude(self, rec: FeatureRec):
        p = rec.params
        faces = self._regions(p.get("entities"), f"extrude \"{rec.name}\"")
        d = self._normal_of_face(faces[0])
        # sketch regions carry the sketch plane normal; face normals of sketch faces may be flipped
        for name, pl in self.sketch_planes.items():
            if f'"{name}"' in p["entities"].text.replace("@@", '"'):
                d = pl.normal
        if p.get("oppositeDirection"):
            d = -d
        bound = p["endBound"].name if p.get("endBound") is not None else "BLIND"

        def extent(bound, depth_key):
            if bound == "BLIND":
                return mag(p.get(depth_key, Q(0.025, (1, 0))))
            if bound == "THROUGH_ALL":
                return G.BIG
            raise KernelFailure(f"bound {bound} not implemented locally")
        e1 = extent(bound, "depth")
        e0 = 0.0
        if p.get("symmetric"):
            e1 = e0 = e1 / 2
        if p.get("hasSecondDirection"):
            b2 = p["secondDirectionBound"].name if p.get("secondDirectionBound") is not None else "BLIND"
            e0 = extent(b2, "secondDirectionDepth")
        shapes = [G.make_prism(G.translate(f, -e0 * d) if e0 else f, (e1 + e0) * d) for f in faces]
        self.apply_op(rec, Id((rec.name, "tool")), shapes)

    def f_revolve(self, rec: FeatureRec):
        p = rec.params
        faces = self._regions(p.get("entities"), f"revolve \"{rec.name}\"")
        ax = self.axis_of(p["axis"], f"revolve \"{rec.name}\" axis")
        d = -ax.direction if p.get("oppositeDirection") else ax.direction
        full = p.get("fullRevolve", True)
        ang = 2 * math.pi if full else mag(p.get("angle", Q(math.radians(30), (0, 1))))
        back = ang / 2 if (p.get("symmetric") and not full) else 0.0
        shapes = [G.make_revol(f, ax.origin, d, 0.0, ang) if not back else
                  G.make_revol(f, ax.origin, d, 2 * math.pi - back, back) for f in faces]
        self.apply_op(rec, Id((rec.name, "tool")), shapes)

    def f_loft(self, rec: FeatureRec):
        from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
        from OCP.BRepTools import BRepTools
        p = rec.params
        profiles = p.get("sheetProfilesArray") or []
        if len(profiles) < 2:
            raise FSError(f"loft \"{rec.name}\": needs at least two profiles in sheetProfilesArray")
        mk = BRepOffsetAPI_ThruSections(True, False)
        for item in profiles:
            faces = self._regions(item["sheetProfileEntities"], f"loft \"{rec.name}\" profile")
            mk.AddWire(BRepTools.OuterWire_s(G.TopoDS.Face(faces[0])))
        mk.Build()
        if not mk.IsDone():
            raise KernelFailure("loft failed")
        self.apply_op(rec, Id((rec.name, "tool")), [mk.Shape()])

    def helix_def(self, rec: FeatureRec) -> dict:
        """The opHelix definition the Helix feature makes (axis, start point, revolutions, pitch, handedness).
        CIRCLE: the circle's own frame (start angle measured from its x axis), AXIS: start radius from the
        axis' first point, start angle from perpendicularVector(axis). Cylinder faces (SURFACE) are not
        supported: their start angle depends on Onshape's face parameterisation."""
        p = rec.params
        what = f"helix \"{rec.name}\""
        axis_type = p["axisType"].name if p.get("axisType") is not None else "SURFACE"
        if axis_type == "SURFACE":
            faces = [e for e in self.query(p["entities"]) if e.kind == "FACE"] if p.get("entities") is not None else []
            if not faces:
                raise FSError(f"{what}: axisType SURFACE needs a cylindrical or conical face in \"entities\" "
                              "(for a sketch circle use \"axisType\" : AxisType.CIRCLE with \"edge\")")
            raise KernelFailure("helix on a cylinder face (axisType SURFACE) is not implemented locally; "
                                "use axisType CIRCLE with a sketch circle")
        if p.get("startType") is not None and p["startType"].name != "START_ANGLE":
            raise KernelFailure("helix start point (startType START_POINT) is not implemented locally")
        if p.get("endType") is not None and p["endType"].name != "HEIGHT":
            raise KernelFailure("helix end point (endType END_POINT) is not implemented locally")
        if axis_type == "CIRCLE":
            if p.get("edge") is None:
                raise FSError(f"{what}: axisType CIRCLE needs a circle or arc in \"edge\"")
            edges = [e for e in self.query(p["edge"]) if e.kind == "EDGE"]
            circ = None
            for e in edges:
                c = G.BRepAdaptor_Curve(G.TopoDS.Edge(e.shape))
                if c.GetType() == G.GeomAbs_Circle:
                    circ = c.Circle()
                    break
            if circ is None:
                raise FSError(f"{what}: \"edge\" selects no circle or arc")
            ax = circ.Position()
            v = lambda d: np.array([d.X(), d.Y(), d.Z()])
            origin, z, x, r = v(ax.Location()), v(ax.Direction()), v(ax.XDirection()), circ.Radius()
        else:
            ln = self.axis_of(p["axis"], f"{what} axis")
            origin, z = ln.origin, ln.direction
            x = self.lib.b["perpendicularVector"].fn(Vec(z)).a
            r = mag(p.get("startRadius", Q(0.025, (1, 0))))
        if p.get("oppositeDirection"):
            z = -z
        a = mag(p.get("startAngle", Q(0.0, (0, 1))))
        start = origin + r * (math.cos(a) * x + math.sin(a) * np.cross(z, x))
        path = p["pathType"].name if p.get("pathType") is not None else "TURNS"
        height = mag(p.get("height", Q(0.025, (1, 0))))
        turns = mag(p.get("revolutions", 4.0))
        pitch = mag(p.get("helicalPitch", Q(0.025, (1, 0))))
        if path == "TURNS":
            pitch = height / turns
        elif path == "PITCH":
            turns = height / pitch
        handed = p["handedness"].name if p.get("handedness") is not None else "CW"
        return {"origin": origin, "direction": z, "start": start, "turns": turns, "pitch": pitch,
                "clockwise": handed == "CW"}

    def f_helix(self, rec: FeatureRec):
        h = self.helix_def(rec)
        edge = G.helix_edge(h["origin"], h["direction"], h["start"], (0.0, h["turns"]), h["pitch"], h["clockwise"])
        oid = Id((rec.name,))
        self.ctx.commit(oid, None, new_bodies=[Body(edge, oid, kind="wire", helical=True)])

    def f_sweep(self, rec: FeatureRec):
        p = rec.params
        what = f"sweep \"{rec.name}\""
        if p.get("bodyType") is not None and p["bodyType"].name != "SOLID":
            raise KernelFailure(f"{p['bodyType'].name} sweeps are not implemented locally")
        if p.get("profileControl") is not None and p["profileControl"].name != "NONE":
            raise KernelFailure(f"profile control {p['profileControl'].name} is not implemented locally")
        for flag in ("hasTwist", "hasScale", "pathOppositeDirection", "extendToFullPath"):
            if p.get(flag):
                raise KernelFailure(f"{flag} is not implemented locally")
        faces = self._regions(p.get("profiles"), what)
        path = [e for e in self.query(p["path"]) if e.kind == "EDGE"] if p.get("path") is not None else []
        if not path:
            raise FSError(f"{what}: \"path\" selects no edges")
        frenet = all(e.body is not None and e.body.helical for e in path)
        try:
            shapes = [G.sweep_solid(f, [e.shape for e in path], frenet) for f in faces]
        except FSError as e:
            raise KernelFailure(str(e))
        self.apply_op(rec, Id((rec.name, "tool")), shapes)

    def _edge_groups(self, qt, what):
        groups: dict = {}
        for e in self.query(qt):
            if e.body is None or e.body.kind != "solid":
                continue
            edges = [e.shape] if e.kind == "EDGE" else G.subshapes(e.shape, G.TopAbs_EDGE) if e.kind == "FACE" else []
            groups.setdefault(id(e.body), (e.body, []))[1].extend(edges)
        if not groups:
            raise FSError(f"{what}: \"entities\" selects no edges (Onshape: FILLET_SELECT_EDGES)")
        return list(groups.values())

    def _modify(self, oid, groups, fn):
        from ..fslite.stdlib import _MultiHistory
        hists, new, removed = [], [], []
        for body, items in groups:
            try:
                shape, h = fn(body.shape, items)
            except FSError as e:
                raise KernelFailure(str(e))
            hists.append(h)
            removed.append(body)
            new += [Body(s, body.creator) for s in G.solids_of(shape)]
        self.ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=removed)

    def f_fillet(self, rec):
        r = mag(rec.params.get("radius", Q(0.005, (1, 0))))
        self._modify(Id((rec.name,)), self._edge_groups(rec.params.get("entities"), f"fillet \"{rec.name}\""),
                     lambda s, es: G.fillet(s, es, r))

    def f_chamfer(self, rec):
        p = rec.params
        if p.get("chamferType") is not None and p["chamferType"].name != "EQUAL_OFFSETS":
            raise KernelFailure("only EQUAL_OFFSETS chamfers are implemented locally")
        w = mag(p.get("width", Q(0.005, (1, 0))))
        self._modify(Id((rec.name,)), self._edge_groups(p.get("entities"), f"chamfer \"{rec.name}\""),
                     lambda s, es: G.chamfer(s, es, w))

    def f_shell(self, rec):
        p = rec.params
        t = mag(p.get("thickness", Q(0.0025, (1, 0))))
        t = t if p.get("oppositeDirection") else -t  # standard Shell hollows inward by default
        groups: dict = {}
        for e in self.query(p["entities"]):
            if e.kind == "FACE" and e.body is not None:
                groups.setdefault(id(e.body), (e.body, []))[1].append(e.shape)
        if not groups:
            raise FSError(f"shell \"{rec.name}\": \"entities\" selects no faces to remove")
        self._modify(Id((rec.name,)), list(groups.values()), lambda s, fs: G.shell(s, fs, t))

    def f_cPlane(self, rec):
        p = rec.params
        if p.get("cplaneType") is not None and p["cplaneType"].name != "OFFSET":
            raise KernelFailure(f"plane type {p['cplaneType'].name} not implemented locally")
        base = self.plane_of(p["entities"], f"plane \"{rec.name}\"")
        off = mag(p.get("offset", Q(0.025, (1, 0))))
        if p.get("oppositeDirection"):
            off = -off
        self.planes[rec.name] = Plane(base.origin + off * base.normal, base.normal, base.x)

    def _pattern(self, rec, transforms: list[np.ndarray]):
        p = rec.params
        ptype = p["patternType"].name if p.get("patternType") is not None else "PART"
        if ptype == "FEATURE":
            produced = []  # so that this pattern can itself be patterned/mirrored later
            for fid in p.get("instanceFunction") or []:
                name = ".".join(fid)
                entries = self.tools.get(name)
                if entries is None:
                    raise KernelFailure(f"feature pattern of \"{name}\" (only extrude/revolve/loft features and "
                                        "their patterns can be patterned locally)")
                for j, tool in enumerate(entries):
                    for k, m in enumerate(transforms):
                        shapes = [G.transform_shape(s, m)[0] for s in tool["shapes"]]
                        fake = FeatureRec("extrude", name, {
                            "operationType": _Enum(tool["op"]), "booleanScope": tool["scope"],
                            "defaultScope": tool["default"]})
                        self.apply_op(fake, Id((rec.name, f"{name}#{j}.{k}")), shapes, record_tool=False)
                        produced.append({**tool, "shapes": shapes})
            self.tools[rec.name] = produced
            return
        if ptype != "PART":
            raise KernelFailure(f"{ptype} patterns are not implemented locally")
        bodies = self.bodies(p.get("entities"), f"pattern \"{rec.name}\"")
        for k, m in enumerate(transforms):
            shapes = [G.transform_shape(b.shape, m)[0] for b in bodies]
            fake = FeatureRec("extrude", rec.name, {"operationType": p.get("operationType") or _Enum("NEW"),
                                                     "booleanScope": p.get("booleanScope"),
                                                     "defaultScope": p.get("defaultScope", False)})
            self.apply_op(fake, Id((rec.name, f"i{k}")), shapes, record_tool=False)

    def f_linearPattern(self, rec):
        p = rec.params
        d = self.direction_of(p["directionOne"], f"linear pattern \"{rec.name}\"")
        d = -d if p.get("oppositeDirection") else d
        n1, s1 = int(mag(p.get("instanceCount", 2))), mag(p.get("distance", Q(0.025, (1, 0))))
        steps = [(i * s1 * d) for i in range(n1)]
        if p.get("hasSecondDir"):
            d2 = self.direction_of(p["directionTwo"], f"linear pattern \"{rec.name}\" direction two")
            d2 = -d2 if p.get("oppositeDirectionTwo") else d2
            n2, s2 = int(mag(p.get("instanceCountTwo", 1))), mag(p.get("distanceTwo", Q(0.025, (1, 0))))
            steps = [a + j * s2 * d2 for a in steps for j in range(n2)]
        mats = []
        for v in steps[1:]:
            m = np.eye(4)
            m[:3, 3] = v
            mats.append(m)
        self._pattern(rec, mats)

    def f_circularPattern(self, rec):
        p = rec.params
        ax = self.axis_of(p["axis"], f"circular pattern \"{rec.name}\"")
        n = int(mag(p.get("instanceCount", 4)))
        ang = mag(p.get("angle", Q(2 * math.pi, (0, 1))))
        if p.get("equalSpace", True):
            step = ang / n if abs(ang - 2 * math.pi) < 1e-9 else ang / max(n - 1, 1)
        else:
            step = ang
        if p.get("oppositeDirection"):
            step = -step
        mats = []
        from ..fslite.stdlib import StdLib as _S  # rotationAround lives in the stdlib builtins
        rot = self.lib.b["rotationAround"].fn
        for i in range(1, n):
            mats.append(rot(ax, Q(i * step, (0, 1))).m)
        self._pattern(rec, mats)

    def f_mirror(self, rec):
        pl = self.plane_of(rec.params["mirrorPlane"], f"mirror \"{rec.name}\"")
        self._pattern(rec, [self.lib.b["mirrorAcross"].fn(pl).m])

    def f_booleanBodies(self, rec):
        p = rec.params
        op = p["operationType"].name if p.get("operationType") is not None else "UNION"
        tools = self.bodies(p.get("tools"), f"boolean \"{rec.name}\" tools")
        oid = Id((rec.name,))
        if op == "UNION":
            shape, h = G.fuse_all([b.shape for b in tools])
            self.ctx.commit(oid, h, new_bodies=[Body(s, tools[0].creator) for s in G.solids_of(shape)],
                            removed_bodies=tools)
            return
        targets = self.bodies(p.get("targets"), f"boolean \"{rec.name}\" targets")
        kind = "SUBTRACTION" if op == "SUBTRACTION" else "INTERSECTION"
        from ..fslite.stdlib import _MultiHistory
        new, hists = [], []
        for t in targets:
            shape, h = G.boolean(kind, [t.shape], [b.shape for b in tools])
            hists.append(h)
            new += [Body(s, t.creator) for s in G.solids_of(shape)]
        removed = targets + ([] if p.get("keepTools") else tools)
        self.ctx.commit(oid, _MultiHistory(hists), new_bodies=new, removed_bodies=removed)

    def f_deleteBodies(self, rec):
        bodies = self.bodies(rec.params.get("entities"), f"delete \"{rec.name}\"")
        self.ctx.commit(Id((rec.name,)), None, removed_bodies=bodies)

    # ------------------------------------------------------------ run
    def build(self, tr: TraceResult) -> LocalResult:
        t0 = time.time()
        res = LocalResult(False)
        for rec in tr.features:
            fn = getattr(self, f"f_{rec.feature_type}", None)
            if fn is None:
                self.partial = True
                self.warnings.append(f"\"{rec.name}\": {rec.feature_type} is not implemented in the local builder "
                                     "(Onshape will build it; local geometry is incomplete)")
                continue
            try:
                fn(rec)
            except KernelFailure as e:
                self.partial = True
                self.warnings.append(f"\"{rec.name}\" ({rec.feature_type}): local kernel could not build it ({e}); "
                                     "skipped locally - Onshape may still succeed")
            except FSError as e:
                res.error = f"feature \"{rec.name}\" ({rec.feature_type}, script line {rec.line}): {e.msg}"
                res.error_feature, res.error_line = rec.name, rec.line
                break
            except Exception as e:  # kernel exceptions from OCP
                self.partial = True
                self.warnings.append(f"\"{rec.name}\" ({rec.feature_type}): local kernel error {type(e).__name__}: "
                                     f"{e}; skipped locally")
        else:
            res.ok = True
        res.run = RunResult(ok=True, context=self.ctx, output=list(self.ctx.log))
        res.warnings, res.partial = self.warnings, self.partial
        res.seconds = time.time() - t0
        return res


class _Enum:
    def __init__(self, name):
        self.name = name


def build_local(tr: TraceResult) -> LocalResult:
    return LocalBuilder().build(tr)


def render(res: LocalResult, path, title=""):
    return render_png(res.run, path, title) if res.run else None
