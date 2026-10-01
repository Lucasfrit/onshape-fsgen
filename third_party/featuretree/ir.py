"""ir.py — a neutral, named feature-tree IR (a small CAD DSL).

The operation-preserving representation that no neutral *file* format provides: a
declarative, ordered list of named features with named parameters. It is the single
source of truth; per-target emitters (FreeCAD here) translate it into that tool's
*native* feature vocabulary, so the tree shows up editable on the left.

Design choices that make round-trip tractable:
  * every feature has a stable, human-meaningful `name` (-> the target object's Label)
    so a human edit can be matched back by name, not by kernel edge id;
  * parameters are named values, not positions in a blob;
  * geometry is referenced symbolically (a feature name + plane / a query), never by
    unstable kernel edge/face ids — the wall that kills neutral feature files.

Scope: sketches (rectangles, circles, AND arbitrary polygons/profiles-with-holes) on
principal planes or face-attached, Pad, Pocket (through/depth), Fillet (edge query).
Plain JSON-able dicts, so the IR crosses cleanly into FreeCAD's own Python (3.11).
"""


def sketch(name, plane="XY", circles=(), rects=(), polys=(), on=None):
    """A 2D sketch.
        circles: [(cx, cy, r), ...]
        rects:   [(w, h, cx, cy), ...]
        polys:   [wire, ...] where wire = [(x, y), ...]  (closed automatically).
                 The FIRST poly is the outer profile; any following polys are holes
                 in it — so an extruded profile-with-holes is one sketch -> one pad.
        plane: "XY" (default) or "XZ" — use "XZ" for a revolve profile (x = radius, z = axial).
        on: None -> the `plane`; or a face QUERY {"face_of": feature, "side": "top"|"bottom"}
            (coords stay global; the emitter maps them into the face's local frame).
    """
    return {"kind": "sketch", "name": name, "plane": plane, "on": on,
            "circles": [list(c) for c in circles],
            "rects": [list(r) for r in rects],
            "polys": [[list(p) for p in poly] for poly in polys]}


def pad(name, sketch, length, symmetric=False, taper=0.0):
    """Extrude a sketch. `taper` (degrees) drafts the walls: POSITIVE shrinks the profile along the
    extrusion, negative grows it — build123d's convention, which the FreeCAD emitter maps onto
    PartDesign's TaperAngle. Molded parts carry ~0.5-3 deg of draft so they release from the
    mould. The key is only written when non-zero, so untapered IR is unchanged."""
    d = {"kind": "pad", "name": name, "sketch": sketch, "length": length, "symmetric": symmetric}
    if taper:
        d["taper"] = float(taper)
    return d


def pocket(name, sketch, through=True, length=None, taper=0.0):
    """Cut a sketch into the solid. `taper` (degrees, blind pockets only): POSITIVE shrinks the
    profile as the cut goes deeper — the drafted cavity of a molded box."""
    d = {"kind": "pocket", "name": name, "sketch": sketch, "through": through, "length": length}
    if taper:
        if through:
            raise ValueError("a tapered pocket must be blind (through=False)")
        d["taper"] = float(taper)
    return d


def fillet(name, radius, select):
    """Round edges chosen by a QUERY (resolved against live geometry at build time,
    never stored kernel edge ids). e.g. select={"circles": "top_outer"}."""
    return {"kind": "fillet", "name": name, "radius": radius, "select": select}


def revolve(name, sketch, angle=360.0):
    """Revolve a profile sketch about the Z axis by `angle` degrees. The sketch must lie in a
    plane CONTAINING the axis — the XZ plane (plane="XZ"), with all radii x >= 0 — and its `polys`
    give the cross-section. This is the primitive round parts need (wheels, bosses, nozzles) that
    sketch/pad can't: a body of revolution."""
    return {"kind": "revolve", "name": name, "sketch": sketch, "angle": angle}


def polar_pocket(name, radius, length, mount_r, z=0.0, count=4, phase=0.0):
    """Cut `count` cylindrical pockets evenly spaced about the Z axis, each of `radius` and
    `length`, its axis TANGENT to the circle of radius `mount_r` at axial height `z`, the ring
    rotated by `phase` degrees. Models a ring of roller pockets (omni wheel), a pin circle on a
    hub, etc. — a polar pattern of a tangent bore that plain pocket() (axial only) can't place."""
    return {"kind": "polar_pocket", "name": name, "radius": radius, "length": length,
            "mount_r": mount_r, "z": z, "count": count, "phase": phase}


def prism_cut(name, origin, normal, xdir, depth, polys=(), taper=0.0):
    """Subtract a 2D profile extruded along an arbitrary axis at an arbitrary location. The profile
    (polys — [outer, hole, hole, ...], each a wire of (u, v[, bulge]) in the plane's own 2D frame)
    lies in the plane through `origin` with local +X = `xdir` and outward normal `normal`; it is
    extruded `depth` along +normal and cut from the running solid.

    This is the general placed cut the STEP recognizer emits for RECOVERED features that plain
    face-attached pocket() can't place: multi-level / through pockets along the main axis (normal ∥
    the extrude axis) AND cross-axis holes (normal ⊥ it). One primitive, any axis, any location —
    a profile swept along some direction, subtracted. `taper` (degrees) drafts it: positive shrinks
    the profile along +normal, as for pad/pocket."""
    return {"kind": "prism_cut", "name": name,
            "origin": [round(float(c), 6) for c in origin],
            "normal": [round(float(c), 6) for c in normal],
            "xdir": [round(float(c), 6) for c in xdir], "depth": depth,
            "polys": [[list(p) for p in poly] for poly in polys]} | ({"taper": float(taper)} if taper else {})


