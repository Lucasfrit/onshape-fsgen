# ---- featuretree Fusion runtime ----
# Builds SPEC as native, named timeline features in a NEW Fusion design: sketches, extrudes
# (join / cut, with taper), revolves, fillets. Face and edge QUERIES from the IR are resolved here,
# against the live bodies — never stored ids — which is what keeps the timeline editable.
#
# Every blind length / depth / radius becomes a named user parameter ("ft_<feature>_<key>", its
# comment "featuretree:<feature>:<key>"), so edits made in Fusion's Parameters dialog read back into
# the IR by feature name (the featuretree_fusion_read script).
#
# After each feature the design's volume is compared with the build123d reference (EXPECTED), and
# at the end the script exports <name>.fusion.step and <name>.fusion.report.json next to itself.
# Verify with:  python cad_verify.py <name>.fusion.step <name>.ir.json --report <...report.json>
import json
import os
import traceback

import adsk.core
import adsk.fusion

CM = 0.1          # Fusion's internal length unit is the centimetre; the IR is in mm
# Fusion's taper-angle sign relative to the IR's (positive = the profile shrinks along the
# extrusion). NOT YET CONFIRMED against Fusion: a wrong sign shows up as a volume mismatch on the
# first tapered feature in the report — flip it to 1.0 if so.
TAPER_SIGN = -1.0


def _p3(w):
    return adsk.core.Point3D.create(w[0] * CM, w[1] * CM, w[2] * CM)


def _v3(w):
    return adsk.core.Vector3D.create(w[0], w[1], w[2])


