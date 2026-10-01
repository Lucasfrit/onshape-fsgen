"""b3d_emit.py — emit a featuretree IR into a build123d Solid (in-process, no FreeCAD).

The third featuretree backend. One IR drives FreeCAD (fc_build, an editable feature tree),
Onshape (onshape_emit), and — here — build123d, a code-CAD B-rep in the caller's own Python.
build123d and FreeCAD share the OpenCASCADE kernel, so the SAME IR yields the SAME geometry:
the plate sample comes out 11497.3 mm^3 either way. That's the point — author the design once
as IR, get both the parametric tree AND a watertight solid you can mesh / sim / interfere-check,
with no second hand-maintained model to drift.

Feature coverage mirrors fc_build.py: sketches (circles / rects / polygons-with-holes) on the
XY plane or attached to a part's top/bottom face; pad (extrude, optional midplane); pocket
(through or blind depth); fillet (edges chosen by the same query the IR stores, resolved against
live geometry — never a stored kernel id).

    python b3d_emit.py --sample plate [out.stl]   # built-in samples: plate | poly | coupling*
    from b3d_emit import emit; part, res = emit(spec)   # part is a build123d Solid
    (* coupling_plate is a software-mfg sample; here `plate` and `poly` are built in.)
"""

import json
import math
import sys
from pathlib import Path

from build123d import (Align, Axis, BuildLine, BuildSketch, Circle, Cylinder, GeomType, Line,
                       Plane, Polygon, Pos, Rectangle, Rot, SagittaArc, export_stl, extrude,
                       fillet, make_face, revolve)

_BIG = 1.0e4  # a through-cut overshoot (mm), clipped by the actual solid


def _poly_face(poly, plane_obj):
    """A wire of (x, y[, bulge]) vertices -> a build123d face on plane_obj. bulge is the DXF arc
    factor tan(theta/4) for the segment to the NEXT vertex (0 / absent = straight); sign = CCW(+)."""
    pts = [(float(p[0]), float(p[1])) for p in poly]
    bul = [float(p[2]) if len(p) > 2 else 0.0 for p in poly]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts, bul = pts[:-1], bul[:-1]
    if all(abs(b) < 1e-4 for b in bul):                  # BULGE_EPS (targets/_geom.py)
        return plane_obj * Polygon(*pts, align=None)     # straight polygon (fast path)
    # Build the arc profile in LOCAL XY, then place it onto plane_obj with a left-multiply — same as
    # the fast path. (Building directly on plane_obj drops the origin's IN-PLANE offset, which is
    # invisible for Z-normal offset planes but wrong for a cross-axis plane whose origin has an
    # in-plane Z component.)
    n = len(pts)
    with BuildSketch(Plane.XY) as sk:
        with BuildLine(Plane.XY):
            for i in range(n):
                p1, p2, b = pts[i], pts[(i + 1) % n], bul[i]
                if abs(b) < 1e-4:
                    Line(p1, p2)
                else:
                    chord = math.dist(p1, p2)
                    SagittaArc(p1, p2, b * chord / 2.0)
        make_face()
    return plane_obj * sk.sketch


def _polys_region(polys, plane_obj):
    """The region the closed polys bound, by NESTING — the same rule FreeCAD's Pad applies to a
    sketch's closed wires: a poly inside another is a hole in it, an island inside a hole is solid
    again, and a poly outside every other is a separate region. So "polys[0] outer, polys[1:]
    holes" still means exactly that, and a layer of several disjoint outlines (four feet, a row of
    lugs) is several regions in one sketch rather than being mis-read as holes. None if empty."""
    if not polys:
        return None
    faces = [_poly_face(p, plane_obj) for p in polys]
    if len(faces) == 1:
        return faces[0]
    loops = [[(q[0], q[1]) for q in p] for p in polys]
    areas = [abs(f.area) for f in faces]

    def pip(x, y, poly):
        inside = False
        for i in range(len(poly)):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % len(poly)]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    order = sorted(range(len(faces)), key=lambda i: -areas[i])
    parent, depth = {}, {}
    for n, i in enumerate(order):
        x, y = loops[i][0]
        best = None
        for j in order[:n]:
            if areas[j] > areas[i] and pip(x, y, loops[j]) and (best is None or areas[j] < areas[best]):
                best = j
        parent[i] = best
        depth[i] = 0 if best is None else depth[best] + 1
    region = None
    for i in order:
        if depth[i] % 2:
            continue
        r = faces[i]
        for k in order:
            if parent.get(k) == i:
                r = r - faces[k]
        region = r if region is None else region + r
    return region


