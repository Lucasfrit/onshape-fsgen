# ---- featuretree SolidWorks runtime ----
# Drives a running SolidWorks over COM (Windows, `pip install pywin32`) and builds SPEC as native,
# named FeatureManager features in a NEW part: sketches, Boss-Extrude / Cut-Extrude (with draft),
# Revolve, Fillet. Face and edge QUERIES from the IR are resolved against the live body here —
# never stored ids.
#
#   python <name>_solidworks.py            # build, then save <name>.SLDPRT + .step + report.json
#   python <name>_solidworks.py --dump     # read the ACTIVE part's feature dimensions back
#                                          #   -> <name>.solidworks.params.json (by feature name)
#
# Coordinates: the IR's world frame is used as-is (IR +Z = SolidWorks model +Z), so parts made
# Z-up appear lying on their back in SolidWorks' Y-up views; the geometry, the STEP and the IoU
# check are unaffected. The SolidWorks API works in metres and radians.
import json
import math
import os
import sys

import pythoncom
import win32com.client

M = 0.001                                  # mm -> m
NULL = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
# Draw sketch entities straight into the database (no snapping to nearby geometry). If
# SolidWorks refuses to find closed contours with this on, set it False.
ADD_TO_DB = True

END_BLIND, END_THROUGH_ALL, END_MID_PLANE = 0, 1, 6
REFPLANE_PERPENDICULAR, REFPLANE_COINCIDENT = 2, 4


def _darr(vals):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in vals])