class Builder:
    def __init__(self, design):
        self.design = design
        self.root = design.rootComponent
        self.sketches = {}       # IR sketch name -> {"sketch", "include", "frame", "z0", "side"}
        self.report = []

    # -- helpers -------------------------------------------------------------------------------
    def volume(self):
        if self.root.bRepBodies.count == 0:
            return None
        props = self.root.getPhysicalProperties(adsk.fusion.CalculationAccuracy.HighCalculationAccuracy)
        return props.volume * 1000.0            # cm^3 -> mm^3

    def zrange(self):
        zs = []
        for b in self.root.bRepBodies:
            bb = b.boundingBox
            zs += [bb.minPoint.z / CM, bb.maxPoint.z / CM]
        return (min(zs), max(zs)) if zs else (0.0, 0.0)

    def uparam(self, fname, key, value, unit="mm"):
        """A named user parameter carrying `value`, returned as a ValueInput expression."""
        ups = self.design.userParameters
        name = param_name(fname, key)
        base, k = name, 2
        while ups.itemByName(name) is not None:
            name, k = "%s_%d" % (base, k), k + 1
        ups.add(name, adsk.core.ValueInput.createByString("%r %s" % (float(value), unit)), unit,
                "featuretree:%s:%s" % (fname, key))
        return adsk.core.ValueInput.createByString(name)

    def draw(self, sk, fr, loops):
        """Draw loops (in frame fr) into sketch sk; return {entityToken: include} for every curve,
        so profiles can be chosen by which IR loop bounds them, not by geometry guessing."""
        tokens = {}
        curves = sk.sketchCurves
        sk.isComputeDeferred = True
        for loop in loops:
            made = []
            segs = loop["segments"]
            if segs[0][0] == "circle":
                _, c, r = segs[0]
                made.append(curves.sketchCircles.addByCenterRadius(
                    sk.modelToSketchSpace(_p3(to_world(fr, c[0], c[1]))), r * CM))
            else:
                first = prev = None
                for i, s in enumerate(segs):
                    last = i == len(segs) - 1
                    a = prev if prev is not None else sk.modelToSketchSpace(_p3(to_world(fr, *s[1])))
                    b = first if (last and first is not None) else \
                        sk.modelToSketchSpace(_p3(to_world(fr, *s[-1])))
                    if s[0] == "line":
                        ent = curves.sketchLines.addByTwoPoints(a, b)
                    else:
                        m = sk.modelToSketchSpace(_p3(to_world(fr, *s[2])))
                        try:
                            ent = curves.sketchArcs.addByThreePoints(a, m, b)
                        except Exception:       # older API: Point3D ends only
                            ent = curves.sketchArcs.addByThreePoints(
                                a.geometry if hasattr(a, "geometry") else a, m,
                                b.geometry if hasattr(b, "geometry") else b)
                    if first is None:
                        first = ent.startSketchPoint
                    prev = ent.endSketchPoint
                    made.append(ent)
            for ent in made:
                tokens[ent.entityToken] = loop["include"]
        sk.isComputeDeferred = False
        return tokens

    def profiles(self, name):
        s = self.sketches[name]
        coll = adsk.core.ObjectCollection.create()
        for pr in s["sketch"].profiles:
            outer = [lp for lp in pr.profileLoops if lp.isOuter]
            if not outer:
                continue
            tok = outer[0].profileCurves.item(0).sketchEntity.entityToken
            if s["include"].get(tok):
                coll.add(pr)
        if coll.count == 0:
            raise RuntimeError("sketch '%s' has no material profile" % name)
        return coll

    def sketch_normal(self, sk):
        return sk.xDirection.crossProduct(sk.yDirection)

    def top_face(self, side):
        best = None
        for b in self.root.bRepBodies:
            for f in b.faces:
                g = f.geometry
                if g.objectType != adsk.core.Plane.classType() or abs(abs(g.normal.z) - 1.0) > 1e-3:
                    continue
                z = f.centroid.z
                if best is None or (z > best[0] if side == "top" else z < best[0]):
                    best = (z, f)
        if best is None:
            raise RuntimeError("no Z-normal planar face to attach to")
        return best[1], best[0] / CM

    def extrude(self, fname, profiles, sk, op, world_dir=None, length=None, key="length",
                symmetric=False, taper=0.0):
        ext = self.root.features.extrudeFeatures
        inp = ext.createInput(profiles, op)
        if length is None:                      # through all, both ways
            try:
                inp.setTwoSidesExtent(adsk.fusion.ThroughAllExtentDefinition.create(),
                                      adsk.fusion.ThroughAllExtentDefinition.create())
            except Exception:
                inp.setAllExtent(adsk.fusion.ExtentDirections.SymmetricExtentDirection)
        elif symmetric:
            inp.setSymmetricExtent(self.uparam(fname, key, length), True)
        else:
            pos = self.sketch_normal(sk).dotProduct(_v3(world_dir)) > 0
            d = adsk.fusion.ExtentDirections.PositiveExtentDirection if pos else \
                adsk.fusion.ExtentDirections.NegativeExtentDirection
            ang = adsk.core.ValueInput.createByString("%r deg" % (TAPER_SIGN * taper))
            inp.setOneSideExtent(adsk.fusion.DistanceExtentDefinition.create(
                self.uparam(fname, key, length)), d, ang)
        feat = ext.add(inp)
        feat.name = fname
        return feat

    def solid_op(self):
        return adsk.fusion.FeatureOperations.NewBodyFeatureOperation if self.root.bRepBodies.count == 0 \
            else adsk.fusion.FeatureOperations.JoinFeatureOperation

    def plane_for(self, fr, name):
        """A construction plane through frame fr: an offset principal plane when the normal is
        axis-aligned (the common case), else one through three sketch points."""
        o, x, y, n = fr
        planes = self.root.constructionPlanes
        axis = max(range(3), key=lambda i: abs(n[i]))
        if abs(abs(n[axis]) - 1.0) < 1e-9:
            base = [self.root.yZConstructionPlane, self.root.xZConstructionPlane,
                    self.root.xYConstructionPlane][axis]
            bn = base.geometry.normal
            off = (o[0] * bn.x + o[1] * bn.y + o[2] * bn.z) * CM
            inp = planes.createInput()
            inp.setByOffset(base, adsk.core.ValueInput.createByReal(off))
            pl = planes.add(inp)
            g = pl.geometry
            got = g.origin.x * bn.x + g.origin.y * bn.y + g.origin.z * bn.z
            if abs(got - off) > 1e-6:           # offset ran against the normal: rebuild
                pl.deleteMe()
                inp = planes.createInput()
                inp.setByOffset(base, adsk.core.ValueInput.createByReal(-off))
                pl = planes.add(inp)
        else:
            ref = self.root.sketches.add(self.root.xYConstructionPlane)
            ref.name = name + "_refs"
            pts = [ref.sketchPoints.add(ref.modelToSketchSpace(_p3(p)))
                   for p in (o, to_world(fr, 10.0, 0.0), to_world(fr, 0.0, 10.0))]
            ref.isVisible = False
            inp = planes.createInput()
            inp.setByThreePoints(*pts)
            pl = planes.add(inp)
        pl.name = name + "_plane"
        pl.isLightBulbOn = False
        return pl

    def placed_cut(self, fname, fr, loops, depth, taper=0.0, key="depth"):
        pl = self.plane_for(fr, fname)
        sk = self.root.sketches.add(pl)
        sk.name = fname + "_sketch"
        self.sketches[sk.name] = {"sketch": sk, "include": self.draw(sk, fr, loops)}
        return self.extrude(fname, self.profiles(sk.name), sk,
                            adsk.fusion.FeatureOperations.CutFeatureOperation,
                            world_dir=fr[3], length=depth, key=key, taper=taper)

    # -- features ------------------------------------------------------------------------------
    def f_sketch(self, f):
        on = f.get("on")
        side = None
        if on:
            side = on.get("side", "top")
            face, z0 = self.top_face(side)
            try:
                sk = self.root.sketches.addWithoutEdges(face)
            except Exception:
                sk = self.root.sketches.add(face)
            fr = offset_xy(z0)
        elif f.get("plane", "XY") == "XZ":
            sk, fr, z0 = self.root.sketches.add(self.root.xZConstructionPlane), XZ, 0.0
        else:
            sk, fr, z0 = self.root.sketches.add(self.root.xYConstructionPlane), XY, 0.0
        sk.name = f["name"]
        loops = sketch_loops(f.get("circles", []), f.get("rects", []), f.get("polys", []))
        self.sketches[f["name"]] = {"sketch": sk, "include": self.draw(sk, fr, loops),
                                    "z0": z0, "side": side}

    def f_pad(self, f):
        s = self.sketches[f["sketch"]]
        d = (0, 0, -1) if s["side"] == "bottom" else (0, 0, 1)
        self.extrude(f["name"], self.profiles(f["sketch"]), s["sketch"], self.solid_op(),
                     world_dir=d, length=f["length"], symmetric=f.get("symmetric", False),
                     taper=f.get("taper", 0.0))

    def f_pocket(self, f):
        s = self.sketches[f["sketch"]]
        cut = adsk.fusion.FeatureOperations.CutFeatureOperation
        if f["through"]:
            self.extrude(f["name"], self.profiles(f["sketch"]), s["sketch"], cut)
        else:
            d = (0, 0, -1) if s["z0"] >= self.zrange()[1] - 1e-6 else (0, 0, 1)
            self.extrude(f["name"], self.profiles(f["sketch"]), s["sketch"], cut, world_dir=d,
                         length=f["length"], taper=f.get("taper", 0.0))

    def f_fillet(self, f):
        top = self.zrange()[1]
        cands = []
        for b in self.root.bRepBodies:
            for e in b.edges:
                g = e.geometry
                if g.objectType in (adsk.core.Circle3D.classType(), adsk.core.Arc3D.classType()) \
                        and abs(g.center.z / CM - top) < 1e-5:
                    cands.append((g.radius, e))
        if f["select"].get("circles") == "top_outer":
            cands = sorted(cands, key=lambda t: -t[0])[:1]
        if not cands:
            raise RuntimeError("fillet selected no edges")
        edges = adsk.core.ObjectCollection.create()
        for _, e in cands:
            edges.add(e)
        fl = self.root.features.filletFeatures
        inp = fl.createInput()
        r = self.uparam(f["name"], "radius", f["radius"])
        try:
            inp.edgeSetInputs.addConstantRadiusEdgeSet(edges, r, True)
        except Exception:
            inp.addConstantRadiusEdgeSet(edges, r, True)
        fl.add(inp).name = f["name"]

    def f_revolve(self, f):
        rv = self.root.features.revolveFeatures
        inp = rv.createInput(self.profiles(f["sketch"]), self.root.zConstructionAxis, self.solid_op())
        inp.setAngleExtent(False, self.uparam(f["name"], "angle", f.get("angle", 360.0), "deg"))
        rv.add(inp).name = f["name"]

    def f_polar_pocket(self, f):
        for i, (fr, loops, depth) in enumerate(polar_cuts(f)):
            self.placed_cut("%s_%d" % (f["name"], i), fr, loops, depth, key="length")

    def f_prism_cut(self, f):
        fr = frame(f["origin"], f["normal"], f["xdir"])
        self.placed_cut(f["name"], fr, sketch_loops(polys=f["polys"]), f["depth"],
                        taper=f.get("taper", 0.0))

    def build(self, spec, expected):
        tl = self.design.timeline
        for i, f in enumerate(spec["features"]):
            row = {"name": f["name"], "kind": f["kind"]}
            start = tl.count
            try:
                getattr(self, "f_" + f["kind"])(f)
                row["ok"] = True
            except Exception as e:
                row["ok"], row["error"] = False, "%s: %s" % (type(e).__name__, e)
            if tl.count - start > 1:            # a placed cut / pattern: group its steps
                try:
                    tl.timelineGroups.add(start, tl.count - 1).name = f["name"]
                except Exception:
                    pass
            row["volume"] = self.volume()
            row["expected"] = expected[i] if i < len(expected) else None
            row["volume_ok"] = volume_check(row["volume"], row["expected"])
            self.report.append(row)
        return self.report


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        try:
            design.rootComponent.name = SPEC["name"]
        except Exception:                        # tied to the (unsaved) document name
            pass
        rows = Builder(design).build(SPEC, EXPECTED)
        here = os.path.dirname(os.path.abspath(__file__))
        step = os.path.join(here, SPEC["name"] + ".fusion.step")
        em = design.exportManager
        em.execute(em.createSTEPExportOptions(step, design.rootComponent))
        with open(os.path.join(here, SPEC["name"] + ".fusion.report.json"), "w") as fh:
            json.dump({"target": "fusion", "part": SPEC["name"], "features": rows,
                       "step": step}, fh, indent=1)
        failed = [r for r in rows if not r["ok"]]
        drift = [r for r in rows if r["ok"] and r["volume_ok"] is False]
        msg = "%s: %d features, %d failed, %d volume mismatches\nSTEP: %s" % (
            SPEC["name"], len(rows), len(failed), len(drift), step)
        for r in (failed + drift)[:8]:
            msg += "\n  %s (%s): %s" % (r["name"], r["kind"], r.get("error") or
                                        "volume %.1f vs %.1f" % (r["volume"] or 0, r["expected"]))
        ui.messageBox(msg, "featuretree")
    except Exception:
        ui.messageBox("featuretree failed:\n" + traceback.format_exc())
# ---- end Fusion runtime ----