def _sketch_faces(f, z0, plane="XY"):
    """The IR sketch's closed regions as build123d faces. On XY: circles/rects each their own
    region, polys[0] an outer profile with polys[1:] as holes, all at z=z0. On XZ (a revolve
    profile): polys placed in the XZ plane (x = radius, z = axial). polys may carry arcs (bulge)."""
    if plane == "XZ":
        r = _polys_region(f.get("polys", []), Plane.XZ)   # (x,y)->(radius, axial)
        return [r] if r is not None else []
    pl = Plane.XY.offset(z0)
    faces = []
    for (cx, cy, r) in f.get("circles", []):
        faces.append(Pos(cx, cy, z0) * Circle(r))
    for (w, h, cx, cy) in f.get("rects", []):
        faces.append(Pos(cx, cy, z0) * Rectangle(w, h))
    region = _polys_region(f.get("polys", []), pl)
    if region is not None:
        faces.append(region)
    return faces


def _face_z(part, side):
    bb = part.bounding_box()
    return bb.max.Z if side == "top" else bb.min.Z


def _resolve_fillet_edges(part, select):
    """Mirror fc_common.resolve_edges: circular edges on the top face; 'top_outer' = the
    largest-radius one. Resolved against the live solid, so it survives edits/rebuilds."""
    want = select.get("circles")
    top_z = part.bounding_box().max.Z
    cands = [e for e in part.edges().filter_by(GeomType.CIRCLE)
             if abs(e.arc_center.Z - top_z) < 1e-6]
    if want == "top_outer":
        cands = sorted(cands, key=lambda e: -e.radius)[:1]
    return cands


def _bool_fuzzy(a, b, op_cls):
    from build123d import Shape
    from OCP.TopTools import TopTools_ListOfShape
    op = op_cls()
    args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(a.wrapped)
    tools.Append(b.wrapped)
    op.SetArguments(args)
    op.SetTools(tools)
    op.SetFuzzyValue(1e-4)
    op.Build()
    if not op.IsDone() or op.Shape().IsNull():
        raise ValueError("Boolean failed, including with a fuzzy tolerance")
    return Shape.cast(op.Shape())


def _fuse(a, b):
    """a + b, retried with a small fuzzy tolerance when OpenCASCADE returns a null shape — which
    it does for solids with near-coincident faces, e.g. layers stacked exactly on each other."""
    try:
        return a + b
    except Exception:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        return _bool_fuzzy(a, b, BRepAlgoAPI_Fuse)


def _cut(a, b):
    """a - b, with the same fuzzy retry as _fuse."""
    try:
        return a - b
    except Exception:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        return _bool_fuzzy(a, b, BRepAlgoAPI_Cut)


