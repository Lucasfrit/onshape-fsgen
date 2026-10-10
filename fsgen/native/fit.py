"""Fit check: put the printed parts in their assembled position and look for interference.

A part script lays its parts out in print orientation. A fit file next to it (parts/<name>.fit.toml) says where each
part goes in the assembly, adds other components (a STEP file of the real thing the parts must fit, or simple
cylinders and boxes for wires and pins) and lists motions to try (turning a hinge, sliding a part off). The check
reports the overlap volume of every pair in the assembled position and, for each motion, the overlap at each step:
a snap fit shows as a small overlap near the closed position, a collision as a large one. 0 Onshape calls.

    # parts/clamp.fit.toml (all lengths in mm, angles in degrees)
    [parts.A]
    at = [0, 20, 5]                 # a point inside the part in the print layout: picks the body
    [parts.B]
    at = [0, -20, 5]
    place = [{ rotate = 180, axis = [1, 0, 0], origin = [0, 0, 10.7] }]   # print layout -> assembly, in order

    [components.core]
    step = "core.step"              # path relative to the fit file, or absolute
    place = [{ move = [0, 0, -15] }, { rotate = 120, axis = [1, 1, 1] }, { move = [0, 14.1, 10.7] }]
    [components.pin]
    cylinder = { radius = 0.875, length = 32, center = [0, 0, 10.7], axis = [1, 0, 0] }

    [[motions]]
    name = "open the hinge"
    moving = ["B"]                  # names of parts/components that move
    rotate = { axis = [1, 0, 0], origin = [0, 0, 10.7] }   # or: slide = [0, 0, 1]
    steps = [0.5, 1.5, 3, 10, 45, 90, 180]                  # degrees or mm
    against = ["A", "core"]         # optional (default: everything that does not move)
    expect = "snap"                 # optional: free | snap | blocked | collides

    [check]
    ignore = [["pin", "A"]]         # pairs allowed to overlap in the assembled position
    snap_limit = 3.0                # mm3: a larger overlap counts as a collision

Placement ops: { move = [x, y, z] }, { rotate = deg, axis = [..], origin = [..] }, or
{ matrix = [[..], [..], [..]], offset = [..] } with a rotation matrix. A printed part can only be moved and turned,
never mirrored: a matrix with determinant -1 is rejected.
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopAbs import TopAbs_IN, TopAbs_ON
from OCP.TopoDS import TopoDS_Shape

from ..fslite import geom as G
from .meshing import to_mm

TOL = 1e-3          # mm3: below this two shapes only touch
COMPONENT_COLOR = "#5b6069"


class FitError(Exception):
    pass


@dataclass
class Placed:
    name: str
    kind: str                     # "part" or "component"
    shape: TopoDS_Shape           # mm, assembled position
    printed: TopoDS_Shape | None  # mm, print layout (parts only)
    color: str | None = None
    index: int | None = None      # which built body (parts only), in build order


@dataclass
class MotionResult:
    name: str
    unit: str
    steps: list
    overlaps: list
    worst: dict = field(default_factory=dict)   # step -> "a & b" with the largest overlap
    verdict: str = ""
    expect: str | None = None

    @property
    def ok(self) -> bool:
        return self.expect is None or self.expect == self.verdict


@dataclass
class FitResult:
    placed: list[Placed]
    clashes: list[tuple]          # (a, b, volume mm3) in the assembled position
    motions: list[MotionResult]

    @property
    def ok(self) -> bool:
        return not self.clashes and all(m.ok for m in self.motions)

    def report(self) -> str:
        lines = ["assembled: " + ", ".join(p.name for p in self.placed)]
        if self.clashes:
            lines.append("OVERLAPS in the assembled position:")
            lines += [f"  {a} & {b}: {v:.3f} mm3" for a, b, v in self.clashes]
        else:
            lines.append("assembled position: no overlaps")
        for m in self.motions:
            steps = ", ".join(f"{s:g}{m.unit}: {v:.2f}" for s, v in zip(m.steps, m.overlaps))
            exp = "" if m.expect is None else (" (as expected)" if m.ok else f" - EXPECTED {m.expect.upper()}")
            lines.append(f"motion \"{m.name}\": {m.verdict}{exp}")
            lines.append(f"  overlap mm3 at {steps}")
            hits = [f"{s:g}{m.unit} {who}" for s, who in m.worst.items()]
            if hits and m.verdict != "free":
                lines.append("  touching: " + "; ".join(hits[:4]))
        lines.append("fit check " + ("passed" if self.ok else "FAILED"))
        return "\n".join(lines)


# ------------------------------------------------------------------ geometry helpers
def _vec3(v, what) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    if a.shape != (3,):
        raise FitError(f"{what}: expected 3 numbers, got {v!r}")
    return a


def op_trsf(op: dict) -> gp_Trsf:
    t = gp_Trsf()
    if "move" in op:
        t.SetTranslation(gp_Vec(*_vec3(op["move"], "move")))
    elif "rotate" in op:
        axis = _vec3(op.get("axis", [0, 0, 1]), "rotate axis")
        origin = _vec3(op.get("origin", [0, 0, 0]), "rotate origin")
        t.SetRotation(gp_Ax1(gp_Pnt(*origin), gp_Dir(*axis)), math.radians(float(op["rotate"])))
    elif "matrix" in op:
        m = np.asarray(op["matrix"], dtype=float)
        if m.shape != (3, 3) or not np.allclose(m @ m.T, np.eye(3), atol=1e-6):
            raise FitError(f"matrix must be a 3x3 rotation matrix, got {op['matrix']!r}")
        if np.linalg.det(m) < 0:
            raise FitError("matrix has determinant -1 (a mirror): a printed part cannot be mirrored; "
                           "check the print-layout map of this part")
        o = _vec3(op.get("offset", [0, 0, 0]), "offset")
        t.SetValues(*m[0], o[0], *m[1], o[1], *m[2], o[2])
    else:
        raise FitError(f"unknown placement op {op!r}: use move, rotate or matrix")
    return t


def compose(ops) -> gp_Trsf:
    total = gp_Trsf()
    for op in ops or []:
        total = op_trsf(op).Multiplied(total)   # apply in the order listed
    return total


def moved(shape: TopoDS_Shape, t: gp_Trsf) -> TopoDS_Shape:
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def overlap(a: TopoDS_Shape, b: TopoDS_Shape) -> float:
    (alo, ahi), (blo, bhi) = G.bbox([a]), G.bbox([b])
    if np.any(alo > bhi + 1e-6) or np.any(blo > ahi + 1e-6):
        return 0.0
    common = BRepAlgoAPI_Common(a, b)
    if not common.IsDone():
        raise FitError("boolean failed while checking an overlap")
    return max(0.0, G.props(common.Shape(), "volume").Mass())


def contains(shape: TopoDS_Shape, p) -> bool:
    c = BRepClass3d_SolidClassifier(shape, gp_Pnt(*p), 1e-6)
    return c.State() in (TopAbs_IN, TopAbs_ON)


def _component_shape(name: str, spec: dict, base: Path) -> TopoDS_Shape:
    if "step" in spec:
        import build123d as bd
        path = Path(spec["step"]).expanduser()
        path = path if path.is_absolute() else base / path
        if not path.exists():
            raise FitError(f"component {name}: STEP file not found: {path}")
        shape = bd.import_step(str(path))
        solids = shape.solids()
        if "solid" in spec:
            solids = [solids[int(spec["solid"])]]
        return G.compound([s.wrapped for s in solids])
    if "cylinder" in spec:
        c = spec["cylinder"]
        axis = _vec3(c.get("axis", [0, 0, 1]), f"component {name} axis")
        axis = axis / np.linalg.norm(axis)
        length = float(c["length"])
        start = _vec3(c.get("center", [0, 0, 0]), f"component {name} center") - axis * length / 2
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*start), gp_Dir(*axis)), float(c["radius"]), length).Shape()
    if "box" in spec:
        b = spec["box"]
        size = _vec3(b["size"], f"component {name} size")
        lo = _vec3(b.get("center", [0, 0, 0]), f"component {name} center") - size / 2
        return BRepPrimAPI_MakeBox(gp_Pnt(*lo), *size).Shape()
    raise FitError(f"component {name}: give step, cylinder or box")


# ------------------------------------------------------------------ main
def default_spec_path(script: Path) -> Path:
    return script.with_suffix("").with_suffix(".fit.toml") if script.suffix else script.with_name(script.name + ".fit.toml")


def load_spec(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise FitError(f"{path}: {e}")


def place(solids_m, spec: dict, base: Path) -> list[Placed]:
    """Match each named part to a built body (by its `at` point) and place it; add the components."""
    bodies = [to_mm(s) for s in solids_m]
    out, used = [], {}
    for name, p in (spec.get("parts") or {}).items():
        if "at" not in p:
            raise FitError(f"part {name}: give `at`, a point inside it in the print layout")
        at = _vec3(p["at"], f"part {name} at")
        hits = [i for i, b in enumerate(bodies) if contains(b, at)]
        if len(hits) != 1:
            boxes = "; ".join(f"part {i + 1}: " + " .. ".join(str(np.round(v, 1).tolist()) for v in G.bbox([b]))
                              for i, b in enumerate(bodies))
            raise FitError(f"part {name}: the point {at.tolist()} is inside {len(hits)} built parts (need 1). "
                           f"Built parts (bounding boxes, mm): {boxes}")
        if hits[0] in used:
            raise FitError(f"parts {used[hits[0]]} and {name} pick the same body")
        used[hits[0]] = name
        out.append(Placed(name, "part", moved(bodies[hits[0]], compose(p.get("place"))), bodies[hits[0]], p.get("color"),
                          hits[0]))
    for name, c in (spec.get("components") or {}).items():
        out.append(Placed(name, "component", moved(_component_shape(name, c, base), compose(c.get("place"))), None,
                          c.get("color", COMPONENT_COLOR)))
    if not out:
        raise FitError("the fit file names no parts or components")
    return out


def _classify(overlaps, snap_limit):
    if all(v < TOL for v in overlaps):
        return "free"
    if overlaps[-1] >= TOL:
        return "blocked"
    return "snap" if max(overlaps) <= snap_limit else "collides"


def run_fit(solids_m, spec: dict, base: Path) -> FitResult:
    placed = place(solids_m, spec, base)
    by_name = {p.name: p for p in placed}
    check = spec.get("check") or {}
    ignore = {frozenset(pair) for pair in check.get("ignore", [])}
    snap_limit = float(check.get("snap_limit", 3.0))
    clashes = []
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if frozenset((a.name, b.name)) in ignore:
                continue
            v = overlap(a.shape, b.shape)
            if v >= TOL:
                clashes.append((a.name, b.name, round(v, 3)))
    motions = []
    for m in spec.get("motions") or []:
        name = m.get("name", "motion")
        moving = m.get("moving") or []
        missing = [n for n in moving + list(m.get("against", [])) if n not in by_name]
        if missing or not moving:
            raise FitError(f"motion {name}: unknown or missing names {missing or 'moving'}; known: {', '.join(by_name)}")
        against = [n for n in (m.get("against") or by_name) if n not in moving]
        if "rotate" in m:
            r = m["rotate"]
            ax = gp_Ax1(gp_Pnt(*_vec3(r.get("origin", [0, 0, 0]), f"{name} origin")),
                        gp_Dir(*_vec3(r["axis"], f"{name} axis")))
            unit = " deg"

            def step_trsf(s, ax=ax):
                t = gp_Trsf()
                t.SetRotation(ax, math.radians(s))
                return t
        elif "slide" in m:
            d = _vec3(m["slide"], f"{name} slide")
            d = d / np.linalg.norm(d)
            unit = " mm"

            def step_trsf(s, d=d):
                t = gp_Trsf()
                t.SetTranslation(gp_Vec(*(d * s)))
                return t
        else:
            raise FitError(f"motion {name}: give rotate or slide")
        steps = [float(s) for s in m.get("steps", [])]
        if not steps:
            raise FitError(f"motion {name}: give steps (degrees or mm)")
        res = MotionResult(name, unit, steps, [], expect=m.get("expect"))
        for s in steps:
            t = step_trsf(s)
            total, worst = 0.0, (0.0, "")
            for mv in moving:
                shp = moved(by_name[mv].shape, t)
                for other in against:
                    v = overlap(shp, by_name[other].shape)
                    total += v
                    if v > worst[0]:
                        worst = (v, f"{mv} & {other}")
            res.overlaps.append(round(total, 3))
            if worst[0] >= TOL:
                res.worst[s] = worst[1]
        res.verdict = _classify(res.overlaps, snap_limit)
        if res.expect not in (None, "free", "snap", "blocked", "collides"):
            raise FitError(f"motion {name}: expect must be free, snap, blocked or collides")
        motions.append(res)
    return FitResult(placed, clashes, motions)


def export_assembly(placed: list[Placed], path: Path) -> Path:
    import build123d as bd
    path.parent.mkdir(parents=True, exist_ok=True)
    bd.export_step(bd.Compound(G.compound([p.shape for p in placed])), str(path))
    return path
