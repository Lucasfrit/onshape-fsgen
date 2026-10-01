"""STEP -> editable Onshape feature tree, using featuretree's feature recognition (third_party/featuretree, MIT).

featuretree infers a neutral feature IR (sketch / pad / pocket / revolve / prism_cut ...) from a dumb
B-rep and self-verifies it by volume. This module translates that IR into an fsgen Part Studio script,
so `fsgen studio push` can turn any STEP (e.g. from Dropbox) into native, editable Onshape features.
The translation is checked offline with the local builder before anything is sent to Onshape.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import numpy as np

FT = Path(__file__).resolve().parent.parent / "third_party" / "featuretree"
HEADER = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n\n'
# default plane -> (sketch x, sketch y, normal) in world coordinates (Onshape conventions)
PLANES = {"Top": ((1, 0, 0), (0, 1, 0), (0, 0, 1)), "Front": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
          "Right": ((0, 1, 0), (0, 0, 1), (1, 0, 0))}


class RecognizeError(Exception):
    pass


def _featuretree():
    if str(FT) not in sys.path:
        sys.path.insert(0, str(FT))
    import build123d
    if not hasattr(build123d.Vector, "to_tuple"):  # featuretree targets an older build123d
        build123d.Vector.to_tuple = lambda self: (self.X, self.Y, self.Z)
    import b3d_emit
    import step_recognize
    return step_recognize, b3d_emit


def _n(v: float) -> str:
    v = float(v)
    return f"{v:.6g}"


def _vec2(p) -> str:
    return f"vector({_n(p[0])}, {_n(p[1])}) * millimeter"


def _name(raw: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9 ]+", " ", raw.replace("_", " ")).strip().capitalize() or "Feature"
    name, k = base, 2
    while name in used:
        name, k = f"{base} {k}", k + 1
    used.add(name)
    return name


def _poly_entities(var: str, ent: str, poly, to_sketch) -> list[str]:
    """Lines and arcs (DXF bulge) of a closed wire; `to_sketch` maps (u, v) -> sketch (x, y)."""
    pts = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0) for p in poly]
    if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    out = []
    for i, (x0, y0, b) in enumerate(pts):
        x1, y1, _ = pts[(i + 1) % len(pts)]
        a, c = to_sketch((x0, y0)), to_sketch((x1, y1))
        if abs(b) < 1e-12:
            out.append(f'skLineSegment({var}, "{ent}l{i}", {{ "start" : {_vec2(a)}, "end" : {_vec2(c)} }});')
            continue
        # bulge = tan(theta/4); sagitta = bulge * chord / 2, on the right of travel for positive bulge
        # in featuretree's convention (verified by volume round-trip against its own builder)
        L = math.hypot(x1 - x0, y1 - y0)
        s = -b * L / 2
        mx, my = (x0 + x1) / 2 + s * (y1 - y0) / L, (y0 + y1) / 2 - s * (x1 - x0) / L
        m = to_sketch((mx, my))
        out.append(f'skArc({var}, "{ent}a{i}", {{ "start" : {_vec2(a)}, "mid" : {_vec2(m)}, "end" : {_vec2(c)} }});')
    return out


def ir_to_script(spec: dict) -> str:
    """featuretree IR -> fsgen Part Studio script (native features)."""
    _, b3d = _featuretree()
    lines, used = [], set()
    sketches: dict[str, dict] = {}  # IR sketch name -> {name, plane, z0, side, has_holes}
    plane_feats: dict[float, str] = {}

    def top_bottom(k):
        part, _ = b3d.emit({"name": "prefix", "features": spec["features"][:k]})
        bb = part.bounding_box()
        return bb.max.Z, bb.min.Z

    def offset_plane(z0: float, base="Top") -> str:
        if abs(z0) < 1e-9:
            return f'qCreatedBy(makeId("{base}"), EntityType.FACE)'
        key = (base, round(z0, 6))
        if key not in plane_feats:
            pname = _name(f"{base} plane {_n(z0)}", used)
            plane_feats[key] = pname
            lines.append(f'    cPlane(context, id + "{pname}", {{ "entities" : qCreatedBy(makeId("{base}"), EntityType.FACE), '
                         f'"offset" : {_n(z0)} * millimeter }});')
        return f'qCreatedBy(id + "{plane_feats[key]}", EntityType.FACE)'

    first_solid = True
    for k, f in enumerate(spec["features"]):
        kind = f["kind"]
        if f.get("taper"):
            raise RecognizeError(f"{f['name']}: tapered (drafted) features are not translated yet")
        if kind == "sketch":
            on = f.get("on")
            if on:
                ztop, zbot = top_bottom(k)
                side = on.get("side", "top")
                z0 = ztop if side == "top" else zbot
                plane_q, plane_name = offset_plane(z0), "Top"
            elif f.get("plane", "XY") == "XZ":
                side, z0, plane_q, plane_name = None, 0.0, 'qCreatedBy(makeId("Front"), EntityType.FACE)', "Front"
            else:
                side, z0, plane_q, plane_name = None, 0.0, 'qCreatedBy(makeId("Top"), EntityType.FACE)', "Top"
            name = _name(f["name"] + " sketch", used)
            var = re.sub(r"\W", "", name.title()) or "sk"
            var = "sk" + var
            lines.append(f'    var {var} = newSketch(context, id + "{name}", {{ "sketchPlane" : {plane_q} }});')
            for i, (cx, cy, r) in enumerate(f.get("circles", [])):
                lines.append(f'    skCircle({var}, "c{i}", {{ "center" : {_vec2((cx, cy))}, "radius" : {_n(r)} * millimeter }});')
            for i, (w, h, cx, cy) in enumerate(f.get("rects", [])):
                lines.append(f'    skRectangle({var}, "r{i}", {{ "firstCorner" : {_vec2((cx - w / 2, cy - h / 2))}, '
                             f'"secondCorner" : {_vec2((cx + w / 2, cy + h / 2))} }});')
            for j, poly in enumerate(f.get("polys", [])):
                lines += ["    " + l for l in _poly_entities(var, f"p{j}", poly, lambda p: p)]
            if f.get("plane") == "XZ":
                lines.append(f'    skLineSegment({var}, "axis", {{ "start" : vector(0, 0) * millimeter, '
                             f'"end" : vector(0, 10) * millimeter, "construction" : true }});')
            lines.append(f"    skSolve({var});")
            sketches[f["name"]] = {"name": name, "side": side, "holes": len(f.get("polys", [])) > 1, "z0": z0}
        elif kind in ("pad", "pocket", "revolve"):
            sk = sketches[f["sketch"]]
            name = _name(f["name"], used)
            regions = f'qSketchRegion(id + "{sk["name"]}"{", true" if sk["holes"] else ""})'
            if kind == "revolve":
                ang = float(f.get("angle", 360))
                op = "NEW" if first_solid else "ADD"
                extra = ' "fullRevolve" : true' if abs(ang - 360) < 1e-9 else \
                    f' "fullRevolve" : false, "angle" : {_n(ang)} * degree'
                lines.append(f'    revolve(context, id + "{name}", {{ "entities" : {regions}, "axis" : '
                             f'sketchEntityQuery(id + "{sk["name"]}", EntityType.EDGE, "axis"), "operationType" : '
                             f'NewBodyOperationType.{op}, "defaultScope" : true,{extra} }});')
                first_solid = False
            elif kind == "pad":
                op = "NEW" if first_solid else "ADD"
                params = [f'"entities" : {regions}', f'"operationType" : NewBodyOperationType.{op}',
                          '"endBound" : BoundingType.BLIND', f'"depth" : {_n(f["length"])} * millimeter',
                          '"defaultScope" : true']
                if f.get("symmetric"):
                    params.append('"symmetric" : true')
                if sk["side"] == "bottom":
                    params.append('"oppositeDirection" : true')
                lines.append(f'    extrude(context, id + "{name}", {{ {", ".join(params)} }});')
                first_solid = False
            else:
                params = [f'"entities" : {regions}', '"operationType" : NewBodyOperationType.REMOVE', '"defaultScope" : true']
                if f.get("through", True):
                    params += ['"endBound" : BoundingType.THROUGH_ALL', '"hasSecondDirection" : true',
                               '"secondDirectionBound" : BoundingType.THROUGH_ALL']
                else:
                    ztop, _ = top_bottom(k)
                    down = sk["z0"] >= ztop - 1e-6
                    params += ['"endBound" : BoundingType.BLIND', f'"depth" : {_n(f["length"])} * millimeter']
                    if down:
                        params.append('"oppositeDirection" : true')
                lines.append(f'    extrude(context, id + "{name}", {{ {", ".join(params)} }});')
        elif kind == "prism_cut":
            nrm = np.array(f["normal"], float)
            xdir = np.array(f["xdir"], float)
            org = np.array(f["origin"], float)
            base = next((p for p, (_, _, n) in PLANES.items() if abs(abs(np.dot(n, nrm)) - 1) < 1e-6), None)
            if base is None:
                raise RecognizeError(f"{f['name']}: oblique prism cuts are not translated yet")
            sx, sy, sn = (np.array(v, float) for v in PLANES[base])
            off = float(np.dot(org, sn))
            plane_q = offset_plane(off, base)
            ydir = np.cross(nrm, xdir)

            def to_sketch(uv, org=org, xdir=xdir, ydir=ydir, sx=sx, sy=sy):
                w = org + uv[0] * xdir + uv[1] * ydir
                return (float(np.dot(w, sx)), float(np.dot(w, sy)))
            sname = _name(f["name"] + " sketch", used)
            var = "sk" + (re.sub(r"\W", "", sname.title()) or "p")
            lines.append(f'    var {var} = newSketch(context, id + "{sname}", {{ "sketchPlane" : {plane_q} }});')
            for j, poly in enumerate(f.get("polys", [])):
                lines += ["    " + l for l in _poly_entities(var, f"p{j}", poly, to_sketch)]
            lines.append(f"    skSolve({var});")
            name = _name(f["name"], used)
            regions = f'qSketchRegion(id + "{sname}"{", true" if len(f.get("polys", [])) > 1 else ""})'
            flip = ', "oppositeDirection" : true' if np.dot(sn, nrm) < 0 else ""
            lines.append(f'    extrude(context, id + "{name}", {{ "entities" : {regions}, "operationType" : '
                         f'NewBodyOperationType.REMOVE, "endBound" : BoundingType.BLIND, "depth" : {_n(f["depth"])} '
                         f'* millimeter, "defaultScope" : true{flip} }});')
        elif kind == "fillet":
            raise RecognizeError(f"{f['name']}: recognized fillets are not translated yet")
        else:
            raise RecognizeError(f"{f['name']}: featuretree feature '{kind}' is not translated yet")
    title = spec.get("name", "part")
    return (HEADER + f"// Recognized from a STEP file by featuretree (step_recognize) and translated by fsgen.\n"
            f"// Part: {title}\nexport function build(context is Context, id is Id)\n{{\n" + "\n".join(lines) + "\n}\n")


def recognize_step(path: Path) -> tuple[str, dict]:
    """STEP -> (Part Studio script, report). The report compares the STEP, featuretree's own rebuild
    and fsgen's local build of the translated script."""
    sr, _ = _featuretree()
    from build123d import import_step
    from .native.local import build_local
    from .native.script import trace

    spec, report = sr.recognize(str(path))
    script = ir_to_script(spec)
    original = float(import_step(str(path)).volume)
    tr = trace(script)
    if not tr.ok:
        raise RecognizeError(f"translated script does not trace: {tr.error}")
    loc = build_local(tr)
    if not loc.ok:
        raise RecognizeError(f"translated script does not build locally: {loc.error}")
    vol = loc.metrics()["volume_mm3"] or 0.0
    status = report.get("status") if isinstance(report, dict) else str(report)
    return script, {"featuretree": status, "step_volume_mm3": round(original, 3),
                    "script_volume_mm3": vol, "match": abs(vol - original) <= 1e-3 * max(original, 1.0),
                    "features": len(tr.features), "report": report}