def emit(spec):
    """Build the IR `spec` into a build123d Solid. Returns (part, result_dict) where the
    result mirrors fc_common.result (tree / volume / editable params) for cross-backend parity."""
    part = None
    sketches = {}          # name -> (faces, z0)  consumed by the next pad/pocket/revolve
    planes = {}            # name -> "XY" | "XZ"
    tree = []
    params = {}
    volumes = []           # solid volume after each feature (None before the first solid)
    sides = {}             # name -> "top" | "bottom" for a face-attached sketch, else None

    for f in spec["features"]:
        kind = f["kind"]
        tree.append((f["name"], f"build123d::{kind}"))
        if kind == "sketch":
            on = f.get("on")
            if on:
                # Attach the sketch to the part's top/bottom face: circles, rects, and polys
                # (arbitrary outlines) all build at that face's z — this is what lets a blind
                # pocket take a non-circular floor outline (the residual pocket-recovery path).
                z0 = _face_z(part, on.get("side", "top"))
                plane = "XY"
            else:
                plane = f.get("plane", "XY")
                if plane not in ("XY", "XZ"):
                    raise ValueError(f"sketch plane must be XY or XZ (v0), got {plane}")
                z0 = 0.0
            sketches[f["name"]] = (_sketch_faces(f, z0, plane), z0)
            planes[f["name"]] = plane
            sides[f["name"]] = (on or {}).get("side", "top") if on else None
            radii = [c[2] for c in f.get("circles", [])]
            if radii:
                params[f["name"]] = {"radii": [round(r, 4) for r in radii]}
        elif kind == "pad":
            faces, z0 = sketches[f["sketch"]]
            length = f["length"]
            # Deterministic direction: grow +Z for an XY / top-face sketch, -Z only for a bottom-face
            # one. extrude(fc, amount) alone follows the region's winding-dependent normal, so the same
            # IR could pad up in one backend and down in another (breaking cross-backend parity and any
            # absolute-coord prism_cut placed against it). A sketch attached to the BOTTOM face grows
            # outward, -Z, as FreeCAD's face-attached Pad does. That is decided by the sketch's declared
            # side, not by its height: a bottom face at z = 0 is still a bottom face.
            d = -1.0 if sides.get(f["sketch"]) == "bottom" else 1.0
            taper = f.get("taper", 0.0)
            if taper and f.get("symmetric"):
                raise ValueError(f"pad '{f['name']}': taper with symmetric is not supported")
            solid = None
            for fc in faces:
                s = (extrude(fc, amount=length / 2, both=True) if f.get("symmetric")
                     else extrude(fc, amount=length, dir=(0, 0, d), taper=taper))
                solid = s if solid is None else solid + s
            part = solid if part is None else _fuse(part, solid)
            params[f["name"]] = {"length": round(float(length), 4)}
        elif kind == "pocket":
            faces, z0 = sketches[f["sketch"]]
            if f["through"]:
                cutter = None
                for fc in faces:
                    s = extrude(fc, amount=_BIG, both=True)
                    cutter = s if cutter is None else cutter + s
                params[f["name"]] = {"length": round(float(part.bounding_box().size.Z), 4),
                                     "type": "ThroughAll"}
            else:
                depth = f["length"]
                # blind: cut INTO the material from the sketch face (top face -> downward)
                sign = -1.0 if z0 >= _face_z(part, "top") - 1e-6 else 1.0
                cutter = None
                for fc in faces:
                    # explicit direction so `taper` always means "shrinks going INTO the part"
                    s = extrude(fc, amount=depth, dir=(0, 0, sign), taper=f.get("taper", 0.0))
                    cutter = s if cutter is None else cutter + s
                params[f["name"]] = {"length": round(float(depth), 4), "type": "Length"}
            part = _cut(part, cutter)
        elif kind == "fillet":
            edges = _resolve_fillet_edges(part, f["select"])
            if not edges:
                raise ValueError(f"fillet '{f['name']}' selected no edges")
            part = fillet(edges, f["radius"])
            params[f["name"]] = {"radius": round(float(f["radius"]), 4)}
        elif kind == "revolve":
            faces, _ = sketches[f["sketch"]]
            if planes.get(f["sketch"]) != "XZ":
                raise ValueError("revolve needs an XZ-plane profile sketch")
            angle = f.get("angle", 360.0)
            solid = None
            for fc in faces:
                s = revolve(fc, Axis.Z, revolution_arc=angle)
                solid = s if solid is None else solid + s
            part = solid if part is None else _fuse(part, solid)
            params[f["name"]] = {"angle": round(float(angle), 4)}
        elif kind == "polar_pocket":
            n, r, L = int(f["count"]), f["radius"], f["length"]
            mr, z0p, phase = f["mount_r"], f.get("z", 0.0), f.get("phase", 0.0)
            cutter = None
            for i in range(n):
                a = phase + 360.0 * i / n
                px, py = mr * math.cos(math.radians(a)), mr * math.sin(math.radians(a))
                # Cylinder along +Z, centered; Rot(90 about X) lays it along +Y, Rot(a about Z)
                # points it along the tangent at azimuth a; Pos drops it on the roller station.
                cyl = Pos(px, py, z0p) * Rot(0, 0, a) * Rot(90, 0, 0) * Cylinder(r, L)
                cutter = cyl if cutter is None else cutter + cyl
            part = _cut(part, cutter)
            params[f["name"]] = {"count": n, "radius": round(float(r), 4)}
        elif kind == "prism_cut":
            # a profile in the plane {origin, x_dir, normal}, extruded `depth` along +normal, cut.
            # Extrude along the EXPLICIT normal (dir=), not the region face's own normal — the latter
            # depends on poly winding and would cut the wrong way for a hole-in-region (e.g. a pocket
            # ledge annulus), silently over-cutting.
            nrm = tuple(f["normal"])
            pl = Plane(origin=tuple(f["origin"]), x_dir=tuple(f["xdir"]), z_dir=nrm)
            region = _polys_region(f["polys"], pl)
            if region is not None:
                part = _cut(part, extrude(region, amount=f["depth"], dir=nrm, taper=f.get("taper", 0.0)))
            params[f["name"]] = {"depth": round(float(f["depth"]), 4)}
        else:
            raise ValueError(f"unknown feature kind: {kind}")
        volumes.append(None if part is None else round(float(part.volume), 3))

    result = {"tree": tree, "volume": round(float(part.volume), 1), "params": params,
              "volumes": volumes}
    return part, result


def main():
    import ir as IR
    args = sys.argv[1:]
    if args and args[0] == "--sample":
        name = args[1] if len(args) > 1 else "plate"
        spec = IR.SAMPLES[name]()
        out = Path(args[2]) if len(args) > 2 else Path(__file__).parent / "out" / f"{spec['name']}.stl"
    elif args:
        spec = json.loads(Path(args[0]).read_text())
        out = Path(args[1]) if len(args) > 1 else Path(args[0]).with_suffix(".stl")
    else:
        print(__doc__)
        return 0
    part, res = emit(spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    export_stl(part, str(out))
    print(f"emitted {out}  (build123d solid, {res['volume']} mm^3)")
    print("tree:")
    for label, tid in res["tree"]:
        print(f"  {label:16} {tid}")
    print("params:", json.dumps(res["params"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
