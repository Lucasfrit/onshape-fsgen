"""fc_build.py — emit a FreeCAD .FCStd feature tree from the IR.

RUNS UNDER freecadcmd (FreeCAD's Python 3.11), not the project interpreter.
Inputs via env: FC_IR (ir json path), FC_OUT (.FCStd path). (Paths can't be argv —
freecadcmd opens path args as documents.) Prints a line "RESULT:" + json (tree,
final volume, as-built params).

Each feature becomes a native PartDesign object with Label = the IR name, so a human
edit in FreeCAD can be matched back by name (see fc_read.py).
"""

import json
import math
import os
import sys

import FreeCAD as App
import Part

sys.path.insert(0, os.environ.get("FC_LIBDIR", os.path.dirname(os.path.abspath(__file__))))
import fc_common  # noqa: E402

# IR taper is build123d's convention (positive = the profile shrinks along the extrusion).
# PartDesign's TaperAngle sign is the opposite way round; see tests/test_taper.py, which builds
# the same tapered IR in both backends and requires equal volumes.
FC_TAPER_SIGN = -1.0
# prism_cut is a REVERSED Pocket, whose taper sign is pinned separately by the same parity test
FC_PRISM_TAPER_SIGN = -1.0


def _add_rect(sk, w, h, cx, cy):
    hw, hh = w / 2.0, h / 2.0
    pts = [App.Vector(cx - hw, cy - hh, 0), App.Vector(cx + hw, cy - hh, 0),
           App.Vector(cx + hw, cy + hh, 0), App.Vector(cx - hw, cy + hh, 0)]
    for i in range(4):
        sk.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]), False)


def _add_poly(sk, pts):
    """Closed wire from [(x, y[, bulge]), ...]. bulge = DXF arc factor tan(theta/4) for the segment
    to the NEXT vertex (0/absent = a line, sign = CCW+). Drops a duplicate closing vertex."""
    v = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0) for p in pts]
    if len(v) > 1 and abs(v[0][0] - v[-1][0]) < 1e-7 and abs(v[0][1] - v[-1][1]) < 1e-7:
        v = v[:-1]
    n = len(v)
    for i in range(n):
        x1, y1, b = v[i]
        x2, y2, _ = v[(i + 1) % n]
        p1, p2 = App.Vector(x1, y1, 0), App.Vector(x2, y2, 0)
        if abs(b) < 1e-4:                      # BULGE_EPS: a straight line (spec rule)
            sk.addGeometry(Part.LineSegment(p1, p2), False)
        else:                                     # 3-point arc: chord midpoint + perpendicular*sagitta
            chord = math.hypot(x2 - x1, y2 - y1)
            sag = b * chord / 2.0
            ux, uy = (x2 - x1) / chord, (y2 - y1) / chord   # left normal = (-uy, ux)
            mid = App.Vector((x1 + x2) / 2 - uy * sag, (y1 + y2) / 2 + ux * sag, 0)
            sk.addGeometry(Part.Arc(p1, mid, p2), False)


def _map_poly(poly, inv, zref):
    """Map a poly's (x, y[, bulge]) vertices from global coords into a sketch's local frame via the
    placement inverse `inv` (bulge is orientation-free, carried through)."""
    out = []
    for p in poly:
        lp = inv.multVec(App.Vector(float(p[0]), float(p[1]), zref))
        out.append([lp.x, lp.y] + ([float(p[2])] if len(p) > 2 else []))
    return out