class Builder:
    def __init__(self, app, doc):
        self.app, self.doc = app, doc
        self.ext, self.sm, self.fm = doc.Extension, doc.SketchManager, doc.FeatureManager
        self.mu = app.GetMathUtility()
        self.sketches = {}                 # IR name -> {"feature", "normal", "z0", "side"}
        self.report = []
        planes, feat = [], doc.FirstFeature()
        while feat is not None and len(planes) < 3:
            if feat.GetTypeName2() == "RefPlane":
                planes.append(feat)
            feat = feat.GetNextFeature()
        self.front, self.top, self.right = planes      # XY, XZ, YZ in the default template

    # -- helpers -------------------------------------------------------------------------------
    def volume(self):
        try:
            mp = self.ext.CreateMassProperty()
            return None if mp is None else mp.Volume * 1e9     # m^3 -> mm^3
        except Exception:
            return None

    def bodies(self):
        return list(self.doc.GetBodies2(0, True) or [])       # swSolidBody

    def zrange(self):
        zs = []
        for b in self.bodies():
            box = b.GetBodyBox()
            zs += [box[2] / M, box[5] / M]
        return (min(zs), max(zs)) if zs else (0.0, 0.0)

    def last_feature(self, name):
        feat = self.doc.FeatureByPositionReverse(0)
        if feat is None:
            raise RuntimeError("no feature was created")
        feat.Name = name
        return feat

    def _xform(self, xf, w):
        p = self.mu.CreatePoint(_darr(w)).MultiplyTransform(xf)
        return p.ArrayData

    def open_sketch(self):
        """Open a sketch on whatever is selected; return (sketch, world->sketch fn, normal)."""
        self.sm.InsertSketch(True)
        sk = self.doc.GetActiveSketch2()
        if sk is None:
            raise RuntimeError("could not open a sketch on the selection")
        xf = sk.ModelToSketchTransform
        inv = xf.Inverse()
        o = self._xform(inv, (0, 0, 0))
        z = self._xform(inv, (0, 0, 1))
        normal = (z[0] - o[0], z[1] - o[1], z[2] - o[2])
        return sk, (lambda w: self._xform(xf, (w[0] * M, w[1] * M, w[2] * M))), normal

    def draw(self, to_sk, fr, loops):
        self.sm.AddToDB = ADD_TO_DB
        for loop in loops:
            for s in loop["segments"]:
                if s[0] == "circle":
                    c = to_sk(to_world(fr, *s[1]))
                    self.sm.CreateCircleByRadius(c[0], c[1], 0.0, s[2] * M)
                elif s[0] == "line":
                    a, b = to_sk(to_world(fr, *s[1])), to_sk(to_world(fr, *s[2]))
                    self.sm.CreateLine(a[0], a[1], 0.0, b[0], b[1], 0.0)
                else:
                    a, m, b = (to_sk(to_world(fr, *p)) for p in s[1:])
                    self.sm.Create3PointArc(a[0], a[1], 0.0, b[0], b[1], 0.0, m[0], m[1], 0.0)
        self.sm.AddToDB = False

    def close_sketch(self, name, normal, z0=0.0, side=None):
        self.sm.InsertSketch(True)
        feat = self.last_feature(name)
        self.sketches[name] = {"feature": feat, "normal": normal, "z0": z0, "side": side}
        return feat

    def select_face(self, side):
        best = None
        for b in self.bodies():
            for f in b.GetFaces() or []:
                srf = f.GetSurface()
                if not srf.IsPlane():
                    continue
                pp = srf.PlaneParams            # (nx, ny, nz, px, py, pz)
                if abs(abs(pp[2]) - 1.0) > 1e-3:
                    continue
                z = pp[5]
                if best is None or (z > best[0] if side == "top" else z < best[0]):
                    best = (z, f)
        if best is None:
            raise RuntimeError("no Z-normal planar face to attach to")
        tri = best[1].GetTessTriangles(True)     # a point strictly inside the face
        p = [(tri[i] + tri[i + 3] + tri[i + 6]) / 3.0 for i in range(3)]
        if not self.ext.SelectByID2("", "FACE", p[0], p[1], p[2], False, 0, NULL, 0):
            raise RuntimeError("could not select the %s face" % side)
        return best[0] / M

    def flip_for(self, sketch_normal, world_dir):
        return sum(a * b for a, b in zip(sketch_normal, world_dir)) < 0

    def select_sketch(self, name):
        self.doc.ClearSelection2(True)
        self.sketches[name]["feature"].Select2(False, 0)

    def boss(self, fname, sname, world_dir, length, symmetric=False, taper=0.0):
        s = self.sketches[sname]
        self.select_sketch(sname)
        t1 = END_MID_PLANE if symmetric else END_BLIND
        feat = self.fm.FeatureExtrusion3(
            True, False, self.flip_for(s["normal"], world_dir), t1, 0, length * M, 0.0,
            bool(taper), False, taper < 0, False, math.radians(abs(taper)), 0.0,
            False, False, False, False, True, True, True, 0, 0.0, False)
        if feat is None:
            raise RuntimeError("FeatureExtrusion3 returned nothing")
        feat.Name = fname

    def cut(self, fname, sname, world_dir=None, length=None, taper=0.0):
        s = self.sketches[sname]
        self.select_sketch(sname)
        if length is None:                  # through all, both directions
            args = (False, False, False, END_THROUGH_ALL, END_THROUGH_ALL, 0.0, 0.0)
        else:
            args = (True, False, self.flip_for(s["normal"], world_dir), END_BLIND, 0, length * M, 0.0)
        feat = self.fm.FeatureCut4(*args, bool(taper), False, taper < 0, False,
                                   math.radians(abs(taper)), 0.0, False, False, False, False,
                                   False, True, True, True, True, False, 0, 0.0, False, False)
        if feat is None:
            raise RuntimeError("FeatureCut4 returned nothing")
        feat.Name = fname

    def plane_for(self, fr, name):
        """A reference plane through frame fr: perpendicular to a 3D-sketch line along the normal,
        coincident with its start point."""
        o, _, _, n = fr
        self.doc.ClearSelection2(True)
        self.sm.Insert3DSketch(True)
        self.sm.AddToDB = True
        e = [o[i] + n[i] * 10.0 for i in range(3)]
        seg = self.sm.CreateLine(o[0] * M, o[1] * M, o[2] * M, e[0] * M, e[1] * M, e[2] * M)
        self.sm.AddToDB = False
        self.sm.Insert3DSketch(True)
        ref = self.last_feature(name + "_axis")
        sel = self.doc.SelectionManager
        d0, d1 = sel.CreateSelectData(), sel.CreateSelectData()
        d0.Mark, d1.Mark = 0, 1
        seg.Select4(False, d0)
        seg.GetStartPoint2().Select4(True, d1)
        if self.fm.InsertRefPlane(REFPLANE_PERPENDICULAR, 0, REFPLANE_COINCIDENT, 0, 0, 0) is None:
            raise RuntimeError("InsertRefPlane failed")
        pl = self.last_feature(name + "_plane")
        ref.Select2(False, 0)
        self.doc.BlankSketch()
        pl.Select2(False, 0)
        self.doc.BlankRefGeom()
        return pl

    def placed_cut(self, fname, fr, loops, depth, taper=0.0):
        pl = self.plane_for(fr, fname)
        self.doc.ClearSelection2(True)
        pl.Select2(False, 0)
        _, to_sk, normal = self.open_sketch()
        self.draw(to_sk, fr, loops)
        self.close_sketch(fname + "_sketch", normal)
        self.cut(fname, fname + "_sketch", world_dir=fr[3], length=depth, taper=taper)

    # -- features ------------------------------------------------------------------------------
    def f_sketch(self, f):
        self.doc.ClearSelection2(True)
        on, side = f.get("on"), None
        if on:
            side = on.get("side", "top")
            z0 = self.select_face(side)
            fr = offset_xy(z0)
        elif f.get("plane", "XY") == "XZ":
            self.top.Select2(False, 0)
            z0, fr = 0.0, XZ
        else:
            self.front.Select2(False, 0)
            z0, fr = 0.0, XY
        sk, to_sk, normal = self.open_sketch()
        self.draw(to_sk, fr, sketch_loops(f.get("circles", []), f.get("rects", []), f.get("polys", [])))
        if f.get("plane") == "XZ" and not on:           # the revolve axis: model Z, as a centreline
            a, b = to_sk((0, 0, -1000.0)), to_sk((0, 0, 1000.0))
            self.sm.CreateCenterLine(a[0], a[1], 0.0, b[0], b[1], 0.0)
        self.close_sketch(f["name"], normal, z0, side)

    def f_pad(self, f):
        d = (0, 0, -1) if self.sketches[f["sketch"]]["side"] == "bottom" else (0, 0, 1)
        self.boss(f["name"], f["sketch"], d, f["length"], f.get("symmetric", False), f.get("taper", 0.0))

    def f_pocket(self, f):
        if f["through"]:
            self.cut(f["name"], f["sketch"])
        else:
            d = (0, 0, -1) if self.sketches[f["sketch"]]["z0"] >= self.zrange()[1] - 1e-6 else (0, 0, 1)
            self.cut(f["name"], f["sketch"], d, f["length"], f.get("taper", 0.0))

    def f_fillet(self, f):
        top = self.zrange()[1] * M
        cands = []
        for b in self.bodies():
            for e in b.GetEdges() or []:
                cu = e.GetCurve()
                if not cu.IsCircle():
                    continue
                cp = cu.CircleParams            # (cx, cy, cz, ax, ay, az, r)
                if abs(cp[2] - top) < 1e-9:
                    cands.append((cp[6], e, cu))
        if f["select"].get("circles") == "top_outer":
            cands = sorted(cands, key=lambda t: -t[0])[:1]
        if not cands:
            raise RuntimeError("fillet selected no edges")
        self.doc.ClearSelection2(True)
        for i, (_, e, cu) in enumerate(cands):
            prm = e.GetCurveParams2()           # (start xyz, end xyz, umin, umax, ...)
            p = cu.Evaluate2((prm[6] + prm[7]) / 2.0, 0)
            self.ext.SelectByID2("", "EDGE", p[0], p[1], p[2], i > 0, 1, NULL, 0)
        E = pythoncom.Empty
        feat = self.fm.FeatureFillet3(195, f["radius"] * M, 0.0, 0, 0, 0, 0, E, E, E, E, E, E, E)
        if feat is None:
            raise RuntimeError("FeatureFillet3 returned nothing")
        feat.Name = f["name"]

    def f_revolve(self, f):
        self.select_sketch(f["sketch"])
        ang = math.radians(f.get("angle", 360.0))
        feat = self.fm.FeatureRevolve2(True, True, False, False, False, False, END_BLIND, END_BLIND,
                                       ang, 0.0, False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True)
        if feat is None:
            raise RuntimeError("FeatureRevolve2 returned nothing")
        feat.Name = f["name"]

    def f_polar_pocket(self, f):
        for i, (fr, loops, depth) in enumerate(polar_cuts(f)):
            self.placed_cut("%s_%d" % (f["name"], i), fr, loops, depth)

    def f_prism_cut(self, f):
        self.placed_cut(f["name"], frame(f["origin"], f["normal"], f["xdir"]),
                        sketch_loops(polys=f["polys"]), f["depth"], f.get("taper", 0.0))

    def build(self, spec, expected):
        for i, f in enumerate(spec["features"]):
            row = {"name": f["name"], "kind": f["kind"]}
            try:
                getattr(self, "f_" + f["kind"])(f)
                row["ok"] = True
            except Exception as e:
                row["ok"], row["error"] = False, "%s: %s" % (type(e).__name__, e)
                try:
                    self.sm.InsertSketch(True) if self.doc.GetActiveSketch2() is not None else None
                except Exception:
                    pass
            row["volume"] = self.volume()
            row["expected"] = expected[i] if i < len(expected) else None
            row["volume_ok"] = volume_check(row["volume"], row["expected"])
            self.report.append(row)
            print("  %-24s %-12s %s" % (f["name"], f["kind"],
                                          "ok" if row["ok"] else "FAILED " + row["error"]))
        return self.report


