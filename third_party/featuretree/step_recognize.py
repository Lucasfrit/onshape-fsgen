"""step_recognize.py — recover a featuretree IR from a STEP B-rep (feature RECOGNITION).

A STEP file is a dumb B-rep: geometry, no feature tree. You cannot *convert* it to an IR;
you can only *infer* one. The insight this leans on: **most machined/printed parts are a 2D
profile EXTRUDED along some axis, or REVOLVED about one** (plus holes). So the core intelligence
is CROSS-SECTIONAL. recognize() tries both:
  * EXTRUDE — find the axis the solid is a prismatic extrusion along (every face is
    planar-perpendicular, planar-parallel, or a cylinder parallel to it), rotate it onto Z, and
    recover the profile + through/blind holes. Orientation-agnostic (X/Y/Z or any face normal).
  * REVOLVE — find the axis the solid is unchanged under rotation about (a body of revolution),
    take the MERIDIAN (a half-plane section through the axis) as the profile, emit revolve(360).
Profiles may have straight edges AND circular arcs (DXF bulge).

MULTI-AXIS RECOVERY closes real machined parts that no single extrude/revolve can: when the base
extrude doesn't verify, the residual (base − original) is carved feature by feature. Every leftover
lump is itself a 2D profile extruded along ITS OWN axis — a floor / through-web pocket along the
main axis, a cross-hole perpendicular to it — so each is recognized and subtracted as a `prism_cut`
(a placed profile-along-an-axis), looping until the residual vanishes. On the NIST CTC-01 test part
this takes a 155%-off base extrude to a verified reconstruction, leaving only the edge chamfers.

The honesty comes from SELF-VERIFICATION: the recognized IR is re-emitted through b3d_emit and its
volume + (rotation-tolerant) bounding box are compared to the original STEP. Several axes can look
prismatic, so recognize() gathers a candidate per axis and lets verification pick the winner — first
a base that verifies on its own, else the first whose recovery verifies. Every result is either
"verified" (same volume and extents within tolerance — NOT proof of the same solid: a volume test
lets over-cut and uncut material cancel; use --iou / iou_check.py for a two-sided check) or PARTIAL
with the residual reported.
Out of scope (surfaced as residual, never silently wrong): edge fillets/chamfers, additive bosses,
lofts/sweeps/freeform, and profiles whose boundary has splines/ellipses (lines + circular arcs only).

    python step_recognize.py part.step                    # recognize + print the tree/verdict
    python step_recognize.py part.step --stl out.stl      # + write the recovered solid (mesh viewer)
    python step_recognize.py part.step --fcstd out.FCStd  # + write an editable FreeCAD tree
    python step_recognize.py part.step --emit out.ir.json # + write the recovered IR
    python step_recognize.py part.step --iou              # + Boolean-free IoU vs the input (iou_check.py)
    python step_recognize.py part.step --intent           # + design intent: holes, patterns, units (intent.py)
    from step_recognize import recognize;  spec, report = recognize("part.step")
"""

import json
import os
import math
import sys
from pathlib import Path

from build123d import Axis, GeomType, Plane, Pos, Rectangle, Vector, import_step

import ir as IR
import b3d_emit

EPS = 1e-3
ANG = 1e-2           # direction tolerance: |dot|<ANG == perpendicular, >1-ANG == parallel
MAX_REGIONS = 8      # cap on disjoint cross-section regions emitted as separate pads

# Fraction of faces allowed to be NON-conforming before an axis is dropped as an extrude candidate.
# This is a RECALL knob, not a correctness knob: verification (_verify) is downstream and independent,
# so loosening it can only turn a refusal into a PARTIAL or VERIFIED — it cannot manufacture a false
# accept. Threaded / heavily-filleted parts (NIST FTC-07, FTC-10) carry so much torus/cone/sphere area
# that a tight gate rejects them before the profile is ever examined.
AXIS_BAD_FRAC = 0.75
VOL_TOL = 0.005      # 0.5% volume agreement -> "verified"
IOU_TOL = 0.995      # ...AND >= 99.5% two-sided overlap (see _verify): volume alone lets errors cancel
IOU_SAMPLES = 12000  # points for that check: ~+-0.2% at 99.5%, a few seconds on a large part
DIM_TOL = 0.05       # mm bbox-size agreement
RECOVER_PASSES = 4   # multi-axis residual-carve passes (each re-decomposes what's left)
RECOVER_EPS = 5.0    # mm^3: ignore boolean slivers / zero-volume sheets in the residual


# --- cross-sectional intelligence: is this solid a 2D profile extruded along SOME axis? ---------
# A prismatic extrusion along axis a has EVERY face either planar-perpendicular to a (an end cap),
# planar-parallel to a (a flat side wall), or cylindrical with its axis parallel to a (a rounded
# wall or a through hole). Any other face (angled plane, cone/sphere/torus/bspline) rules a out.
# We find such an axis, rotate it onto Z, and then the Z-prismatic profile+holes logic applies to
# ANY orientation — the general "most parts are an extrude of a 2D wire" case.

def _cyl_axis(face):
    ces = face.edges().filter_by(GeomType.CIRCLE)
    if len(ces) < 2:
        return None
    a, b = ces[0].arc_center, ces[1].arc_center
    d = Vector(b.X - a.X, b.Y - a.Y, b.Z - a.Z)
    return d.normalized() if d.length > EPS else None


def _extrude_nonconforming(solid, a):
    """How many faces are NOT consistent with a prismatic extrusion along `a` (angled walls, holes
    across the axis, freeform). 0 == a clean extrude; a few == a mostly-extruded part with chamfers
    or cross-holes (recoverable as PARTIAL, the residual flagged)."""
    bad = 0
    for f in solid.faces():
        gt = f.geom_type
        if gt == GeomType.PLANE:
            d = abs(_normal(f).normalized().dot(a))
            if not (d < ANG or d > 1 - ANG):
                bad += 1                          # angled wall (chamfer/draft)
        elif gt in (GeomType.CYLINDER, GeomType.CONE):
            ax = _cyl_axis(f)                      # cone (countersink) fine if its axis is along a
            if ax is not None and abs(ax.dot(a)) < 1 - ANG:
                bad += 1                          # a hole across the axis (cross-hole)
        else:
            bad += 1                              # sphere/torus/bspline
    return bad


def _extrude_axes(solid):
    """Candidate extrude axes RANKED by how prismatic the solid is along each (fewest non-conforming
    faces first), keeping only axes that are mostly-prismatic (n_bad <= 40% of faces). A ranked list,
    not just the winner, so the recognizer can fall through to the next axis when the best one yields
    a fragmented cross-section (a cross-feature can make an orthogonal axis look deceptively clean)."""
    faces = solid.faces()
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for f in faces.filter_by(GeomType.PLANE):
        cands.append(_normal(f).normalized())
    for gt in (GeomType.CYLINDER, GeomType.CONE):
        for f in faces.filter_by(gt):
            ax = _cyl_axis(f)
            if ax:
                cands.append(ax)
    scored, seen = [], []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a.normalized())
        scored.append((a.normalized(), _extrude_nonconforming(solid, a)))
    lim = AXIS_BAD_FRAC * len(faces)
    return sorted([(a, b) for a, b in scored if b <= lim], key=lambda t: t[1])


def _find_extrude_axis(solid):
    """The single best-fit extrude axis (fewest non-conforming faces). Returns (axis, n_bad), or
    (None, None) if even the best axis is mostly non-conforming."""
    axes = _extrude_axes(solid)
    return axes[0] if axes else (None, None)


def _align_to_z(solid, a):
    """Rotate the solid so axis `a` lands on +Z (the IR's extrude/revolve axis)."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    if abs(a.dot(z)) > 1 - ANG:
        return solid                              # already along Z
    axis_dir = a.cross(z)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, a.dot(z)))))
    return solid.rotate(Axis((0, 0, 0), axis_dir.to_tuple()), angle)


def _canon_axis(a):
    """Flip an axis so its dominant component is positive — a stable canonical direction (a prism is
    the same whether swept +axis or -axis), which keeps prism_cut off the -Z degenerate."""
    t = a.to_tuple()
    i = max(range(3), key=lambda k: abs(t[k]))
    return a if t[i] >= 0 else Vector(-a.X, -a.Y, -a.Z)


def _unalign_vec(v, a):
    """Inverse of _align_to_z, on a direction vector: map the Z-frame vector `v` back into axis a's
    frame (Rodrigues rotation by -angle about a×z). So a profile recovered in the aligned Z-frame can
    be placed back at a's true orientation for a prism_cut."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    d = max(-1.0, min(1.0, a.dot(z)))
    if d > 1 - ANG:
        return v
    if d < -1 + ANG:
        return Vector(v.X, -v.Y, -v.Z)            # a == -Z: 180deg about X
    k = a.cross(z).normalized()
    theta = -math.acos(d)
    ct, st = math.cos(theta), math.sin(theta)
    kv, kd = k.cross(v), k.dot(v)
    return Vector(v.X * ct + kv.X * st + k.X * kd * (1 - ct),
                  v.Y * ct + kv.Y * st + k.Y * kd * (1 - ct),
                  v.Z * ct + kv.Z * st + k.Z * kd * (1 - ct))


def _face_polys(face):
    """A planar face -> [outer, hole, hole, ...] wires as (u, v[, bulge]) rings (a circle becomes a
    2-arc ring), preserving islands. None if any wire is a spline/ellipse (recover honestly, no fake)."""
    polys = []
    for w in [face.outer_wire()] + list(face.inner_wires()):
        cl = _classify_wire(w, [])
        if cl is None:
            return None
        if cl[0] == "circle":
            _, cx, cy, r = cl
            polys.append([[cx - r, cy, 1.0], [cx + r, cy, 1.0]])   # full circle = two 180deg arcs
        else:
            polys.append(cl[1])
    return polys


def _component_prism(comp, name):
    """A residual component -> a prism_cut IR feature: its recognized 2D profile extruded along its
    OWN dominant axis over its own extent, placed at its true location. None if it isn't a single
    clean profile this pass (multi-region -> a later pass re-decomposes it; sliver -> left honest)."""
    try:
        a, _ = _find_extrude_axis(comp)
        if a is None:
            return None
        a = _canon_axis(a.normalized())
        al = _align_to_z(comp, a)
        lb = al.bounding_box()
        cx, cy = 0.5 * (lb.min.X + lb.max.X), 0.5 * (lb.min.Y + lb.max.Y)   # lumps are off-center
        zc = lb.min.Z + 0.5 * lb.size.Z
        sec = al.intersect(Pos(cx, cy, zc) * Rectangle(lb.size.X + 50, lb.size.Y + 50)).faces()
        if len(sec) != 1:
            return None
        polys = _face_polys(sec[0])
        if polys is None:
            return None
        b3d_emit._polys_region(polys, Plane.XY)     # validate: raises on a degenerate profile
        origin = _unalign_vec(Vector(0, 0, lb.min.Z), a)
        xdir = _unalign_vec(Vector(1, 0, 0), a)
        return IR.prism_cut(name, origin=origin.to_tuple(), normal=a.to_tuple(),
                            xdir=xdir.to_tuple(), depth=round(lb.size.Z, 4), polys=polys)
    except Exception:
        return None


