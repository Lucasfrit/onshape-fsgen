"""fc_common.py — shared FreeCAD-side helpers (runs under freecadcmd).

Side-effect free so both fc_build.py and fc_read.py can import it.
"""

import Part


def resolve_edges(shape, select):
    """Resolve an edge QUERY against live geometry -> ['Edge3', ...].

    The query (e.g. {"circles": "top_outer"}) is what the IR stores; the EdgeN names
    are recomputed here every build, so they never go stale (no persistent kernel ids).
    Supports circular edges on the top face: "top_outer" (largest) / "top_all".
    """
    want = select.get("circles")
    zmax = max((v.Z for v in shape.Vertexes), default=0.0)
    cands = []
    for i, e in enumerate(shape.Edges):
        cu = e.Curve
        if isinstance(cu, Part.Circle) and abs(cu.Center.z - zmax) < 1e-6:
            cands.append((i + 1, cu.Radius))
    if want == "top_outer":
        cands.sort(key=lambda t: -t[1])
        cands = cands[:1]
    return [f"Edge{i}" for i, _ in cands]


def resolve_face(shape, side="top"):
    """Resolve a face QUERY -> 'FaceN'. The Z-normal planar face at max ('top') or
    min ('bottom') z. Query stored in IR; FaceN recomputed each build."""
    planar = []
    for i, f in enumerate(shape.Faces):
        s = f.Surface
        if isinstance(s, Part.Plane) and abs(abs(s.Axis.z) - 1.0) < 1e-3:
            planar.append((i + 1, f.CenterOfMass.z))
    if not planar:
        raise ValueError("no Z-normal planar face to attach to")
    planar.sort(key=lambda t: -t[1] if side == "top" else t[1])
    return f"Face{planar[0][0]}"


def result(doc):
    """Extract the as-built tree, final volume, and editable params (keyed by Label)."""
    body = next(o for o in doc.Objects if o.TypeId == "PartDesign::Body")
    feats = [o for o in doc.Objects if o.TypeId.startswith(("PartDesign", "Sketcher"))]
    params = {}
    for o in feats:
        if o.TypeId in ("PartDesign::Pad", "PartDesign::Pocket"):
            params[o.Label] = {"length": round(float(o.Length), 4)}
            if o.TypeId == "PartDesign::Pocket":
                params[o.Label]["type"] = str(o.Type)
        elif o.TypeId == "PartDesign::Fillet":
            params[o.Label] = {"radius": round(float(o.Radius), 4)}
        elif o.TypeId == "PartDesign::Revolution":
            params[o.Label] = {"angle": round(float(o.Angle), 4)}
        elif o.TypeId == "Sketcher::SketchObject":
            radii = [round(g.Radius, 4) for g in o.Geometry if isinstance(g, Part.Circle)]
            if radii:
                params[o.Label] = {"radii": radii}
    return {
        "tree": [(o.Label, o.TypeId) for o in feats],
        "volume": round(float(body.Shape.Volume), 1),
        "params": params,
    }


def apply_edits(doc, edits):
    """Apply {label: {key: value}} edits to named features — a human dimension change.

    Supports pad/pocket 'length' and sketch 'radius' (first circle). Returns the
    labels actually changed.
    """
    changed = []
    by_label = {o.Label: o for o in doc.Objects}
    for label, kv in edits.items():
        o = by_label.get(label)
        if o is None:
            continue
        if "length" in kv and hasattr(o, "Length"):
            o.Length = float(kv["length"])
            changed.append(label)
        if "radius" in kv:
            if o.TypeId == "Sketcher::SketchObject":
                for i, g in enumerate(o.Geometry):
                    if isinstance(g, Part.Circle):
                        o.setRadius(i, float(kv["radius"]))
                        changed.append(label)
                        break
            elif hasattr(o, "Radius"):          # a fillet
                o.Radius = float(kv["radius"])
                changed.append(label)
    doc.recompute()
    return changed