def dump(app, here):
    """Read the active part's driving dimensions back, keyed by IR feature name."""
    doc = app.ActiveDoc
    keys = {"pad": "length", "pocket": "length", "fillet": "radius", "prism_cut": "depth",
            "revolve": "angle"}
    params = {}
    for f in SPEC["features"]:
        key = keys.get(f["kind"])
        if key is None or (f["kind"] == "pocket" and f.get("through")):
            continue
        p = doc.Parameter("D1@" + f["name"])
        if p is not None:
            v = p.SystemValue
            params[f["name"]] = {key: round(math.degrees(v) if key == "angle" else v / M, 6)}
    out = os.path.join(here, SPEC["name"] + ".solidworks.params.json")
    with open(out, "w") as fh:
        json.dump(params, fh, indent=1)
    print("wrote", out)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    app = win32com.client.Dispatch("SldWorks.Application")
    app.Visible = True
    if "--dump" in sys.argv:
        return dump(app, here)
    app.SetUserPreferenceToggle(10, False)      # swInputDimValOnCreate: no dimension pop-ups
    tmpl = app.GetUserPreferenceStringValue(8)  # swDefaultTemplatePart
    doc = app.NewDocument(tmpl, 0, 0, 0)
    if doc is None:
        raise SystemExit("could not create a part from the default template: %r" % tmpl)
    print("building %s (%d features)" % (SPEC["name"], len(SPEC["features"])))
    rows = Builder(app, doc).build(SPEC, EXPECTED)
    doc.EditRebuild3()
    base = os.path.join(here, SPEC["name"])
    doc.SaveAs3(base + ".SLDPRT", 0, 1)
    doc.SaveAs3(base + ".solidworks.step", 0, 3)   # silent | copy: the open doc stays the .SLDPRT
    with open(base + ".solidworks.report.json", "w") as fh:
        json.dump({"target": "solidworks", "part": SPEC["name"], "features": rows,
                   "step": base + ".solidworks.step"}, fh, indent=1)
    failed = [r for r in rows if not r["ok"]]
    drift = [r for r in rows if r["ok"] and r["volume_ok"] is False]
    print("%d features, %d failed, %d volume mismatches" % (len(rows), len(failed), len(drift)))
    print("verify: python cad_verify.py %s.solidworks.step %s.ir.json --report %s.solidworks.report.json"
          % ((SPEC["name"],) * 3))


if __name__ == "__main__":
    main()
# ---- end SolidWorks runtime ----