def _is_hole_prism(feat):
    p = feat.get("polys", [])
    return len(p) == 1 and len(p[0]) == 2 and all(len(v) > 2 and abs(abs(v[2]) - 1) < 0.1 for v in p[0])


def _robust_cut(a, b):
    """a - b, or None. OpenCASCADE sometimes returns a null shape for a Boolean between solids with
    near-coincident faces (FTC-08's pan, once carving has started); a small fuzzy tolerance usually
    resolves it. Returning None lets the caller stop carving and KEEP what it has, instead of
    losing every feature to one failed Boolean."""
    try:
        return a - b
    except Exception:
        pass
    try:
        from build123d import Shape
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.TopTools import TopTools_ListOfShape
        op = BRepAlgoAPI_Cut()
        args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        args.Append(a.wrapped)
        tools.Append(b.wrapped)
        op.SetArguments(args)
        op.SetTools(tools)
        op.SetFuzzyValue(1e-4)
        op.Build()
        if op.IsDone() and not op.Shape().IsNull():
            return Shape.cast(op.Shape())
    except Exception:
        pass
    return None


def _recover_multiaxis(spec, orig, axis, name):
    """Close the residual of a best-effort extrude (outline + through-holes that didn't verify) by
    recovering the machined interior as prism_cuts: (A) floor pockets — every significant intermediate
    perpendicular-to-axis planar face is a pocket floor; (B) a residual-carve loop — whatever's left
    (multi-level / through-web pockets, cross-axis holes) is a set of separate lumps, each a clean 2D
    profile extruded along its OWN axis. All prism_cuts, so the whole thing self-verifies via re-emit.
    Returns (recovered_spec, counts)."""
    feats = list(spec["features"])
    counts = {"pockets": 0, "holes": 0, "residual_lumps": 0}

    # Work in the EMITTED base's own frame. The base sketch uses the aligned part's own X/Y
    # coordinates and b3d_emit pads it deterministically +Z from z=0, so the two frames differ ONLY
    # by the part's base height along Z. (This used to align bounding-box corners in X and Y too,
    # which is right only when the base outline spans the part's full extent; FTC-07's rim lip
    # overhangs its drafted walls by 3.18 mm, and every carved feature landed 3.18 mm off.)
    part, _ = b3d_emit.emit(IR.part(name, *feats))
    pbb = part.bounding_box()
    aligned = _align_to_z(orig, axis)
    abb = aligned.bounding_box()
    aligned0 = aligned.translate((0.0, 0.0, pbb.min.Z - abb.min.Z))
    base_z0, top_z0 = pbb.min.Z, pbb.max.Z

    # (A) floor pockets — every significant intermediate perpendicular-to-axis planar face is a floor.
    foot = pbb.size.X * pbb.size.Y
    for f in aligned0.faces().filter_by(GeomType.PLANE):
        nrm = _normal(f).normalized()
        if abs(abs(nrm.Z) - 1) > ANG:
            continue
        zc = f.position_at(0.5, 0.5).Z
        if not (base_z0 + EPS < zc < top_z0 - EPS) or f.area <= 0.01 * foot:
            continue
        polys = _face_polys(f)
        if polys is None:
            continue
        origin_z, depth = (zc, top_z0 - zc) if nrm.Z > 0 else (base_z0, zc - base_z0)
        feats.append(IR.prism_cut(f"pocket{counts['pockets']}", origin=(0, 0, origin_z),
                                  normal=(0, 0, 1), xdir=(1, 0, 0), depth=round(depth, 4), polys=polys))
        counts["pockets"] += 1

    # (B) residual carve
    if counts["pockets"]:
        part, _ = b3d_emit.emit(IR.part(name, *feats))
    for _p in range(RECOVER_PASSES):
        resid = _robust_cut(part, aligned0)
        if resid is None:
            break                  # OCCT could not form the residual: keep what is carved so far
        comps = [c for c in resid.solids() if c.volume > RECOVER_EPS]
        if not comps:
            break
        added = []
        for c in comps:
            pf = _component_prism(c, f"cut{counts['pockets'] + counts['holes'] + len(added)}")
            if pf is not None:
                added.append(pf)
        if not added:
            break
        trial = feats + added
        try:
            part2, _ = b3d_emit.emit(IR.part(name, *trial))
        except Exception:
            break                                        # a cut didn't build -> keep what we have
        if part2.volume >= part.volume - RECOVER_EPS:
            break                                        # no progress -> stop (rest is chamfers)
        for pf in added:
            counts["holes" if _is_hole_prism(pf) else "pockets"] += 1
        feats, part = trial, part2
    resid = _robust_cut(part, aligned0)
    counts["residual_lumps"] = (len([c for c in resid.solids() if c.volume > RECOVER_EPS])
                                if resid is not None else -1)     # -1: residual not computable
    return IR.part(name, *feats), counts


# --- revolve intelligence: is this solid a body of revolution about SOME axis? -----------------
# Robust test: rotating a body of revolution by any angle about its axis leaves it unchanged. So
# rotate by a non-special angle and check the symmetric-difference volume is ~0 — works for any
# surface types (cone/sphere/torus/bspline), unlike a per-face axis check. Then the MERIDIAN (the
# 2D section in a half-plane through the axis) is the profile the IR revolves.

def _is_revolve_about(solid, a, delta=17.0):
    try:
        rot = solid.rotate(Axis((0, 0, 0), a.normalized().to_tuple()), delta)
        slack = (solid - rot).volume + (rot - solid).volume
    except Exception:
        return False
    return slack < 1e-3 * solid.volume


def _find_revolve_axis(solid):
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for gt in (GeomType.CYLINDER, GeomType.CONE):
        for f in solid.faces().filter_by(gt):
            ax = _cyl_axis(f)                     # axis via the circular edges' centers
            if ax:
                cands.append(ax)
    seen = []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a.normalized())
        if _is_revolve_about(solid, a):
            return a.normalized()
    return None


# ------------------------------------------------------------------ SHELL (open box) recognition
# An open box, cup or enclosure is not a prism along ANY axis: across its opening you cut two
# stray walls, along it the section changes with depth (draft, a floor at one end). Looking at
# it from several aspects is what reveals it: along the opening axis a dense stack of slices is
# mostly RINGS (one region, one large inner loop), solid at the floor end and open at the other.
# That is recovered as what a designer would draw — pad the outer outline, pocket the cavity from
# the open face to the floor — and handed to multi-axis recovery for slots, bosses and holes.
# The largest outer outline and the SMALLEST cavity are used so the result contains the part:
# carving can only remove, and it cannot put back what an over-cut took.

SHELL_SLICES = 24
SHELL_MIN_RING_FRAC = 0.5    # at least half the slices must be rings
SHELL_MIN_CAVITY = 0.3       # inner loop >= 30% of the outer loop's area


def _arc3(p0, pm, p1):
    """Circle through three 2D points -> (center, radius), or None if collinear."""
    ax, ay = p0; bx, by = pm; cx, cy = p1
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def _approx_segments(e, tol, depth=0, t0=0.0, t1=1.0):
    """Segments (start, end, bulge) approximating edge `e` over [t0, t1]: exact for lines and
    circular arcs, otherwise circular arcs through (start, mid, end), split until every sub-arc
    stays within `tol` of the curve at its quarter points. Used for the drafted corner rounds
    of molded parts, whose sections are ellipses; the rebuild check still judges the result."""
    a, b, m = e @ t0, e @ t1, e @ ((t0 + t1) / 2)
    p0, p1, pm = (a.X, a.Y), (b.X, b.Y), (m.X, m.Y)
    chord = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if chord < EPS:
        return []
    ux, uy = (p1[0] - p0[0]) / chord, (p1[1] - p0[1]) / chord
    sag = -uy * (pm[0] - p0[0]) + ux * (pm[1] - p0[1])
    if e.geom_type in (GeomType.LINE, GeomType.CIRCLE) and t0 == 0.0 and t1 == 1.0:
        return [(_r2(a), _r2(b), 0.0 if e.geom_type == GeomType.LINE else _edge_bulge(e))]
    circ = _arc3(p0, pm, p1)
    ok = True
    for t in (t0 + (t1 - t0) / 4, t0 + 3 * (t1 - t0) / 4):
        q = e @ t
        if circ is None:
            dev = abs(-uy * (q.X - p0[0]) + ux * (q.Y - p0[1]))
        else:
            (cx, cy), r = circ
            dev = abs(math.hypot(q.X - cx, q.Y - cy) - r)
        ok = ok and dev <= tol
    if ok or depth >= 5:
        bulge = 0.0 if circ is None or abs(sag) < 1e-9 else round(2.0 * sag / chord, 6)
        return [(_r2(a), _r2(b), bulge)]
    tm = (t0 + t1) / 2
    return _approx_segments(e, tol, depth + 1, t0, tm) + _approx_segments(e, tol, depth + 1, tm, t1)


def _ordered_edges(wire):
    """A wire's edges IN ORDER, each with a flag for whether it is traversed reversed. OCCT's
    BRepTools_WireExplorer knows the order; reconstructing it by matching rounded endpoints both
    failed on some loops and pushed them into a slow polyline fallback."""
    from build123d import Edge
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopAbs import TopAbs_REVERSED
    out = []
    ex = BRepTools_WireExplorer(wire.wrapped)
    while ex.More():
        e = ex.Current()
        out.append((Edge(e), e.Orientation() == TopAbs_REVERSED))
        ex.Next()
    return out


def _loop_points(wire):
    """A cheap ordered point list around a loop: each edge's start and midpoint. Uses EDGE
    parameters; `wire @ t` re-measures the whole wire's arc length on every call, and ~96,000 such
    calls were 180 s of a 183 s recognition of CTC-02."""
    pts = []
    for e, rev in _ordered_edges(wire):
        a, m = (e @ 1.0, e @ 0.5) if rev else (e @ 0.0, e @ 0.5)
        pts += [(a.X, a.Y), (m.X, m.Y)]
    return pts


def _approx_poly(wire, tol=DIM_TOL):
    """A closed wire as an ordered (x, y, bulge) loop, conics approximated by arcs. Edges are walked
    in the wire's own order; a reversed edge contributes its segments reversed, bulges negated."""
    loop = []
    try:
        for e, rev in _ordered_edges(wire):
            segs = _approx_segments(e, tol)
            if rev:
                segs = [(en, st, -bl) for (st, en, bl) in reversed(segs)]
            for st, en, bl in segs:
                loop.append([st[0], st[1], bl])
    except Exception:
        return _polyline(wire)
    return loop if len(loop) >= 2 else _polyline(wire)


def _polyline(wire, max_seg=0.5):
    """Last resort: straight segments sampled per EDGE (never via wire parameters), at most ~400
    points per loop, so a fallback outline cannot make every later Boolean crawl."""
    try:
        edges = _ordered_edges(wire)
        per = max(2, min(64, int(400 / max(1, len(edges)))))
        out = []
        for e, rev in edges:
            ts = [i / per for i in range(per)]
            for t in (ts if not rev else [1 - t for t in ts]):
                p = _r2(e @ t)
                if not out or not _close(tuple(out[-1][:2]), p):
                    out.append([p[0], p[1], 0.0])
        return out if len(out) >= 3 else None
    except Exception:
        return None