def part(name, *features):
    return {"name": name, "features": list(features)}


def update_from_params(spec, params):
    """Flow human edits read back from any backend ({feature name: {key: value}} — FreeCAD's
    fc_read, the Fusion read script, SolidWorks --dump) into the IR, matched by feature name."""
    for f in spec["features"]:
        p = params.get(f["name"])
        if not p:
            continue
        if f["kind"] in ("pad", "pocket") and "length" in p and f.get("length") is not None:
            f["length"] = p["length"]
        if f["kind"] == "fillet" and "radius" in p:
            f["radius"] = p["radius"]
        if f["kind"] == "revolve" and "angle" in p:
            f["angle"] = p["angle"]
        if f["kind"] == "prism_cut" and "depth" in p:
            f["depth"] = p["depth"]
        if f["kind"] == "sketch" and "radii" in p:
            for i, r in enumerate(p["radii"]):
                if i < len(f["circles"]):
                    f["circles"][i][2] = r
    return spec


update_from_freecad = update_from_params     # the original name, kept for existing callers


def validate(spec):
    """Well-formedness of an IR spec, as a list of problems (empty = valid). Every backend is
    entitled to assume these hold; build123d happens to tolerate some violations (it unions
    loops one at a time) where FreeCAD and Onshape reject them, so a spec that breaks a rule can
    VERIFY in one backend and fail in another.

      names      feature names unique; every sketch reference names an EARLIER sketch
      loops      closed: >= 3 segments, or >= 2 if any is an arc; no zero-length segment; no
                 self-intersection
      profiles   within one sketch, loop boundaries never touch or cross (nesting is fine)
      pocket     a taper needs a blind pocket; a blind pocket needs a positive length
      placement  prism_cut normal and xdir are non-zero and orthogonal

    Loop checks need shapely; without it they are skipped and reported as one problem."""
    import math
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets"))
    import _geom as G
    problems, seen, sketches = [], set(), set()

    def loops_ok(where, loops):
        try:
            from shapely.geometry import LineString, Polygon
        except ImportError:
            problems.append(f"{where}: loop checks skipped (pip install shapely)")
            return
        polys = []
        for i, lp in enumerate(loops):
            seg = lp["segments"]
            if seg[0][0] != "circle":
                arcs = any(sg[0] == "arc" for sg in seg)
                if len(seg) < (2 if arcs else 3):          # two arcs close a loop; lines need three
                    problems.append(f"{where}: loop {i} has {len(seg)} segment(s)")
                    continue
                if any(math.dist(sg[1], sg[-1]) < 1e-6 for sg in seg):
                    problems.append(f"{where}: loop {i} has a zero-length segment")
            poly = Polygon(G.loop_points(lp))
            if not poly.is_valid:
                problems.append(f"{where}: loop {i} self-intersects")
            polys.append((i, poly))
        for a in range(len(polys)):
            for b in range(a + 1, len(polys)):
                (i, p), (j, q) = polys[a], polys[b]
                if LineString(p.exterior.coords).intersects(LineString(q.exterior.coords)):
                    problems.append(f"{where}: loops {i} and {j} touch or cross")

    for f in spec["features"]:
        n, k = f["name"], f["kind"]
        if n in seen:
            problems.append(f"{n}: duplicate feature name")
        seen.add(n)
        if k == "sketch":
            sketches.add(n)
            loops_ok(n, G.sketch_loops(f.get("circles", []), f.get("rects", []), f.get("polys", [])))
        elif k in ("pad", "pocket", "revolve"):
            if f["sketch"] not in sketches:
                problems.append(f"{n}: sketch '{f['sketch']}' is not an earlier sketch")
            if k == "pocket" and not f["through"] and not (f.get("length") or 0) > 0:
                problems.append(f"{n}: blind pocket needs a positive length")
            if k == "pocket" and f["through"] and f.get("taper"):
                problems.append(f"{n}: taper on a through pocket")
        elif k == "prism_cut":
            nn, xx = f["normal"], f["xdir"]
            if not any(nn) or not any(xx) or abs(sum(a * b for a, b in zip(nn, xx))) > 1e-6:
                problems.append(f"{n}: normal and xdir must be non-zero and orthogonal")
            loops_ok(n, G.sketch_loops(polys=f["polys"]))
    return problems


# --- built-in samples (also a smoke test: volume must match a build123d twin) ---

def sample_plate():
    """40x30x10 plate with an 8 mm through hole. FreeCAD volume == 11497.3 mm^3."""
    return part(
        "plate",
        sketch("outline", "XY", rects=[(40, 30, 0, 0)]),
        pad("body", "outline", length=10),
        sketch("hole_sketch", "XY", circles=[(0, 0, 4)]),
        pocket("hole", "hole_sketch", through=True),
    )


def sample_poly():
    """An L-shaped polygon profile + a hole, extruded — exercises the polygon primitive."""
    L = [(0, 0), (40, 0), (40, 14), (16, 14), (16, 30), (0, 30)]
    return part(
        "lbracket",
        sketch("profile", "XY", polys=[L]),
        pad("body", "profile", length=6),
        sketch("hole_sketch", "XY", circles=[(8, 8, 2.6)]),
        pocket("hole", "hole_sketch", through=True),
    )


SAMPLES = {"plate": sample_plate, "poly": sample_poly}