def build(spec, out_path):
    doc = App.newDocument(spec["name"])
    body = doc.addObject("PartDesign::Body", "Body")
    # A PartDesign Body is one solid by default, so a sketch of several disjoint outlines (two feet,
    # a row of lugs) pads only ONE of them, silently halving the part. FreeCAD 1.0 lifts that with
    # AllowCompound; the IR's region semantics (and build123d's) are the union of all outlines.
    if hasattr(body, "AllowCompound"):
        body.AllowCompound = True
    sketches = {}
    tip = None      # last solid-producing feature (fillet bases reference it)
    volumes = []    # tip volume after each IR feature, for localising a disagreement

    for f in spec["features"]:
        kind = f["kind"]
        if kind == "sketch":
            sk = body.newObject("Sketcher::SketchObject", f["name"])
            sk.Label = f["name"]
            on = f.get("on")
            if on:                                  # attach to a face chosen by query
                face = fc_common.resolve_face(tip.Shape, on.get("side", "top"))
                sk.AttachmentSupport = [(tip, [face])]
                sk.MapMode = "FlatFace"
                doc.recompute()                     # so sk.Placement is resolved
                zref = max(v.Z for v in tip.Shape.Vertexes) if on.get("side", "top") == "top" \
                    else min(v.Z for v in tip.Shape.Vertexes)
                inv = sk.Placement.inverse()        # global -> sketch-local coords
                for (cx, cy, r) in f["circles"]:
                    lp = inv.multVec(App.Vector(cx, cy, zref))
                    sk.addGeometry(Part.Circle(App.Vector(lp.x, lp.y, 0), App.Vector(0, 0, 1), r), False)
                for (w, h, cx, cy) in f.get("rects", []):
                    lp = inv.multVec(App.Vector(cx, cy, zref))       # top/bottom face -> axis-aligned
                    _add_rect(sk, w, h, lp.x, lp.y)
                for poly in f.get("polys", []):
                    _add_poly(sk, _map_poly(poly, inv, zref))
            else:
                plane = f.get("plane", "XY")
                if plane == "XZ":                     # revolve profile: local (u,v) -> global (x=u, z=v)
                    sk.Placement = App.Placement(App.Vector(0, 0, 0),
                                                 App.Rotation(App.Vector(1, 0, 0), 90))
                elif plane != "XY":
                    raise ValueError("unattached sketches must be on XY or XZ (v0)")
                for (cx, cy, r) in f["circles"]:
                    sk.addGeometry(Part.Circle(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), r), False)
                for (w, h, cx, cy) in f["rects"]:
                    _add_rect(sk, w, h, cx, cy)
                for poly in f.get("polys", []):
                    _add_poly(sk, poly)
            sketches[f["name"]] = sk
        elif kind == "pad":
            p = body.newObject("PartDesign::Pad", f["name"])
            p.Label = f["name"]
            p.Profile = sketches[f["sketch"]]
            p.Length = f["length"]
            p.Midplane = bool(f["symmetric"])
            if f.get("taper"):
                p.TaperAngle = FC_TAPER_SIGN * f["taper"]
            tip = p
        elif kind == "pocket":
            p = body.newObject("PartDesign::Pocket", f["name"])
            p.Label = f["name"]
            p.Profile = sketches[f["sketch"]]
            if f["through"]:
                p.Type = "ThroughAll"
                p.Midplane = True            # cut both ways -> robust without a face attach
            else:
                p.Length = f["length"]
                if f.get("taper"):
                    p.TaperAngle = FC_TAPER_SIGN * f["taper"]
            tip = p
        elif kind == "fillet":
            fl = body.newObject("PartDesign::Fillet", f["name"])
            fl.Label = f["name"]
            edges = fc_common.resolve_edges(tip.Shape, f["select"])  # QUERY -> live EdgeN
            if not edges:
                raise ValueError(f"fillet '{f['name']}' selected no edges")
            fl.Base = (tip, edges)
            fl.Radius = f["radius"]
            tip = fl
        elif kind == "revolve":
            rev = body.newObject("PartDesign::Revolution", f["name"])
            rev.Label = f["name"]
            rev.Profile = sketches[f["sketch"]]
            rev.ReferenceAxis = (sketches[f["sketch"]], ["V_Axis"])   # the XZ sketch's V axis = global Z
            rev.Angle = f.get("angle", 360.0)
            rev.Midplane = False
            tip = rev
        elif kind == "polar_pocket":
            n, r, L = int(f["count"]), f["radius"], f["length"]
            mr, zc, phase = f["mount_r"], f.get("z", 0.0), f.get("phase", 0.0)
            for i in range(n):
                a = phase + 360.0 * i / n
                cyl = body.newObject("PartDesign::SubtractiveCylinder", f"{f['name']}_{i}")
                cyl.Label = f"{f['name']}_{i}"
                cyl.Radius = r
                cyl.Height = L
                # cylinder axis (local +Z) -> tangent at azimuth a; center it on the roller station
                rot = App.Rotation(App.Vector(0, 0, 1), a).multiply(App.Rotation(App.Vector(1, 0, 0), 90))
                axis = rot.multVec(App.Vector(0, 0, 1))
                ctr = App.Vector(mr * math.cos(math.radians(a)), mr * math.sin(math.radians(a)), zc)
                cyl.Placement = App.Placement(ctr - axis * (L / 2.0), rot)
                tip = cyl
        elif kind == "prism_cut":
            # A placed profile-along-an-axis cut: a datum plane at {origin, xdir, normal}, a sketch of
            # the polys on it, and a Pocket of `depth` along +normal (Reversed, since a PartDesign
            # Pocket cuts opposite the sketch normal by default). Mirrors b3d_emit's prism_cut.
            ox = App.Vector(*f["origin"])
            zx = App.Vector(*f["normal"]); zx.normalize()
            xx = App.Vector(*f["xdir"]); xx.normalize()
            yx = zx.cross(xx)
            plc = App.Placement(App.Matrix(xx.x, yx.x, zx.x, ox.x,
                                           xx.y, yx.y, zx.y, ox.y,
                                           xx.z, yx.z, zx.z, ox.z,
                                           0, 0, 0, 1))
            dp = body.newObject("PartDesign::Plane", f["name"] + "_pl")
            dp.MapMode = "Deactivated"
            dp.Placement = plc
            doc.recompute()
            sk = body.newObject("Sketcher::SketchObject", f["name"] + "_sk")
            sk.AttachmentSupport = [(dp, "")]
            sk.MapMode = "FlatFace"
            doc.recompute()
            for poly in f["polys"]:                 # polys are already in the plane's local (u, v) frame
                _add_poly(sk, poly)
            p = body.newObject("PartDesign::Pocket", f["name"])
            p.Label = f["name"]
            p.Profile = sk
            p.Length = f["depth"]
            p.Reversed = True                       # cut along +normal (the datum's Z)
            if f.get("taper"):
                p.TaperAngle = FC_PRISM_TAPER_SIGN * f["taper"]
            tip = p
        else:
            raise ValueError(f"unknown feature kind: {kind}")
        doc.recompute()
        try:                                        # the per-feature trace other backends report
            volumes.append(round(float(tip.Shape.Volume), 3) if tip is not None and not tip.Shape.isNull() else None)
        except Exception:
            volumes.append(None)

    doc.recompute()
    # freecadcmd writes NO GuiDocument.xml, and the GUI stores per-object display state THERE (not
    # in Document.xml's App Visibility) — so without it every object opens hidden. Set the App
    # Visibility (show Body + final tip solid, hide sketches/intermediates) AND inject a matching
    # GuiDocument.xml so the GUI actually honours it.
    visible = {body.Name, getattr(tip, "Name", None)}
    for o in doc.Objects:
        if hasattr(o, "Visibility"):
            o.Visibility = o.Name in visible
    doc.saveAs(out_path)
    _write_gui_document(out_path, [o.Name for o in doc.Objects], visible)
    stl = out_path[:-6] + ".stl" if out_path.endswith(".FCStd") else out_path + ".stl"
    body.Shape.exportStl(stl)
    body.Shape.exportStep(stl[:-4] + ".step")     # for the IoU gate against the reference
    return dict(fc_common.result(doc), volumes=volumes)


def _write_gui_document(out_path, names, visible):
    """Inject a GuiDocument.xml (the file the FreeCAD GUI reads for per-object display state) into
    the .FCStd zip, so freecadcmd output doesn't open all-hidden. Sets each ViewProvider Visibility."""
    import zipfile
    vps = []
    for name in names:
        val = "true" if name in visible else "false"
        vps.append('<ViewProvider name="%s" expanded="0">'
                   '<Properties Count="1" TransientCount="0">'
                   '<Property name="Visibility" type="App::PropertyBool" status="1">'
                   '<Bool value="%s"/></Property></Properties></ViewProvider>' % (name, val))
    gui = ("<?xml version='1.0' encoding='utf-8'?>\n<Document SchemaVersion=\"1\">\n"
           '<ViewProviderData Count="%d">\n%s\n</ViewProviderData>\n</Document>\n'
           % (len(vps), "\n".join(vps)))
    with zipfile.ZipFile(out_path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("GuiDocument.xml", gui)


# freecadcmd execs this file but not as __main__, so run at top level.
_spec = json.load(open(os.environ["FC_IR"]))
_result = build(_spec, os.environ["FC_OUT"])
print("RESULT:" + json.dumps(_result))