def _slice(solid, bb, z):
    return solid.intersect(Plane.XY.offset(z) * Rectangle(bb.size.X + 50, bb.size.Y + 50)).faces()


def _ring(faces):
    """(outer_wire, largest_inner_wire, outer_area, inner_area) if the slice is one region with a
    large inner loop, else None."""
    if len(faces) != 1:
        return None
    f = faces[0]
    inners = list(f.inner_wires())
    if not inners:
        return None
    from build123d import Face
    oa = Face(f.outer_wire()).area
    big = max(inners, key=lambda w: Face(w).area)
    ia = Face(big).area
    return (f.outer_wire(), big, oa, ia) if ia >= SHELL_MIN_CAVITY * oa else None


def _recognize_shell(orig, name, axis):
    """Recognize `orig` as an open shell (box / cup / enclosure) along `axis`, or raise."""
    solid = _align_to_z(orig, axis)
    bb = solid.bounding_box()
    base_z, thick = bb.min.Z, bb.size.Z
    # cheap pre-check: five slices, most of which must already be rings, before the full stack
    probe = [_ring(_slice(solid, bb, base_z + f * thick)) for f in (0.3, 0.45, 0.6, 0.75, 0.9)]
    probe2 = [_ring(_slice(solid, bb, base_z + f * thick)) for f in (0.1,)]
    if sum(r is not None for r in probe) < 2 and probe2[0] is None:
        raise ValueError("not a shell along this axis (pre-check)")
    zs = [base_z + (k + 0.5) / SHELL_SLICES * thick for k in range(SHELL_SLICES)]
    secs = [_slice(solid, bb, z) for z in zs]
    rings = [_ring(f) for f in secs]
    n_ring = sum(r is not None for r in rings)
    if n_ring < SHELL_MIN_RING_FRAC * SHELL_SLICES:
        raise ValueError(f"not a shell along this axis ({n_ring}/{SHELL_SLICES} ring slices)")
    # open end = the end whose outermost slices are rings; the floor end is where they stop
    top_open, bot_open = rings[-1] is not None, rings[0] is not None
    if top_open == bot_open:
        raise ValueError("cavity is open at both ends or neither — a tube, not an open shell")
    ring_idx = [k for k, r in enumerate(rings) if r is not None]
    # floor: just beyond the ring slice FARTHEST from the opening. Slices between it and the
    # opening may still be non-rings — a slot through a side wall splits the section into several
    # regions — so the floor is not "the first non-ring from the open end".
    if top_open:
        k_far = ring_idx[0]
        if k_far == 0:
            raise ValueError("no floor: rings reach both ends")
        lo, hi = zs[k_far - 1], zs[k_far]
    else:
        k_far = ring_idx[-1]
        if k_far == SHELL_SLICES - 1:
            raise ValueError("no floor: rings reach both ends")
        lo, hi = zs[k_far], zs[k_far + 1]
    for _ in range(12):
        mid = (lo + hi) / 2
        is_ring = _ring(_slice(solid, bb, mid)) is not None
        if top_open:        # rings above the floor: a ring at mid means the floor is below it
            lo, hi = (lo, mid) if is_ring else (mid, hi)
        else:               # rings below the floor
            lo, hi = (mid, hi) if is_ring else (lo, mid)
    floor_z = hi if top_open else lo
    depth = (bb.max.Z - floor_z) if top_open else (floor_z - base_z)

    # OUTLINE LAYERS. The outer outline as a function of depth is not one thing: FTC-07's is a
    # 1-degree draft plus a rim lip, FTC-08's is a flange 10 mm deep and then straight walls. Split
    # it wherever the outline JUMPS between neighbouring slices; each layer becomes its own pad,
    # stacked on the one below, and is drafted only if its own slices lie on a straight line.
    # (One straight-line fit across FTC-08's flange step read as an 8-degree "draft".)
    from build123d import Face, offset, Kind

    def _outer(k):
        return rings[k][0] if rings[k] else max(secs[k], key=lambda f: f.area).outer_wire()

    def _hw(w):
        b = w.bounding_box()
        return (b.size.X + b.size.Y) / 4                  # mean half-width of a loop

    def _fit(ks, vals):
        """(slope, max |residual|) of vals against z over slices ks."""
        zz = [zs[k] for k in ks]
        n = len(zz)
        mz, mv = sum(zz) / n, sum(vals) / n
        sxx = sum((z - mz) ** 2 for z in zz)
        slope = sum((z - mz) * (v - mv) for z, v in zip(zz, vals)) / sxx if sxx else 0.0
        return slope, max(abs(v - (mv + slope * (z - mz))) for z, v in zip(zz, vals))

    # Slices that are NOT rings but sit between ring slices are interruptions -- a slot through a
    # side wall splits the section into pieces -- and say nothing about the outline; FTC-07's
    # largest mid-depth piece is an 83 mm fragment. Only rings, and the solid floor-end slices
    # beyond them, define the outline.
    valid = [k for k in range(SHELL_SLICES)
             if rings[k] is not None or not (ring_idx[0] < k < ring_idx[-1])]
    hws = {k: _hw(_outer(k)) for k in valid}

    # Piecewise-linear segmentation of half-width against depth (Douglas-Peucker): split a run at
    # its worst deviation from the straight line through its ends until every run is straight
    # within 0.1 mm. A drafted wall stays ONE layer; a flange or a rim lip splits off even when
    # its step is small (FTC-07's lip grows 1.2-1.5 mm per slice, barely more than its draft).
    def _dp(ks):
        if len(ks) <= 2:
            return [ks]
        z0, z1, h0, h1 = zs[ks[0]], zs[ks[-1]], hws[ks[0]], hws[ks[-1]]
        dev = [abs(hws[k] - (h0 + (h1 - h0) * (zs[k] - z0) / (z1 - z0))) for k in ks]
        m = max(range(len(ks)), key=lambda i: dev[i])
        if dev[m] <= 0.1:
            return [ks]
        return _dp(ks[:m + 1]) + _dp(ks[m + 1:])      # m is interior: both halves non-empty
    layers = _dp(valid)
    # merge runs that sit on one straight line (DP can over-split at the breakpoint itself)
    fused = [layers[0]]
    for L in layers[1:]:
        cand = fused[-1] + L
        if len(cand) >= 3:
            sl, rs = _fit(cand, [hws[k] for k in cand])
            if rs <= 0.1:
                fused[-1] = cand
                continue
        fused.append(L)
    layers = fused
    # a one-slice layer is a transition (a chamfer between steps): give it to the LARGER
    # neighbour, so the stacked pads still contain the part and carving can take the rest
    merged = []
    for li, L in enumerate(layers):
        if len(L) == 1 and len(layers) > 1:
            nb = [x for x in (li - 1, li + 1) if 0 <= x < len(layers)]
            big = max(nb, key=lambda x: max(hws[k] for k in layers[x]))
            if big < li and merged:
                merged[-1] = sorted(merged[-1] + L)
                continue
            layers[big] = sorted(layers[big] + L)
            continue
        merged.append(L)
    layers = merged

    # layer boundaries: bisect where the outline crosses halfway between neighbouring layers
    def _boundary(ka, kb, ha, hb):
        lo, hi = zs[ka], zs[kb]
        mid_h = (ha + hb) / 2
        for _ in range(10):
            m = (lo + hi) / 2
            fs = _slice(solid, bb, m)
            if not fs:
                break
            hm = _hw(max(fs, key=lambda f: f.area).outer_wire())
            if (hm - mid_h) * (ha - mid_h) > 0:
                lo = m
            else:
                hi = m
        return (lo + hi) / 2
    bounds = [base_z]
    for a, b in zip(layers, layers[1:]):
        bounds.append(_boundary(a[-1], b[0], hws[a[-1]], hws[b[0]]))
    bounds.append(bb.max.Z)

    feats, notes, drafts = [], [], []
    for li, L in enumerate(layers):
        zb, ze = bounds[li], bounds[li + 1]
        slope, resid = _fit(L, [hws[k] for k in L]) if len(L) >= 4 else (0.0, 0.0)
        drafted = len(L) >= 4 and resid < 0.05 and abs(slope) > math.tan(math.radians(0.05))
        if drafted:
            # carry a clean slice's loop down to this layer's start plane along the fitted draft
            k_ref = L[len(L) // 2]
            sh = offset(Face(_outer(k_ref)), amount=-(zs[k_ref] - zb) * slope, kind=Kind.ARC)
            wire = (sh.faces()[0] if hasattr(sh, "faces") else sh).outer_wire()
            taper = round(-math.degrees(math.atan(slope)), 4)
            drafts.append(math.degrees(math.atan(slope)))
        else:
            wire = _outer(max(L, key=lambda k: hws[k]))    # the layer's largest: contains it
            taper = 0.0
        poly = _approx_poly(wire)
        if poly is None:
            raise ValueError(f"shell layer {li} outline did not close into one loop")
        sk, pd = ("outline", "body") if li == 0 else (f"outline{li}", f"body{li}")
        feats.append(IR.sketch(sk, "XY", polys=[poly]) if li == 0 else
                     IR.sketch(sk, polys=[poly], on={"face_of": "body", "side": "top"}))
        feats.append(IR.pad(pd, sk, length=round(ze - zb, 4), taper=taper))
        b = wire.bounding_box()
        notes.append(f"{ze - zb:.1f} mm @ {b.size.X:.1f}x{b.size.Y:.1f}"
                     + (f" drafted {math.degrees(math.atan(slope)):.2f} deg" if drafted else ""))

    # CAVITY: one pocket from the open face. Drafted -> a clean slice's loop carried to the open
    # face along the fitted draft; straight -> the TYPICAL wall-slice cavity (the median of the
    # middle ring slices), not the smallest, which a floor fillet shrinks for every slice above it.
    lo_i = int(0.2 * len(ring_idx))
    mid = ring_idx[lo_i:max(int(0.8 * len(ring_idx)), lo_i + 2)]
    cav_slope, cav_res = _fit(mid, [_hw(rings[k][1]) for k in mid])
    cav_drafted = len(mid) >= 4 and cav_res < 0.05 and abs(cav_slope) > math.tan(math.radians(0.05))
    if cav_drafted:
        k_ref = mid[-1] if top_open else mid[0]
        dz = (bb.max.Z - zs[k_ref]) if top_open else (zs[k_ref] - base_z)
        grow = dz * cav_slope if top_open else -dz * cav_slope    # toward the open face
        sh = offset(Face(rings[k_ref][1]), amount=grow, kind=Kind.ARC)
        cav_w = (sh.faces()[0] if hasattr(sh, "faces") else sh).outer_wire()
    else:
        med = sorted(mid, key=lambda k: _hw(rings[k][1]))[len(mid) // 2]
        cav_w = rings[med][1]
    hole = _approx_poly(cav_w)
    if hole is None:
        raise ValueError("shell cavity did not close into one loop")
    draft_cav = math.degrees(math.atan(cav_slope)) if cav_drafted else 0.0
    pocket_taper = round(draft_cav if top_open else -draft_cav, 4)
    warnings = [f"open shell along {tuple(round(c, 3) for c in axis.to_tuple())}: "
                f"{n_ring}/{SHELL_SLICES} ring slices, cavity {depth:.2f} deep from the "
                f"{'top' if top_open else 'bottom'} face; outline layers: " + ", ".join(notes)
                + (f"; draft {(drafts[0] if drafts else 0.0):.2f} deg outside, {draft_cav:.2f} deg inside"
                   if drafts or cav_drafted else "")]
    feats += [IR.sketch("cavity_sk", polys=[hole], on={"face_of": "body", "side": "top" if top_open else "bottom"}),
              IR.pocket("cavity", "cavity_sk", through=False, length=round(depth, 4), taper=pocket_taper)]

    # FLOOR CUTOUTS. The floor-end slice shows every opening through the floor as an inner loop
    # (FTC-08's pan floor has 21). Cut each one: through-all when it lies inside the cavity outline
    # (below it is only the cavity, so nothing else is hit), otherwise blind, one floor deep.
    floor_th = (bb.max.Z - floor_z) if top_open is False else (floor_z - base_z)
    fz = (floor_z + bb.max.Z) / 2 if not top_open else (base_z + floor_z) / 2
    ffs = _slice(solid, bb, fz)
    n_floor = 0
    if ffs and floor_th > EPS:
        cb = cav_w.bounding_box()
        for w in max(ffs, key=lambda f: f.area).inner_wires():
            poly = _approx_poly(w)
            if poly is None:
                continue
            wb = w.bounding_box()
            inside = (wb.min.X >= cb.min.X - EPS and wb.max.X <= cb.max.X + EPS and
                      wb.min.Y >= cb.min.Y - EPS and wb.max.Y <= cb.max.Y + EPS)
            sk = f"floor_sk{n_floor}"
            if inside:
                feats += [IR.sketch(sk, "XY", polys=[poly]),
                          IR.pocket(f"floor{n_floor}", sk, through=True)]
            else:
                feats += [IR.sketch(sk, polys=[poly], on={"face_of": "body", "side": "bottom" if top_open else "top"}),
                          IR.pocket(f"floor{n_floor}", sk, through=False, length=round(floor_th, 4))]
            n_floor += 1
    if n_floor:
        warnings[0] += f"; {n_floor} floor cutout(s)"
    return IR.part(name, *feats), {"method": "extrude", "axis": axis, "warnings": warnings,
                                   "shell": True, "through_holes": 0, "blind_holes": 0}



# ------------------------------------------------------------------ LAYERED 2.5D recognition
# The general form of every case above. Slice the part densely along an axis; each slice is a set
# of REGIONS, each an outer loop with holes. Split the depth into LAYERS wherever the slices stop
# agreeing (the number of regions or holes changes, or a size stops varying linearly); build each
# layer as pads for its outer loops and pockets for its holes over the layer's depth, drafted where
# the slices fit a straight line. A plain extrude is one layer; a box, pan or housing is rings over
# a floor layer; floor cutouts are holes in the floor layer; a slot is a layer where a wall splits;
# a lug on top is a layer of small regions. Cross-axis features are left to carving.

LAYER_SLICES = 32


def _section_regions(solid, z):
    """Regions of the plane section at height z, as [(outer_wire, [hole_wires], outer_area)].

    Built from BRepAlgoAPI_Section's CURVES, not by intersecting the solid with a face: that
    returned "one region, no holes" for CTC-02's ring sections (and segfaulted on the same part).
    Loops are nested by containment: even depth = a region's outer loop, odd depth = its hole."""
    from build123d import Edge, Face, Wire
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_IN
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    sec = BRepAlgoAPI_Section(solid.wrapped, gp_Pln(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)), True)
    edges = []
    ex = TopExp_Explorer(sec.Shape(), TopAbs_EDGE)
    while ex.More():
        edges.append(Edge(TopoDS.Edge_s(ex.Current())))
        ex.Next()
    if not edges:
        return []
    loops = []
    for w in Wire.combine(edges):
        if not w.is_closed:
            continue
        try:
            f = Face(w)
            loops.append((w, f, abs(f.area)))
        except Exception:
            continue
    loops.sort(key=lambda t: -t[2])
    # nesting by a plain 2D point-in-polygon test on the sampled loops (OCCT's face classifier on a
    # point lying on the inner loop misreported CTC-02's cavity as a second solid region)
    polys2d = [_loop_points(w) for w, _, _ in loops]

    def _pip(x, y, poly):
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    parent, depth = {}, {}
    for i, (w, f, a) in enumerate(loops):
        px, py = polys2d[i][0]
        best = None
        for j in range(i):                                  # smallest larger loop containing it
            if loops[j][2] > a and _pip(px, py, polys2d[j]) and (best is None or loops[j][2] < loops[best][2]):
                best = j
        parent[i] = best
        depth[i] = 0 if best is None else depth[best] + 1
    regions = []
    for i, (w, f, a) in enumerate(loops):
        if depth[i] % 2 == 0:
            holes = [loops[k][0] for k in range(len(loops)) if parent.get(k) == i]
            regions.append((w, holes, a))
    return regions


def _loop_key(w):
    b = w.bounding_box()
    return ((b.min.X + b.max.X) / 2, (b.min.Y + b.max.Y) / 2, (b.size.X + b.size.Y) / 4)


def _signature(regs, min_hole=0.0):
    """Topology of a slice: region count and each region's hole count (largest first)."""
    return tuple(sorted(((len(h) for _, h, _ in regs)), reverse=True)) + (len(regs),)


def _recognize_layers(orig, name, axis):
    """Recognize `orig` as a stack of 2.5D layers along `axis`. Raises if it cannot."""
    from build123d import Face, offset, Kind
    solid = _align_to_z(orig, axis)
    bb = solid.bounding_box()
    base_z, thick = bb.min.Z, bb.size.Z
    # LAYER BOUNDARIES COME FROM THE PART, not from even sampling: the section of a 2.5D part can
    # only change topology at a face perpendicular to the axis -- a step, a floor, a flange top,
    # the edge of a slot. Even slices put ~2 of 32 inside FTC-08's 3.4 mm floor and it was folded
    # into the walls. So the event heights are the perpendicular planar faces' heights, and each
    # interval between consecutive events is a candidate layer.
    ev = {round(base_z, 4), round(bb.max.Z, 4)}
    for f in solid.faces().filter_by(GeomType.PLANE):
        if abs(abs(_normal(f).normalized().Z) - 1) < ANG:
            ev.add(round(f.center().Z, 4))
    ev = sorted(ev)
    merged_ev = [ev[0]]
    for z in ev[1:]:
        if z - merged_ev[-1] > 0.05:
            merged_ev.append(z)
    if merged_ev[-1] < bb.max.Z - 0.05:
        merged_ev.append(bb.max.Z)
    intervals = [(a, b) for a, b in zip(merged_ev, merged_ev[1:])]
    if len(intervals) > 60:
        raise ValueError(f"{len(intervals)} step heights: not a 2.5D part along this axis")

    # ...but flat faces are not the only place the section changes: topology also changes at the
    # end of a CURVED face -- a boss merging into a wall, a rounded rib -- with no flat face there.
    # CTC-02 has one interval that is a ring, then two C-shaped walls, then solid, with no flat face
    # between, and one representative slice made it a 245 mm solid slab. So sample each interval
    # every ~8 mm, and split it wherever the signature changes, bisecting to the height.
    cache = {}

    def _sig_at(z):
        if z not in cache:
            cache[z] = _signature(_section_regions(solid, z))
        return cache[z]
    split = []
    # a budget of ~240 sections per axis: 8 mm spacing on small parts, coarser on big ones (CTC-02's
    # side views took 190 s each at a fixed 8 mm)
    step = max(8.0, thick / 240.0)
    for a, b in intervals:
        n = max(2, min(40, int((b - a) / step)))
        pts = [a + (i + 0.5) * (b - a) / n for i in range(n)]
        cuts = [a]
        for z0, z1 in zip(pts, pts[1:]):
            if _sig_at(z0) != _sig_at(z1):
                lo, hi = z0, z1
                for _ in range(10):
                    m = (lo + hi) / 2
                    lo, hi = (m, hi) if _sig_at(m) == _sig_at(z0) else (lo, m)
                cuts.append((lo + hi) / 2)
        cuts.append(b)
        split += [(u, v) for u, v in zip(cuts, cuts[1:]) if v - u > 0.05]
    intervals = split
    if len(intervals) > 80:
        raise ValueError(f"{len(intervals)} section changes: not a 2.5D part along this axis")
    # a few slices per interval: the middle, plus quarter points on long intervals so draft and
    # curvature (chamfers, fillets) can be told apart from a constant section
    zs, lay_idx = [], []
    for a, b in intervals:
        fr = (0.5,) if b - a < 4.0 else (0.15, 0.38, 0.62, 0.85)
        ks = []
        for f in fr:
            zs.append(a + f * (b - a))
            ks.append(len(zs) - 1)
        lay_idx.append(ks)
    slices = [_section_regions(solid, z) for z in zs]
    if not any(slices):
        raise ValueError("empty sections")
    # An interval whose slices all come back EMPTY has material in it (the part is one solid): the
    # section failed to close at those heights. Dropping it silently put every layer above it
    # 17.27 mm low on CTC-02, because pads stack on "the current top face". So retry at other
    # heights, and if the section still will not close, extend the layer below over the gap --
    # the stack keeps its true height and the verification gate judges the approximation.
    for (a, b), ks in zip(intervals, lay_idx):
        if any(slices[k] for k in ks):
            continue
        for f in (0.1, 0.3, 0.7, 0.9, 0.02, 0.98):
            reg = _section_regions(solid, a + f * (b - a))
            if reg:
                zs.append(a + f * (b - a))
                slices.append(reg)
                ks[:] = [len(zs) - 1]
                break
    layers, bounds_by_layer, gaps = [], [], []
    for (a, b), ks in zip(intervals, lay_idx):
        if any(slices[k] for k in ks):
            layers.append(ks)
            bounds_by_layer.append((a, b))
        elif bounds_by_layer:
            bounds_by_layer[-1] = (bounds_by_layer[-1][0], b)
            gaps.append((a, b))
        else:
            gaps.append((a, b))             # below the first layer: it starts higher

    # Merge neighbouring intervals whose sections are the SAME: a step height elsewhere on the
    # part (an internal rib's top) splits a wall into intervals that a designer would draw as one
    # pad. Same topology and every loop within 0.05 mm (centre and size) -> one layer.
    def _same(ka, kb):
        ra, rb = slices[ka], slices[kb]
        if _signature(ra) != _signature(rb):
            return False
        la = sorted(_loop_key(w) for r in ra for w in [r[0]] + list(r[1]))
        lb = sorted(_loop_key(w) for r in rb for w in [r[0]] + list(r[1]))
        return len(la) == len(lb) and all(max(abs(x - y) for x, y in zip(p, q)) < 0.05 for p, q in zip(la, lb))
    if not layers:
        raise ValueError("empty sections")
    bounds_by_layer[0] = (base_z, bounds_by_layer[0][1])   # pads are built up from the part's base
    ml, mb = [layers[0]], [bounds_by_layer[0]]
    for L, bd in zip(layers[1:], bounds_by_layer[1:]):
        if _same(ml[-1][-1], L[0]) and _same(ml[-1][0], L[-1]):
            ml[-1] = ml[-1] + L
            mb[-1] = (mb[-1][0], bd[1])
        else:
            ml.append(L)
            mb.append(bd)
    layers, bounds_by_layer = ml, mb

    # 5a. PADS: each layer's outer loops, stacked from the bottom
    def _fit(ks, hs):
        zz = [zs[k] for k in ks]
        n = len(zz)
        mz, mh = sum(zz) / n, sum(hs) / n
        sxx = sum((z - mz) ** 2 for z in zz)
        sl = sum((z - mz) * (h - mh) for z, h in zip(zz, hs)) / sxx if sxx else 0.0
        return sl, max(abs(h - (mh + sl * (z - mz))) for z, h in zip(zz, hs))

    def _nearest(cands, cx, cy, hw=None, tol=None):
        c = [w for w in cands if hw is None or abs(_loop_key(w)[2] - hw) <= tol]
        return min(c, key=lambda w: math.hypot(_loop_key(w)[0] - cx, _loop_key(w)[1] - cy)) if c else None

    def _taper_ok(wire, depth, taper):
        """Test-build a drafted extrusion of this loop: OCCT's tapered extrude (a loft underneath)
        fails on some outlines, and a spec that cannot be emitted cannot be verified."""
        try:
            from build123d import extrude
            return extrude(Face(wire), amount=depth, taper=taper).volume > 0
        except Exception:
            return False

    def _emit_ok(poly, z0, nz, depth, taper):
        try:
            from build123d import Plane, extrude
            from b3d_emit import _polys_region
            pl = Plane(origin=(0, 0, z0), x_dir=(1, 0, 0), z_dir=(0, 0, nz))
            reg = _polys_region([poly], pl)
            return reg is not None and extrude(reg, amount=depth, dir=(0, 0, nz), taper=taper).volume > 0
        except Exception:
            return False

    def _median(ws):
        ws = sorted((w for w in ws if w is not None), key=lambda w: _loop_key(w)[2])
        return ws[len(ws) // 2] if ws else None

    def _carry(wire, amount):
        sh = offset(Face(wire), amount=amount, kind=Kind.ARC)
        return (sh.faces()[0] if hasattr(sh, "faces") else sh).outer_wire()

    # ONE sketch and ONE pad per layer, holding all of its region outlines. Pads are stacked on
    # "the current top face", so padding a layer's regions one at a time put the second region ON
    # TOP of the first (FTC-07's four feet sent its floor slab 2.5 mm up and out of the part).
    feats, notes, skipped = [], [], []
    # Curves that are not lines or circular arcs (ellipses, conic sections, splines) can only be
    # APPROXIMATED by the sketch's arcs. That may still verify within tolerance, but the tree then
    # holds arcs where the designer had, say, an ellipse -- so it is disclosed, never silent.
    n_approx = sum(1 for sl_ in slices for r in sl_ for w in [r[0]] + list(r[1]) for e in w.edges()
                   if e.geom_type not in (GeomType.LINE, GeomType.CIRCLE))
    for li, L in enumerate(layers):
        zb, ze = bounds_by_layer[li]
        Lz = ze - zb
        rep = L[len(L) // 2]
        if Lz < EPS:
            continue
        if not slices[rep]:
            skipped.append((li, zb, ze, "empty section"))
            continue
        outlines, slopes = [], []
        for ri, (ow, holes, oa) in enumerate(slices[rep]):
            cx, cy, hw0 = _loop_key(ow)
            track = [_nearest([r[0] for r in slices[k]], cx, cy) for k in L]
            sl = None
            if len(L) >= 4 and all(track):
                s_, res = _fit(L, [_loop_key(w)[2] for w in track])
                if res < 0.05:
                    sl = s_
            outlines.append((ow, track))
            slopes.append(sl)
        # one taper for the whole layer: only when every region fits the same draft
        drafted = (all(v is not None for v in slopes) and max(slopes) - min(slopes) < 1e-3
                   and abs(slopes[0]) > math.tan(math.radians(0.05)))
        polys = []
        taper = 0.0
        if drafted:
            sl = sum(slopes) / len(slopes)
            taper = round(-math.degrees(math.atan(sl)), 4)
            try:
                polys = [_approx_poly(_carry(ow, -(zs[rep] - zb) * sl)) for ow, _ in outlines]
                if any(p is None for p in polys) or not all(_emit_ok(p, 0.0, 1.0, Lz, taper) for p in polys):
                    polys = []
            except Exception:
                polys = []
        if not polys:                           # straight: each region's MEDIAN outline
            taper = 0.0
            polys = [_approx_poly(_median(tr) or ow) for ow, tr in outlines]
        polys = [p for p in polys if p is not None]
        if not polys:
            skipped.append((li, zb, ze, "outline not approximable"))
            continue
        sk = f"L{li}_sk"
        first = not feats
        feats.append(IR.sketch(sk, "XY", polys=polys) if first else
                     IR.sketch(sk, polys=polys, on={"face_of": "body", "side": "top"}))
        feats.append(IR.pad("body" if first else f"L{li}", sk, length=round(Lz, 4), taper=taper))
        notes.append(f"{Lz:.1f} mm: {len(slices[rep])} region(s), {sum(len(r[1]) for r in slices[rep])} hole(s)")

    # 5b. HOLES as vertical RUNS: a hole that continues through several layers (a bolt hole through
    # a flange and the layer above it, a cavity through a wall and its lip) is ONE cut over its
    # whole run -- as a designer draws it -- placed at its own height with prism_cut. CTC-02 was
    # 632 features when every layer re-pocketed its holes. A change of size is a new run, so a
    # counterbore stays a counterbore.
    # Two holes in adjacent layers are ONE continuing hole only if they MEET at the boundary: the
    # last slice of the lower layer and the first of the upper agree to 0.3 mm in size and 0.5 mm
    # in position. (A 10%-of-size tolerance merged FTC-08's smaller flange opening with its wall
    # cavity, and "smallest along the run" then under-cut every wall.)
    runs, open_runs = [], []
    for li, L in enumerate(layers):
        rep = L[len(L) // 2]
        nxt = []
        for h in [h for r in slices[rep] for h in r[1]]:
            cx, cy, hw = _loop_key(h)
            first = _nearest([g for rr in slices[L[0]] for g in rr[1]], cx, cy)
            fk = _loop_key(first) if first is not None else (cx, cy, hw)
            m = None
            for r in open_runs:
                if r["layers"][-1] != li - 1 or r["end"] is None:
                    continue
                ek = r["end"]
                if abs(ek[0] - fk[0]) < 0.5 and abs(ek[1] - fk[1]) < 0.5 and abs(ek[2] - fk[2]) < 0.3:
                    m = r
                    break
            if m is None:
                m = {"layers": [], "key": (cx, cy, hw)}
                runs.append(m)
            m["layers"].append(li)
            m["key"] = (cx, cy, hw)
            last = _nearest([g for rr in slices[L[-1]] for g in rr[1]], cx, cy)
            m["end"] = _loop_key(last) if last is not None else None
            nxt.append(m)
        open_runs = nxt

    n_cuts = 0
    for r in runs:
        zb, ze = bounds_by_layer[r["layers"][0]][0], bounds_by_layer[r["layers"][-1]][1]
        ks = [k for li in r["layers"] for k in layers[li]]
        cx, cy, hw = r["key"]
        track = [_nearest([h for g in slices[k] for h in g[1]], cx, cy, hw, max(2.0, 0.05 * hw)) for k in ks]
        drafted = False
        if len(ks) >= 4 and all(track):
            sl, res = _fit(ks, [_loop_key(w)[2] for w in track])
            drafted = res < 0.05 and abs(sl) > math.tan(math.radians(0.05))
        if drafted:
            # cut from the end where the hole is LARGER, narrowing into the part
            kref = ks[len(ks) // 2]
            ref = track[len(ks) // 2]
            try:
                # always cut UPWARD from the run's bottom plane: carry the loop down to it and let
                # the taper follow the fitted slope (negative = widening as it rises). Cutting down
                # from the top needs a flipped plane, which mirrors the polygon and its arcs, and
                # OCCT's tapered extrude failed on exactly that for FTC-07's cavity.
                wire, z0, nz = _carry(ref, -(zs[kref] - zb) * sl), zb, 1.0
                taper = round(-math.degrees(math.atan(sl)), 4)
                drafted = True
            except Exception:
                drafted = False
        if not drafted:                         # the MEDIAN along the run: a floor fillet shrinks
            wire = _median(track)               # the smallest slice, under-cutting every wall
            if wire is None:
                continue
            z0, nz, taper = zb, 1.0, 0.0
        poly = _approx_poly(wire)
        if poly is None:
            continue
        if nz < 0:                              # local frame of a -Z plane: v = -y, arcs mirror
            poly = [[p[0], -p[1], -p[2] if len(p) > 2 else 0.0] for p in poly]
        if taper and not _emit_ok(poly, z0 - base_z, nz, ze - zb, taper):
            # test-build EXACTLY what will be emitted (the approximated polygon, on its plane);
            # if the kernel cannot draft it, cut the median loop straight instead
            poly = _approx_poly(_median(track))
            if poly is None:
                continue
            z0, nz, taper = zb, 1.0, 0.0
        # pads are built from z = 0 while slice heights are in the aligned frame: shift by base_z
        feats.append(IR.prism_cut(f"hole{n_cuts}", origin=(0.0, 0.0, z0 - base_z), normal=(0.0, 0.0, nz),
                                  xdir=(1.0, 0.0, 0.0), depth=round(ze - zb, 4), polys=[poly], taper=taper))
        n_cuts += 1
    if not feats:
        raise ValueError("no layers")
    warnings = [f"layered along {tuple(round(c, 3) for c in axis.to_tuple())}: {len(layers)} layer(s), "
                f"{n_cuts} hole run(s) -- " + "; ".join(notes)]
    if gaps:
        warnings.append("section would not close over " + ", ".join(
            f"{a - base_z:.2f}..{b - base_z:.2f} mm" for a, b in gaps) + " -- the layer below was extended over it")
    if skipped:
        # later layers stack on "the current top face", so a dropped layer shortens the whole stack
        warnings.append("layer(s) dropped, so the stack above them sits low: " + "; ".join(
            f"L{li} {zb - base_z:.2f}..{ze - base_z:.2f} mm ({why})" for li, zb, ze, why in skipped))
    if n_approx:
        warnings.append(f"{n_approx} section curve(s) that are not lines or circular arcs (ellipse, "
                        f"conic, spline) approximated by arcs within {DIM_TOL} mm -- the tree holds "
                        "arcs where the design had a freeform curve")
    # method "layered": registered like an extrude (built from z = 0), but NOT handed to the
    # carving pass -- its holes are already cut, and carving has no per-step over-cut guard
    return IR.part(name, *feats), {"method": "layered", "axis": axis, "warnings": warnings,
                                   "layers": len(layers), "through_holes": 0, "blind_holes": 0}


def _recognize_revolve(orig, name):
    """Recognize a body of revolution: find the axis, take the meridian (half-plane section), and
    emit an XZ profile + revolve(360). Returns (spec, extras) or raises."""
    axis = _find_revolve_axis(orig)
    if axis is None:
        raise ValueError("not a body of revolution")
    solid = _align_to_z(orig, axis)
    bb = solid.bounding_box()
    R, H = bb.max.X + max(1.0, 0.1 * bb.size.X), bb.size.Z + 2
    zc = (bb.min.Z + bb.max.Z) / 2                          # center the section on the solid's z-range
    half = Plane.XZ * Pos(R / 2, zc, 0) * Rectangle(R, H)   # a face on XZ spanning x in [0, R]
    faces = solid.intersect(half).faces()
    if len(faces) != 1:
        raise ValueError(f"revolve meridian has {len(faces)} regions (axial hole etc. — unsupported)")
    merid = faces[0].rotate(Axis((0, 0, 0), (1, 0, 0)), -90)   # XZ (r,axial) -> XY (x=r, y=axial)
    warnings = []
    prof = _classify_wire(merid.outer_wire(), warnings)
    if prof is None or prof[0] != "poly":
        raise ValueError("revolve meridian is not a line/arc profile")
    spec = IR.part(name,
                   IR.sketch("section", "XZ", polys=[prof[1]]),
                   IR.revolve("body", "section", angle=360.0))
    return spec, {"warnings": warnings, "method": "revolve", "axis": axis,
                  "through_holes": 0, "blind_holes": 0}


def _r2(v):
    return (round(v.X, 4), round(v.Y, 4))


def _normal(f):
    try:
        return f.normal_at(f.position_at(0.5, 0.5))
    except Exception:
        return f.normal_at()


def _classify_wire(wire, warnings):
    """A closed outer wire -> ('circle', cx, cy, r) | ('poly', [(x,y[,bulge]),...]) | None.
    Handles lines AND circular arcs (as DXF bulges); rejects splines / ellipses."""
    edges = wire.edges()
    circ = edges.filter_by(GeomType.CIRCLE)
    if len(edges) == 1 and len(circ) == 1:
        c = circ[0]
        return ("circle", round(c.arc_center.X, 4), round(c.arc_center.Y, 4), round(c.radius, 4))
    if all(e.geom_type in (GeomType.LINE, GeomType.CIRCLE) for e in edges):
        return ("poly", _ordered_poly(edges))
    warnings.append("outline has splines/ellipses — unsupported (lines + circular arcs only)")
    return None


def _edge_bulge(e):
    """DXF bulge tan(theta/4) of a circular-arc edge = 2*(signed sagitta)/chord; 0 for a line."""
    if e.geom_type != GeomType.CIRCLE:
        return 0.0
    a, b, m = e @ 0.0, e @ 1.0, e @ 0.5
    chord = math.hypot(b.X - a.X, b.Y - a.Y)
    if chord < EPS:
        return 0.0
    ux, uy = (b.X - a.X) / chord, (b.Y - a.Y) / chord      # chord dir; left normal = (-uy, ux)
    sag = -uy * (m.X - a.X) + ux * (m.Y - a.Y)             # signed dist chord->arc-mid
    return round(2.0 * sag / chord, 6)


def _ordered_poly(edges):
    """Chain line/arc edges by shared endpoints into an ordered loop of (x, y, bulge), where the
    bulge on each vertex describes the segment LEAVING it (0 = straight)."""
    segs = [(_r2(e @ 0.0), _r2(e @ 1.0), _edge_bulge(e)) for e in edges]
    order = [(segs[0][0], segs[0][2])]            # (vertex, outgoing bulge)
    used, tail = {0}, segs[0][1]
    while len(used) < len(segs):
        for i, (s, e, bl) in enumerate(segs):
            if i in used:
                continue
            if _close(s, tail):
                order.append((s, bl)); used.add(i); tail = e; break
            if _close(e, tail):
                order.append((e, -bl)); used.add(i); tail = s; break   # reversed arc -> flip bulge
        else:
            break
    return [[p[0], p[1], b] for (p, b) in order]


def _close(a, b):
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


def _wire_xy(wire):
    """A wire's (x, y) bounding-box center, rounded — a stable key for matching the same vertical
    hole across cross-sections at different heights."""
    wb = wire.bounding_box()
    return (round((wb.min.X + wb.max.X) / 2, 1), round((wb.min.Y + wb.max.Y) / 2, 1))


def _through_centroids(solid, base_z, top_z, bb):
    """(x, y) keys of inner-wire holes that appear near BOTH end faces — i.e. bores that actually go
    all the way through. A blind pocket appears near only one face, so it's excluded (and recovered as
    a pocket instead of being over-cut as a through-hole)."""
    def inner_keys(z):
        cut = Plane.XY.offset(z) * Rectangle(bb.size.X + 50, bb.size.Y + 50)
        keys = set()
        for ff in solid.intersect(cut).faces():
            for w in ff.inner_wires():
                keys.add(_wire_xy(w))
        return keys
    d = min(1.0, 0.02 * (top_z - base_z))
    return inner_keys(base_z + d) & inner_keys(top_z - d)


def input_frame(orig, report):
    """Map the recovered tree's frame back onto the INPUT's, exactly.

    Recognition rotates the chosen axis onto +Z (_align_to_z) and, for an extrude, builds the base
    pad from z = 0 where the aligned part starts at its own base height. Undoing exactly that is an
    exact registration — no orientation search, no corner alignment — so an IoU measured through
    it is a MEASUREMENT of the recovery, not a lower bound on it. Returns (place, point, direction):
    place(shape) moves a build123d shape, point/direction map (x, y, z) tuples."""
    solid = orig.solids()[0] if hasattr(orig, "solids") and orig.solids() else orig
    a = Vector(*report["extrude_axis"]).normalized()
    z = Vector(0, 0, 1)
    rotated = abs(a.dot(z)) <= 1 - ANG                  # mirrors _align_to_z's own test
    k = a.cross(z).normalized() if rotated else None
    ang = math.degrees(math.acos(max(-1.0, min(1.0, a.dot(z))))) if rotated else 0.0
    dz = (_align_to_z(solid, a).bounding_box().min.Z
          if report.get("method") in ("extrude", "layered") else 0.0)

    def place(shape):
        s = Pos(0, 0, dz) * shape
        return s.rotate(Axis((0, 0, 0), k.to_tuple()), -ang) if rotated else s

    def _rot(v):                                         # Rodrigues, by -ang about k
        if not rotated:
            return v
        t = math.radians(-ang)
        c, sn = math.cos(t), math.sin(t)
        kv = (k.Y * v[2] - k.Z * v[1], k.Z * v[0] - k.X * v[2], k.X * v[1] - k.Y * v[0])
        kd = k.X * v[0] + k.Y * v[1] + k.Z * v[2]
        kk = (k.X, k.Y, k.Z)
        return tuple(v[i] * c + kv[i] * sn + kk[i] * kd * (1 - c) for i in range(3))

    def point(p):
        return _rot((p[0], p[1], p[2] + dz))

    def direction(v):
        return _rot(tuple(v))

    return place, point, direction


def _report_for(spec, extras, name):
    ax = extras.get("axis")
    return {"name": name, "features": len(spec["features"]), "method": extras.get("method"),
            "through_holes": extras.get("through_holes", 0),
            "blind_holes": extras.get("blind_holes", 0), "warnings": list(extras.get("warnings", [])),
            "extrude_axis": tuple(round(c, 3) for c in ax.to_tuple()) if ax is not None else None,
            "verified": None}


def recognize(step_path, name=None, verify=True, recover=True):
    """STEP file -> (IR spec, report). Recovers a feature tree by EXTRUDE (a 2D profile swept along an
    axis + holes) or REVOLVE. Several axes may look prismatic, so it gathers a candidate per axis and
    lets SELF-VERIFICATION pick the winner: first a candidate whose base re-emit VERIFIES on its own
    (Δvol≈0); else — with recover=True — multi-axis recovery (floor / through pockets + cross-axis
    holes as prism_cuts) is run per axis and the first that verifies wins (report.recovered has the
    tally). If nothing verifies, the closest residual is returned (report.verified is False)."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    name = name or Path(step_path).stem

    # candidate decompositions: one extrude per prismatic axis (rank order) + a revolve if applicable.
    cands = []
    for axis, n_bad in _extrude_axes(orig):
        try:
            cands.append(_extrude_along(orig, name, axis, n_bad))
        except ValueError:
            continue
    try:
        cands.append(_recognize_revolve(orig, name))
    except Exception:
        pass
    # Open shells (see _recognize_shell) and the general 2.5D form (see _recognize_layers), each
    # looked at along the three principal axes. These cut hundreds of sections, which is where
    # OpenCASCADE segfaults on unlucky geometry (CTC-02 died with "no output" in one run of three),
    # so each runs in its own fresh process -- a crash costs that candidate, not the part -- and
    # the six run concurrently.
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(kind, ax) for kind in ("_shell_file", "_layers_file")
            for ax in ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0))]
    # capped: six OCCT processes, each multi-threaded, saturated a 28-core box and it hung
    with ThreadPoolExecutor(int(os.environ.get("FEATURETREE_JOBS", "3"))) as pool:
        got = list(pool.map(lambda j: _isolated(j[0], (str(step_path), name, j[1]), fallback=None), jobs))
    for g in got:
        if g is not None:
            spec_, extras_ = g
            cands.append((spec_, {**extras_, "axis": Vector(*extras_["axis"])}))
    if not cands:
        raise ValueError("neither a recognizable extrude nor a body of revolution")
    if not verify:
        spec, extras = cands[0]
        return spec, _report_for(spec, extras, name)

    # Pass 1: prefer a candidate whose BASE verifies with NO recovery — the simplest clean extrude /
    # revolve (this is what keeps an L extruded along Y from being "recovered" as a box + a carve).
    reports = []
    for spec, extras in cands:
        report = _report_for(spec, extras, name)
        report.update(_isolated("_verify_file", (spec, str(step_path), report), fallback={
            "verified": False, "reason": "re-emit crashed the geometry kernel",
            "vol_orig": round(orig.volume, 1)}))
        if report["verified"]:
            return spec, report
        reports.append((spec, extras, report))

    # Pass 2: multi-axis recovery per extrude candidate (rank order); first that verifies wins.
    def _closeness(r):                  # best overlap first; volume error only when unmeasured
        return (-r["iou_pct"], 0.0) if r.get("iou_pct") is not None else (0.0, r.get("dvol_pct", 1e9))
    best = min(reports, key=lambda c: _closeness(c[2]))
    if recover:
        for spec, extras, report in reports:
            if extras.get("method") != "extrude" or extras.get("axis") is None:
                continue
            try:
                got = _isolated("_recover_file", (spec, str(step_path), extras["axis"].to_tuple(), name))
                if got is None:
                    continue                      # carving crashed the kernel for this candidate
                rspec, counts = got
            except Exception:
                continue
            rreport = _report_for(rspec, extras, name)
            rreport.update(_isolated("_verify_file", (rspec, str(step_path), rreport), fallback={
                "verified": False, "reason": "re-emit crashed the geometry kernel",
                "vol_orig": round(orig.volume, 1)}))
            rreport["recovered"] = counts
            rreport["warnings"].append(
                f"multi-axis recovery: +{counts['pockets']} pocket(s), +{counts['holes']} "
                f"cross-hole(s); {counts['residual_lumps']} residual lump(s) (chamfers/fillets) left uncut")
            if rreport["verified"]:
                return rspec, rreport
            if _closeness(rreport) < _closeness(best[2]):
                best = (rspec, extras, rreport)
    return best[0], best[2]


def _extrude_along(orig, name, axis, n_bad):
    """Recognize `orig` as a 2D profile extruded along the given `axis` (+ through/blind holes).
    Raises ValueError if the cross-section along this axis isn't a single recognizable profile."""
    warnings = []
    solid = _align_to_z(orig, axis)
    if abs(axis.dot(Vector(0, 0, 1))) < 1 - ANG:
        warnings.append(f"extrude axis {tuple(round(c, 3) for c in axis.to_tuple())} -> rotated onto Z")
    if n_bad:
        warnings.append(f"{n_bad} face(s) not captured by a single extrude (chamfers / cross-holes / "
                        "freeform) — best-effort, expect PARTIAL")
    bb = solid.bounding_box()
    base_z, top_z, thick = bb.min.Z, bb.max.Z, bb.size.Z

    # 1. OUTLINE + through-holes from a CROSS-SECTION, not a single end face — robust to stepped or
    # fragmented ends (a plate whose bottom is broken into islands still yields one clean outline).
    # Sample a few heights (not just one fraction) so the section doesn't land exactly on a feature
    # plane (a pocket floor) and fragment; take the first height giving one clean region.
    FRACS = (0.4, 0.27, 0.6, 0.5, 0.72, 0.33)
    secs = None
    for frac in FRACS:
        zc = base_z + frac * thick
        faces = solid.intersect(Plane.XY.offset(zc) * Rectangle(bb.size.X + 50, bb.size.Y + 50)).faces()
        if len(faces) == 1:
            secs = [faces[0]]
            break

    # MULTI-REGION FALLBACK. A section that is disjoint at every height is not a failure — it is a
    # part made of several parallel prisms (two bosses on a common axis, a forked bracket). Each
    # region is its own profile, so emit a sketch+pad per region rather than refusing. Only reached
    # when NO height gives a single region, so the single-profile path above is unchanged. Require
    # every region to classify: one freeform lobe still means this axis is the wrong story.
    if secs is None:
        for frac in FRACS:
            zc = base_z + frac * thick
            faces = solid.intersect(Plane.XY.offset(zc) * Rectangle(bb.size.X + 50, bb.size.Y + 50)).faces()
            if not (1 < len(faces) <= MAX_REGIONS):
                continue
            if all(_classify_wire(f.outer_wire(), []) is not None for f in faces):
                secs = sorted(faces, key=lambda f: -f.area)
                warnings.append(f"cross-section is {len(secs)} disjoint region(s) -> one pad each")
                break
    if secs is None:
        raise ValueError("cross-section is disjoint at every sampled height — not one extruded profile")

    # The largest region keeps the name 'body' so face-attached sketches (blind holes) still resolve.
    feats = []
    for i, sc in enumerate(secs):
        outline = _classify_wire(sc.outer_wire(), warnings)
        if outline is None:
            raise ValueError("outline not recognizable (lines + circular arcs only; has splines/ellipses)")
        sk = "outline" if i == 0 else f"outline{i}"
        feats.append(_sketch_from_outline(sk, outline))
        feats.append(IR.pad("body" if i == 0 else f"body{i}", sk, length=round(thick, 4)))

    # 2a. THROUGH holes = the section's INNER wires that are ACTUALLY through — present near BOTH end
    # faces (a blind pocket whose floor is below the section shows up here too, but only near one face;
    # emitting it as through would over-cut, and an over-cut can't be recovered, so we leave it for the
    # pocket-recovery pass). Any shape (circle, slot, obround, polygon, arcs) — circles become one drill
    # sketch, each non-circular wire a poly cut. `captured` collects the through wires' circular-edge
    # signatures (+ the outline's) so their cylinders aren't re-detected as blind holes below.
    def _sig(e):
        return (round(e.arc_center.X, 2), round(e.arc_center.Y, 2), round(e.radius, 2))
    thru_at = _through_centroids(solid, base_z, top_z, bb)
    captured = {_sig(e) for sc in secs for e in sc.outer_wire().edges().filter_by(GeomType.CIRCLE)}
    thru_circ, thru_poly = [], []
    for sc in secs:
        for w in sc.inner_wires():
            cl = _classify_wire(w, warnings)
            if cl is None or _wire_xy(w) not in thru_at:
                continue                      # blind / stepped -> recovery handles it (don't over-cut)
            captured.update(_sig(e) for e in w.edges().filter_by(GeomType.CIRCLE))
            (thru_circ if cl[0] == "circle" else thru_poly).append(
                (cl[1], cl[2], cl[3]) if cl[0] == "circle" else cl[1])
    if thru_circ:
        feats.append(IR.sketch("holes", "XY", circles=thru_circ))
        feats.append(IR.pocket("drill", "holes", through=True))
    for i, poly in enumerate(thru_poly):
        feats.append(IR.sketch(f"cut_sk{i}", "XY", polys=[poly]))
        feats.append(IR.pocket(f"cut{i}", f"cut_sk{i}", through=True))

    # 2b. BLIND holes = concave Z-cylinders that DON'T span the thickness (circular only), excluding
    # any cylinder whose circle belongs to a wire we already captured (outline arcs, through-holes).
    blind = []
    for f in solid.faces().filter_by(GeomType.CYLINDER):
        h = _cyl_hole(f, base_z, top_z, warnings, captured)
        if h is None or h["through"]:
            continue
        blind.append(h)
    for i, h in enumerate(blind):
        sk = IR.sketch(f"blind_sk{i}", circles=[(h["x"], h["y"], h["r"])],
                       on={"face_of": "body", "side": h["side"]})
        feats.append(sk)
        feats.append(IR.pocket(f"blind{i}", f"blind_sk{i}", through=False, length=round(h["depth"], 4)))

    spec = IR.part(name, *feats)
    return spec, {"method": "extrude", "axis": axis, "warnings": warnings, "regions": len(secs),
                  "through_holes": len(thru_circ) + len(thru_poly), "blind_holes": len(blind)}


def _sketch_from_outline(sk_name, outline):
    if outline[0] == "circle":
        _, cx, cy, r = outline
        return IR.sketch(sk_name, "XY", circles=[(cx, cy, r)])
    return IR.sketch(sk_name, "XY", polys=[outline[1]])


def _cyl_hole(face, base_z, top_z, warnings, outline_arcs=frozenset()):
    """A cylindrical face -> a hole dict if it is concave (material outside) with a ~Z axis and is
    NOT part of the outer wire (a rounded outline corner)."""
    ces = face.edges().filter_by(GeomType.CIRCLE)
    if not ces:
        return None
    ctr = ces[0].arc_center
    r = ces[0].radius
    if (round(ctr.X, 2), round(ctr.Y, 2), round(r, 2)) in outline_arcs:
        return None                       # this cylinder is an outline arc, already in the profile
    if any(abs(e.radius - r) > EPS for e in ces):
        return None                       # tapered/variable — not a straight bore
    zs = [e.arc_center.Z for e in ces]
    zlo, zhi = min(zs), max(zs)
    if zhi - zlo < EPS:
        return None                       # degenerate (a flat circle edge, not a wall)
    # concavity: outward surface normal points toward the axis => a hole (void outside)
    sp = face.position_at(0.5, 0.5)
    n = _normal(face)
    if n.X * (sp.X - ctr.X) + n.Y * (sp.Y - ctr.Y) >= 0:
        return None                       # convex -> outer wall, part of the outline
    through = (zlo - base_z) < EPS and (top_z - zhi) < EPS
    side = "top" if (top_z - zhi) < EPS else "bottom"
    return {"x": round(ctr.X, 4), "y": round(ctr.Y, 4), "r": round(r, 4),
            "through": through, "side": side, "depth": round(zhi - zlo, 4)}


def _isolated(fn_name, args, fallback=None, timeout=900):
    """Run step_recognize.<fn_name>(*args) in a FRESH Python process and return its result, or
    `fallback` if it crashes, errors or times out.

    OpenCASCADE can segfault inside a Boolean on unlucky geometry, and a segfault kills the whole
    recognition -- every candidate, not just the one that crashed (NIST CTC-02 came back as "no
    output" after 456 s although each candidate recognised fine alone). A first version FORKED,
    and deadlocked: forking a process with live threads can copy a lock another thread held, and
    the child waited on it forever at 0% CPU. A fresh interpreter cannot inherit a held lock; it
    costs a few seconds of start-up, and the timeout bounds any hang."""
    import pickle
    import subprocess
    code = ("import sys, pickle; sys.path.insert(0, %r); import step_recognize as sr; "
            "fn, a = pickle.load(sys.stdin.buffer); "
            "sys.stdout.buffer.write(b'@@PICKLE@@' + pickle.dumps(getattr(sr, fn)(*a)))"
            % os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run([sys.executable, "-c", code], input=pickle.dumps((fn_name, args)),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {**fallback, "reason": f"{fn_name} timed out after {timeout} s"} if isinstance(fallback, dict) else fallback
    i = p.stdout.find(b"@@PICKLE@@")
    if i < 0:
        why = (p.stderr.decode(errors="replace").strip().splitlines() or [f"exit code {p.returncode}"])[-1]
        return {**fallback, "reason": f"{fn_name}: {why[:200]}"} if isinstance(fallback, dict) else fallback
    return pickle.loads(p.stdout[i + len(b"@@PICKLE@@"):])


def _candidate_file(fn, step_path, name, axis_xyz):
    """fn(orig, name, axis) in an isolated process; None if it does not apply. The axis goes back
    as a tuple: OCCT vectors do not pickle."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    try:
        spec, extras = fn(orig, name, Vector(*axis_xyz))
    except Exception:
        return None
    return spec, {**extras, "axis": tuple(extras["axis"].to_tuple())}


def _shell_file(step_path, name, axis_xyz):
    return _candidate_file(_recognize_shell, step_path, name, axis_xyz)


def _layers_file(step_path, name, axis_xyz):
    return _candidate_file(_recognize_layers, step_path, name, axis_xyz)


def _verify_file(spec, step_path, report):
    """_verify in an isolated process: re-reads the input from its STEP file."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    return _verify(spec, orig, report)


def _recover_file(spec, step_path, axis_xyz, name):
    """_recover_multiaxis in an isolated process: re-reads the input from its STEP file."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    return _recover_multiaxis(spec, orig, Vector(*axis_xyz), name)


def _verify(spec, solid, report=None):
    """Re-emit the recognized IR and compare it to the original STEP: volume, rotation-tolerant
    bounding box, and -- when both of those pass -- a TWO-SIDED shape test.

    Volume and extents alone are not a sound acceptance test: over-cut and uncut material cancel in
    a volume difference. When layered recognition multiplied the candidates, two NIST parts passed
    them at 95.0% and 93.7% overlap (CTC-03, FTC-07) -- false accepts. So a VERIFIED tree must also
    overlap the input by >= IOU_TOL, measured without Boolean operations (iou_check) through the
    recogniser's own exact transform (input_frame). It runs only on candidates that already pass
    the cheap tests, so its cost is paid a handful of times per part."""
    try:
        part, res = b3d_emit.emit(spec)
    except Exception as e:
        return {"verified": False, "reason": f"re-emit failed: {e}", "vol_orig": round(solid.volume, 1)}
    ob, nb = solid.bounding_box(), part.bounding_box()
    # sorted bbox sizes: rotation-tolerant (the IR is rebuilt with the extrude axis on Z, which may
    # permute the original's axes)
    os_, ns_ = sorted([ob.size.X, ob.size.Y, ob.size.Z]), sorted([nb.size.X, nb.size.Y, nb.size.Z])
    dvol = abs(res["volume"] - solid.volume)
    dsize = max(abs(o - n) for o, n in zip(os_, ns_))
    ok = dvol <= VOL_TOL * solid.volume and dsize <= DIM_TOL
    out = {"verified": bool(ok), "vol_orig": round(solid.volume, 1), "vol_ir": res["volume"],
           "dvol": round(dvol, 2), "dvol_pct": round(100 * dvol / max(solid.volume, 1e-9), 2),
           "dsize_mm": round(dsize, 3)}
    # measured on every NEAR candidate, not only passing ones: when nothing verifies, the returned
    # PARTIAL is the one that overlaps best. Picking the closest VOLUME instead handed back FTC-08
    # at 93.5% overlap over a 98.6% sibling -- volume error is where compensating errors hide.
    if (dvol <= 0.05 * solid.volume) and report is not None and report.get("extrude_axis"):
        try:
            import iou_check
            place, _, _ = input_frame(solid, report)
            p, ci = iou_check.iou(solid, place(part), n=IOU_SAMPLES)
            out["iou_pct"], out["iou_ci_pct"] = round(100 * p, 3), round(100 * ci, 3)
            if ok and p < IOU_TOL:
                out["verified"] = False
                out["reason"] = (f"volume and extents match but the shapes overlap only "
                                 f"{100 * p:.2f}% (< {100 * IOU_TOL:g}%): compensating errors")
        except Exception as e:                   # an unmeasurable shape is not a verified one
            if ok:
                out["verified"] = False
                out["reason"] = f"two-sided shape test failed to run: {e}"
    return out

def _fixtures(tmp):
    """Generate known STEP fixtures from IR via b3d_emit: three in-scope prismatic parts +
    one out-of-scope part (an additive boss on top of the base) the recognizer must REJECT."""
    from build123d import export_step
    disc = IR.part("disc_hole",
                   IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("body", "o", length=8),
                   IR.sketch("h", "XY", circles=[(0, 0, 5)]), IR.pocket("drill", "h", through=True))
    boxpost = IR.part("boxpost",                 # RECTANGULAR base + a raised central post: not a
                      IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("body", "o", length=6),
                      IR.sketch("b", circles=[(0, 0, 6)], on={"face_of": "body", "side": "top"}),
                      IR.pad("post", "b", length=10))   # revolve (rect base) NOR a clean extrude (post)
    specs = {"plate": IR.SAMPLES["plate"](), "poly": IR.SAMPLES["poly"](),
             "disc_hole": disc, "boxpost": boxpost}
    paths = {}
    for nm, spec in specs.items():
        part, _ = b3d_emit.emit(spec)
        p = Path(tmp) / f"{nm}.step"
        export_step(part, str(p))
        paths[nm] = str(p)
    # OFF-AXIS: the L-bracket extruded along Z, rotated 90deg about X so it's extruded along Y —
    # the axis-agnostic recognizer must still find it and verify.
    lbr, _ = b3d_emit.emit(IR.SAMPLES["poly"]())
    p = Path(tmp) / "off_axis.step"
    export_step(lbr.rotate(Axis((0, 0, 0), (1, 0, 0)), 90), str(p))
    paths["off_axis"] = str(p)
    # ARC boundary: a D-shape (one rounded side, bulge) — profile has a circular arc, not just lines
    dshape = IR.part("dshape",
                     IR.sketch("o", "XY", polys=[[(-10, -10, 0.0), (10, -10, 0.0),
                                                  (10, 10, 0.0), (-10, 10, 0.6)]]),
                     IR.pad("body", "o", length=5))
    dpart, _ = b3d_emit.emit(dshape)
    p = Path(tmp) / "dshape.step"
    export_step(dpart, str(p))
    paths["dshape"] = str(p)
    # REVOLVE: a cone frustum with a bore (a conical face -> the extrude path can't do it, so the
    # dispatcher must route to revolve). Meridian (r, axial): r2..r10 slanted side + r2 bore.
    cone = IR.part("cone",
                   IR.sketch("m", "XZ", polys=[[(2, -8), (10, -8), (2, 8)]]),
                   IR.revolve("body", "m", angle=360.0))
    cpart, _ = b3d_emit.emit(cone)
    p = Path(tmp) / "cone.step"
    export_step(cpart, str(p))
    paths["cone"] = str(p)
    # SLOT: a plate with a native OBROUND through-hole (non-circular hole — arcs in a hole wire)
    from build123d import BuildPart, BuildSketch, Box, SlotOverall, extrude, Mode, Plane
    with BuildPart() as sp:
        Box(30, 16, 5)
        with BuildSketch(Plane.XY):
            SlotOverall(12, 6)
        extrude(amount=5, both=True, mode=Mode.SUBTRACT)
    p = Path(tmp) / "slot.step"
    export_step(sp.part, str(p))
    paths["slot"] = str(p)
    # OUT OF SCOPE: a loft from a square to a circle. The section changes SHAPE continuously with
    # height -- neither prismatic, nor drafted, nor a body of revolution -- so no stack of pads and
    # pockets reproduces it, and the honest answer is PARTIAL.
    from build123d import BuildPart as _BP, BuildSketch as _BS, Circle as _C, loft as _loft
    with _BP() as lp:
        with _BS(Plane.XY):
            Rectangle(30, 30)
        with _BS(Plane.XY.offset(25)):
            _C(10)
        _loft()
    p = Path(tmp) / "loft.step"
    export_step(lp.part, str(p))
    paths["loft"] = str(p)
    return paths


def selftest():
    import tempfile
    paths = _fixtures(tempfile.mkdtemp())
    problems = []
    for nm in ("plate", "poly", "disc_hole", "off_axis", "dshape", "cone", "slot",
               "boxpost"):                                                           # in scope -> VERIFY
        _, rep = recognize(paths[nm])
        print(f"  {nm:10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  "
              f"vol Δ{rep['dvol_pct']}%  (method={rep.get('method')}, thru={rep['through_holes']})")
        if not rep["verified"]:
            problems.append(f"{nm}: expected VERIFIED, got Δ{rep['dvol_pct']}%")
    # boxpost (a base + an additive post) was the out-of-scope case until layered 2.5D recovery;
    # it now verifies as two stacked layers, and the loft is the part that must NOT verify.
    _, rep = recognize(paths["loft"])                # out of scope -> must be flagged PARTIAL
    print(f"  {'loft':10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  vol Δ{rep['dvol_pct']}%  "
          "(square-to-circle loft — no stack of pads and pockets reproduces it)")
    if rep["verified"]:
        problems.append("loft: must NOT verify (nothing in the vocabulary can reproduce it)")
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 1
    print("PASS: 2.5D-prismatic parts recognized + re-emit-verified; out-of-scope flagged, not faked")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if not args:
        print(__doc__)
        return 0
    step_path = args[0]
    spec, report = recognize(step_path)
    v = report.get("verified")
    tag = "VERIFIED" if v else ("PARTIAL" if v is False else "UNVERIFIED")
    print(f"recognize {step_path}:")
    print(f"  tree: {' -> '.join(f['name'] for f in spec['features'])}")
    print(f"  {report['through_holes']} through hole(s), {report['blind_holes']} blind")
    if "vol_orig" in report:
        print(f"  volume: STEP {report['vol_orig']}  vs re-emitted IR {report.get('vol_ir','?')}  "
              f"(Δ{report.get('dvol_pct','?')}%, bbox Δ{report.get('dsize_mm','?')}mm)")
    for w in report["warnings"]:
        print(f"  ! {w}")
    print(f"  => {tag}" + ("" if v else " — fall back to importing the STEP as one solid"))

    if "--intent" in args:
        # Design intent (see intent.py): holes as holes, patterns, units, standard sizes. It only
        # re-expresses the tree, so it cannot change the verdict above.
        import intent
        ob = import_step(str(step_path))
        sb = (ob.solids()[0] if ob.solids() else ob).bounding_box()
        d = intent.describe(spec, extents=(sb.size.X, sb.size.Y, sb.size.Z))
        print(f"  intent: {d['units']} units; {len(d['holes'])} round feature(s), "
              f"{len(d['counterbores'])} counterbore(s)")
        for dia, lab in d["nominals"].items():
            n = sum(1 for h in d["holes"] if round(h.diameter, 3) == dia)
            print(f"    Ø{dia:<9g} x{n:<3} {lab or '(no standard size)'}")
        for p in d["patterns"]:
            if p["type"] == "single":
                continue
            geom = (f"PCD {p['pcd']:.3f}" if "pcd" in p else f"pitch {p['pitch']:.3f}"
                    + (f" x {p['pitch2']:.3f}" if "pitch2" in p else ""))
            print(f"    pattern: {p['type']:<9} {len(p['holes'])} x Ø{p['diameter']:.3f}  {geom}")

    if "--iou" in args:
        # Two-sided, Boolean-FREE check (see iou_check.py): over-cut and uncut material cannot
        # cancel as they can in the volume test above, and no OCCT union is trusted.
        import iou_check
        rec, _ = b3d_emit.emit(spec)
        orig = import_step(str(step_path))
        r = iou_check.registered_iou(orig, rec)
        print(f"  IoU (Boolean-free, point-sampled): {'>= ' if not v else ''}"
              f"{100 * r['iou']:.2f}% ± {100 * r['ci']:.2f} (95%)"
              + ("" if v else "  [lower bound: best of 24 axis-aligned registrations]"))

    def _arg(flag):
        return Path(args[args.index(flag) + 1]) if flag in args else None
    if _arg("--emit"):
        _arg("--emit").write_text(json.dumps(spec, indent=2))
        print(f"  wrote {_arg('--emit')}  (recovered IR)")
    if _arg("--stl"):
        from build123d import export_stl
        part, _ = b3d_emit.emit(spec)
        export_stl(part, str(_arg("--stl")))
        print(f"  wrote {_arg('--stl')}  (build123d solid — view in any mesh viewer)")
    if _arg("--fcstd"):
        import gen                                   # emits via FreeCAD (needs the AppImage)
        gen.emit(spec, str(_arg("--fcstd")))
        print(f"  wrote {_arg('--fcstd')}  (editable FreeCAD tree)")
    return 0 if v else 1


if __name__ == "__main__":
    sys.exit(main())
