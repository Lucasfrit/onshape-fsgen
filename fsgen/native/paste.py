"""Paste export: a Part Studio script -> one parametric custom feature (Feature Studio code).

Pasting code into Onshape's UI costs no API calls. Part Studio code cannot be pasted, but a Feature Studio
can, so this writes the part as a custom feature built from low-level operations (opExtrude, opRevolve,
opBoolean, opFillet, ...: the layer the local engine reproduces and the approach Onshape experts recommend
for patterns inside custom features). Variables become the feature's parameters; derived variables become
computed values; feature patterns become loops driven by the parameters.

The result is verified offline: the generated custom feature is run by the FS-Lite engine and must give the
same volume as the local build of the native script.
"""
from __future__ import annotations

import math
import re

import numpy as np

from ..fslite.values import FSError, Q, Vec, expr_of, has_expr, mag
from .local import LocalBuilder
from .script import FeatureRec, QText, TraceResult

HEADER = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n\n'
HELPERS = '''// Midpoint of an arc from its endpoints; k = tan(sweep / 4) keeps the arc's sweep when parameters change.
function arcMid(a is Vector, b is Vector, k is number) returns Vector
{
    var d = b - a;
    return (a + b) / 2 + vector(d[1], -d[0]) * (k / 2);
}

'''
UNITS = {"mm": "millimeter", "cm": "centimeter", "m": "meter", "in": "inch", "deg": "degree"}


class PasteError(Exception):
    pass


def _ident(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", " ", name).title().replace(" ", "")
    return s[:1].lower() + s[1:] if s else "f"


def _num(v: float) -> str:
    return f"{float(v):.10g}"


class PasteWriter:
    def __init__(self, tr: TraceResult, title: str):
        self.tr, self.title = tr, title
        self.lb = LocalBuilder()
        self.loc = self.lb.build(tr)
        if not self.loc.ok:
            raise PasteError(f"script does not build locally: {self.loc.error}")
        self.varmap: dict[str, str] = {}
        self.ids = {f.name: _ident(f.name) for f in tr.features}
        self.recs = {f.name: f for f in tr.features}
        self.plane_fs: dict[str, tuple[str, tuple, tuple]] = {
            "Top": ("vector(0, 0, 0) * millimeter", (0, 0, 1), (1, 0, 0)),
            "Front": ("vector(0, 0, 0) * millimeter", (0, -1, 0), (1, 0, 0)),
            "Right": ("vector(0, 0, 0) * millimeter", (1, 0, 0), (0, 1, 0))}
        self.sketch_plane: dict[str, str] = {}  # sketch name -> plane name
        self.body: list[str] = []
        self.sketches: list[str] = []  # construction bodies (sketches, helices) deleted at the end

    # ---------------------------------------------------------------- expressions
    def expr(self, onshape_expr: str) -> str:
        """Onshape expression ("#w / 2 + 5 mm") -> FeatureScript ("definition.w / 2 + (5 * millimeter)")."""
        e = re.sub(r"#(\w+)", lambda m: self.varmap.get(m.group(1), f"__unknown_{m.group(1)}"), onshape_expr)
        e = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(mm|cm|m|in|deg)\b",
                   lambda m: f"({m.group(1)} * {UNITS[m.group(2)]})", e)
        if "__unknown_" in e:
            raise PasteError(f"expression {onshape_expr!r} uses an unknown variable")
        return e

    def val(self, v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (Q, int, float)):
            return self.expr(expr_of(v))
        raise PasteError(f"cannot write value {v!r}")

    def point2(self, v: Vec) -> str:
        if v.exprs:
            return "vector(" + ", ".join(self.val(c) for c in v.items()) + ")"
        return f"vector({_num(v.a[0] * 1000)}, {_num(v.a[1] * 1000)}) * millimeter"

    def query(self, qt: QText) -> str:
        text = qt.text
        for name in qt.refs:
            text = text.replace(f'makeId("@@{name}@@")', f'id + "{self.ids[name]}"')
        return text

    # ---------------------------------------------------------------- parameters
    def parameters(self) -> tuple[list[str], list[str]]:
        pre, derived = [], []
        for v in self.tr.variable_list:
            name, e, t = v["name"], v["expression"], v["type"]
            if "#" in e:
                local = f"v{name[:1].upper()}{name[1:]}"
                derived.append(f"    const {local} = {self.expr(e)};")
                self.varmap[name] = local
                continue
            self.varmap[name] = f"definition.{name}"
            label = re.sub(r"(?<!^)([A-Z])", r" \1", name).capitalize()
            num = float(re.match(r"[-+0-9.eE]+", e).group(0))
            if t == "LENGTH":
                spec = f"{{ (millimeter) : [{_num(min(0, num))}, {_num(num)}, {_num(max(10000, num))}] }} as LengthBoundSpec"
                pre.append(f'        annotation {{ "Name" : "{label}" }}\n        isLength(definition.{name}, {spec});')
            elif t == "ANGLE":
                spec = f"{{ (degree) : [-360, {_num(num)}, 360] }} as AngleBoundSpec"
                pre.append(f'        annotation {{ "Name" : "{label}" }}\n        isAngle(definition.{name}, {spec});')
            elif float(num).is_integer():
                spec = f"{{ (unitless) : [0, {int(num)}, 10000] }} as IntegerBoundSpec"
                pre.append(f'        annotation {{ "Name" : "{label}" }}\n        isInteger(definition.{name}, {spec});')
            else:
                spec = f"{{ (unitless) : [-1e6, {_num(num)}, 1e6] }} as RealBoundSpec"
                pre.append(f'        annotation {{ "Name" : "{label}" }}\n        isReal(definition.{name}, {spec});')
        return pre, derived

    # ---------------------------------------------------------------- geometry helpers
    def plane_name_of(self, qt: QText) -> str:
        t = qt.text
        for p in ("Top", "Front", "Right"):
            if t == f'qCreatedBy(makeId("{p}"), EntityType.FACE)':
                return p
        m = re.fullmatch(r'qCreatedBy\(makeId\("@@(.+)@@"\), EntityType\.FACE\)', t)
        if m and m.group(1) in self.plane_fs:
            return m.group(1)
        raise PasteError(f"plane {t} is not a default or offset plane (sketches on part faces are not supported "
                         "in the paste export; use cPlane)")

    def plane_expr(self, pname: str) -> str:
        o, n, x = self.plane_fs[pname]
        return f"plane({o}, vector({', '.join(_num(c) for c in n)}), vector({', '.join(_num(c) for c in x)}))"

    def sketch_of(self, qt: QText) -> str:
        names = [r for r in qt.refs if self.recs[r].sketch is not None]
        if not names:
            raise PasteError(f"{qt.text}: regions must come from a sketch")
        return names[0]

    @staticmethod
    def vec3(a) -> str:
        return f"vector({', '.join(_num(c) for c in a)})"

    def line_expr(self, line) -> str:
        return f"line({self.vec3(np.asarray(line.origin) * 1000)} * millimeter, {self.vec3(line.direction)})"

    # ---------------------------------------------------------------- features
    def emit(self, line: str = ""):
        self.body.append("    " + line if line else "")

    def f_newSketch(self, rec: FeatureRec):
        sk = rec.sketch
        pname = self.plane_name_of(sk.plane)
        self.sketch_plane[rec.name] = pname
        var = self.ids[rec.name] + "Sk"  # not "sk" + Name: a sketch named "Circle" would shadow skCircle()
        self.emit(f'var {var} = newSketchOnPlane(context, id + "{self.ids[rec.name]}", '
                  f'{{ "sketchPlane" : {self.plane_expr(pname)} }});')
        for e in sk.entities:
            g = e.geom
            ename = re.sub(r"\W", "_", e.name)
            constr = ', "construction" : true' if e.construction else ""
            if e.kind == "line":
                self.emit(f'skLineSegment({var}, "{ename}", {{ "start" : {self.point2(g["p0"])}, '
                          f'"end" : {self.point2(g["p1"])}{constr} }});')
            elif e.kind == "circle":
                self.emit(f'skCircle({var}, "{ename}", {{ "center" : {self.point2(g["c"])}, '
                          f'"radius" : {self.val(g["r"])}{constr} }});')
            else:
                a, b = g["p0"], g["p1"]
                tm = (g["t0"] + g["t1"]) / 2
                mid = np.array([g["cx"] + g["r"] * math.cos(tm), g["cy"] + g["r"] * math.sin(tm)])
                d = b.a - a.a
                k = float(np.dot(mid - (a.a + b.a) / 2, np.array([d[1], -d[0]])) / (np.dot(d, d) / 2))
                self.emit(f'skArc({var}, "{ename}", {{ "start" : {self.point2(a)}, "mid" : arcMid({self.point2(a)}, '
                          f'{self.point2(b)}, {_num(k)}), "end" : {self.point2(b)}{constr} }});')
        self.emit(f"skSolve({var});")
        self.sketches.append(self.ids[rec.name])

    def f_cPlane(self, rec: FeatureRec):
        p = rec.params
        if p.get("cplaneType") is not None and p["cplaneType"].name != "OFFSET":
            raise PasteError(f"plane \"{rec.name}\": only offset planes are supported")
        base = self.plane_name_of(p["entities"])
        o, n, x = self.plane_fs[base]
        off = self.val(p.get("offset", Q(0.025, (1, 0))))
        sign = "-" if p.get("oppositeDirection") else ""
        self.plane_fs[rec.name] = (f"{o} + {sign}({off}) * {self.vec3(n)}", n, x)

    def tool(self, rec: FeatureRec, tid: str):
        """Emit the tool body of an extrude/revolve/loft feature under id expression `tid`."""
        p = rec.params
        if rec.feature_type == "extrude":
            sk = self.sketch_of(p["entities"])
            n = np.array(self.plane_fs[self.sketch_plane[sk]][1], float)
            if p.get("oppositeDirection"):
                n = -n
            bound = p["endBound"].name if p.get("endBound") is not None else "BLIND"
            if bound not in ("BLIND", "THROUGH_ALL"):
                raise PasteError(f"\"{rec.name}\": bound {bound} not supported in the paste export")
            depth = self.val(p.get("depth", Q(0.025, (1, 0))))
            fields = [f'"entities" : {self.query(p["entities"])}', f'"direction" : {self.vec3(n)}',
                      f'"endBound" : BoundingType.{bound}']
            if p.get("symmetric"):
                fields += [f'"endDepth" : ({depth}) / 2', '"startBound" : BoundingType.BLIND',
                           f'"startDepth" : ({depth}) / 2']
            else:
                if bound == "BLIND":
                    fields.append(f'"endDepth" : {depth}')
                if p.get("hasSecondDirection"):
                    b2 = p["secondDirectionBound"].name if p.get("secondDirectionBound") is not None else "BLIND"
                    fields.append(f'"startBound" : BoundingType.{b2}')
                    if b2 == "BLIND":
                        fields.append(f'"startDepth" : {self.val(p.get("secondDirectionDepth", Q(0.025, (1, 0))))}')
            self.emit(f"opExtrude(context, {tid}, {{ {', '.join(fields)} }});")
        elif rec.feature_type == "revolve":
            ax = self.lb.axis_of(p["axis"], rec.name)
            d = -ax.direction if p.get("oppositeDirection") else ax.direction
            from ..fslite.values import Line
            ang = "360 * degree" if p.get("fullRevolve", True) else self.val(p["angle"])
            self.emit(f'opRevolve(context, {tid}, {{ "entities" : {self.query(p["entities"])}, '
                      f'"axis" : {self.line_expr(Line(ax.origin, d))}, "angleForward" : {ang} }});')
        elif rec.feature_type == "loft":
            profs = ", ".join(self.query(i["sheetProfileEntities"]) for i in p.get("sheetProfilesArray") or [])
            self.emit(f'opLoft(context, {tid}, {{ "profileSubqueries" : [{profs}], "bodyType" : ToolBodyType.SOLID }});')
        elif rec.feature_type == "sweep":
            if p.get("bodyType") is not None and p["bodyType"].name != "SOLID":
                raise PasteError(f"\"{rec.name}\": only solid sweeps are supported in the paste export")
            if p.get("profileControl") is not None and p["profileControl"].name != "NONE":
                raise PasteError(f"\"{rec.name}\": sweep profile control is not supported in the paste export")
            for flag in ("hasTwist", "hasScale", "pathOppositeDirection", "extendToFullPath"):
                if p.get(flag):
                    raise PasteError(f"\"{rec.name}\": {flag} is not supported in the paste export")
            self.emit(f'opSweep(context, {tid}, {{ "profiles" : {self.query(p["profiles"])}, '
                      f'"path" : {self.query(p["path"])} }});')
        else:
            raise PasteError(f"\"{rec.name}\": {rec.feature_type} cannot be used as a tool")

    def boolean(self, rec: FeatureRec, tid: str, bid: str):
        p = rec.params
        op = p["operationType"].name if p.get("operationType") is not None else "NEW"
        if op == "NEW":
            return
        tool_q = f"qCreatedBy({tid}, EntityType.BODY)"
        scope = "qAllModifiableSolidBodies()" if p.get("defaultScope") else (
            self.query(p["booleanScope"]) if p.get("booleanScope") is not None else None)
        if scope is None:
            self.emit(f"// {rec.name}: empty boolean scope (does nothing in Onshape either)")
            self.emit(f'opDeleteBodies(context, {bid}, {{ "entities" : {tool_q} }});')
            return
        scope = f"qSubtraction({scope}, {tool_q})"
        if op == "ADD":  # existing part first, so the merged part keeps its identity
            self.emit(f'opBoolean(context, {bid}, {{ "tools" : qUnion([{scope}, {tool_q}]), '
                      f'"operationType" : BooleanOperationType.UNION }});')
        elif op == "REMOVE":
            self.emit(f'opBoolean(context, {bid}, {{ "tools" : {tool_q}, "targets" : {scope}, '
                      f'"operationType" : BooleanOperationType.SUBTRACTION }});')
        else:
            self.emit(f'opBoolean(context, {bid}, {{ "tools" : qUnion([{scope}, {tool_q}]), '
                      f'"operationType" : BooleanOperationType.INTERSECTION }});')

    def f_extrude(self, rec):
        i = self.ids[rec.name]
        self.tool(rec, f'id + "{i}" + "tool"')
        self.boolean(rec, f'id + "{i}" + "tool"', f'id + "{i}" + "bool"')

    f_revolve = f_extrude
    f_loft = f_extrude
    f_sweep = f_extrude

    def f_helix(self, rec):
        """opHelix with the Helix feature's axis/start convention (see LocalBuilder.helix_def), read from the
        geometry at run time so the helix follows the parameters."""
        p = rec.params
        i = self.ids[rec.name]
        v = "h" + i[:1].upper() + i[1:]
        axis_type = p["axisType"].name if p.get("axisType") is not None else "SURFACE"
        for key, ok in (("startType", "START_ANGLE"), ("endType", "HEIGHT")):
            if p.get(key) is not None and p[key].name != ok:
                raise PasteError(f"\"{rec.name}\": helix {key} {p[key].name} is not supported in the paste export")
        if axis_type == "CIRCLE":
            self.emit(f'const {v}Circle = evCurveDefinition(context, {{ "edge" : {self.query(p["edge"])} }});')
            origin, z, x = (f"{v}Circle.coordSystem.{k}" for k in ("origin", "zAxis", "xAxis"))
            r = f"{v}Circle.radius"
        elif axis_type == "AXIS":
            self.emit(f'const {v}Axis = evAxis(context, {{ "axis" : {self.query(p["axis"])} }});')
            origin, z, x = f"{v}Axis.origin", f"{v}Axis.direction", f"perpendicularVector({v}Axis.direction)"
            r = self.val(p.get("startRadius", Q(0.025, (1, 0))))
        else:
            raise PasteError(f"\"{rec.name}\": helix on a cylinder face (axisType SURFACE) is not supported; "
                             "use axisType CIRCLE with a sketch circle")
        if p.get("oppositeDirection"):
            z = f"-{z}"
        start = f"{origin} + ({r}) * {x}"
        if p.get("startAngle") is not None and mag(p["startAngle"]) != 0:
            a = self.val(p["startAngle"])
            start = f"{origin} + ({r}) * (cos({a}) * {x} + sin({a}) * cross({z}, {x}))"
        path = p["pathType"].name if p.get("pathType") is not None else "TURNS"
        height = self.val(p.get("height", Q(0.025, (1, 0))))
        turns = self.val(p.get("revolutions", 4))
        pitch = self.val(p.get("helicalPitch", Q(0.025, (1, 0))))
        if path == "TURNS":
            pitch = f"({height}) / ({turns})"
        elif path == "PITCH":
            turns = f"({height}) / ({pitch})"
        cw = "false" if p.get("handedness") is not None and p["handedness"].name == "CCW" else "true"
        self.emit(f'opHelix(context, id + "{i}", {{ "direction" : {z}, "axisStart" : {origin}, '
                  f'"startPoint" : {start}, "interval" : [0, {turns}], "clockwise" : {cw}, '
                  f'"helicalPitch" : {pitch}, "spiralPitch" : 0 * meter }});')
        self.sketches.append(i)

    def f_fillet(self, rec):
        self.emit(f'opFillet(context, id + "{self.ids[rec.name]}", {{ "entities" : {self.query(rec.params["entities"])}, '
                  f'"radius" : {self.val(rec.params.get("radius", Q(0.005, (1, 0))))} }});')

    def f_chamfer(self, rec):
        p = rec.params
        if p.get("chamferType") is not None and p["chamferType"].name != "EQUAL_OFFSETS":
            raise PasteError(f"\"{rec.name}\": only equal-offset chamfers are supported")
        self.emit(f'opChamfer(context, id + "{self.ids[rec.name]}", {{ "entities" : {self.query(p["entities"])}, '
                  f'"chamferType" : ChamferType.EQUAL_OFFSETS, "width" : {self.val(p.get("width", Q(0.005, (1, 0))))} }});')

    def f_shell(self, rec):
        p = rec.params
        t = self.val(p.get("thickness", Q(0.0025, (1, 0))))
        sign = "" if p.get("oppositeDirection") else "-"  # opShell: positive grows outward
        self.emit(f'opShell(context, id + "{self.ids[rec.name]}", {{ "entities" : {self.query(p["entities"])}, '
                  f'"thickness" : {sign}({t}) }});')

    def f_booleanBodies(self, rec):
        p = rec.params
        op = p["operationType"].name if p.get("operationType") is not None else "UNION"
        fields = [f'"tools" : {self.query(p["tools"])}', f'"operationType" : BooleanOperationType.{op}']
        if op == "SUBTRACTION":
            fields.append(f'"targets" : {self.query(p["targets"])}')
        if p.get("keepTools"):
            fields.append('"keepTools" : true')
        self.emit(f'opBoolean(context, id + "{self.ids[rec.name]}", {{ {", ".join(fields)} }});')

    def f_deleteBodies(self, rec):
        self.emit(f'opDeleteBodies(context, id + "{self.ids[rec.name]}", {{ "entities" : {self.query(rec.params["entities"])} }});')

    # ---------------------------------------------------------------- patterns
    def loops_of(self, rec: FeatureRec) -> list[dict]:
        """One loop descriptor for this pattern feature."""
        p = rec.params
        if rec.feature_type == "linearPattern":
            if p.get("hasSecondDir"):
                raise PasteError(f"\"{rec.name}\": two-direction linear patterns are not supported yet")
            d = self.lb.direction_of(p["directionOne"], rec.name)
            d = -d if p.get("oppositeDirection") else d
            return [{"count": self.val(p.get("instanceCount", 2)),
                     "t": lambda k: f"transform({self.vec3(d)} * ({k} * ({self.val(p.get('distance', Q(0.025, (1, 0))))})))"}]
        if rec.feature_type == "circularPattern":
            ax = self.lb.axis_of(p["axis"], rec.name)
            n, ang = self.val(p.get("instanceCount", 4)), self.val(p.get("angle", Q(2 * math.pi, (0, 1))))
            full = abs(mag(p.get("angle", Q(2 * math.pi, (0, 1)))) - 2 * math.pi) < 1e-9
            if p.get("equalSpace", True):
                step = f"({ang}) / ({n})" if full else f"({ang}) / (({n}) - 1)"
            else:
                step = f"({ang})"
            sign = "-" if p.get("oppositeDirection") else ""
            return [{"count": n, "t": lambda k: f"rotationAround({self.line_expr(ax)}, {sign}{k} * {step})"}]
        if rec.feature_type == "mirror":
            pl = self.lb.plane_of(p["mirrorPlane"], rec.name)
            return [{"count": None, "t": lambda k: f"mirrorAcross(plane({self.vec3(np.asarray(pl.origin) * 1000)} * "
                                                    f"millimeter, {self.vec3(pl.normal)}, {self.vec3(pl.x)}))"}]
        raise PasteError(f"{rec.feature_type} is not a pattern")

    def recipes(self, name: str) -> list[tuple[FeatureRec, list]]:
        rec = self.recs[name]
        if rec.feature_type in ("linearPattern", "circularPattern", "mirror"):
            out = []
            for fid in rec.params.get("instanceFunction") or []:
                for base, loops in self.recipes(".".join(fid)):
                    out.append((base, loops + self.loops_of(rec)))
            return out
        if rec.feature_type in ("extrude", "revolve", "loft", "sweep"):
            return [(rec, [])]
        raise PasteError(f"feature pattern of \"{name}\" ({rec.feature_type}) is not supported")

    def pattern(self, rec: FeatureRec):
        p = rec.params
        if (p["patternType"].name if p.get("patternType") is not None else "PART") != "FEATURE":
            raise PasteError(f"\"{rec.name}\": only feature patterns are supported in the paste export")
        pid = self.ids[rec.name]
        for j, fid in enumerate(p.get("instanceFunction") or []):
            for r, (base, inner) in enumerate(self.recipes(".".join(fid))):
                loops = inner + self.loops_of(rec)
                ks = [f"k{j}_{r}_{n}" for n in range(len(loops))]
                indent = 0
                for k, lp in zip(ks, loops):
                    if lp["count"] is not None:
                        self.emit("    " * indent + f"for (var {k} = 1; {k} < {lp['count']}; {k} += 1)")
                        self.emit("    " * indent + "{")
                        indent += 1
                t = " * ".join(lp["t"](k) for k, lp in reversed(list(zip(ks, loops))))
                inst = " ~ \"_\" ~ ".join(ks) if any(l["count"] for l in loops) else '"m"'
                tid = f'id + "{pid}" + ("{j}_{r}_" ~ {inst})'
                save = self.body
                self.body = []
                self.tool(base, f'{tid} + "tool"')
                self.emit(f'opTransform(context, {tid} + "move", {{ "bodies" : qCreatedBy({tid} + "tool", '
                          f'EntityType.BODY), "transform" : {t} }});')
                self.boolean(base, f'{tid} + "tool"', f'{tid} + "bool"')
                inner_lines, self.body = self.body, save
                for line in inner_lines:
                    self.body.append("    " * indent + line)
                for _ in range(indent):
                    indent -= 1
                    self.emit("    " * indent + "}")

    f_linearPattern = f_circularPattern = f_mirror = pattern

    # ---------------------------------------------------------------- assemble
    def write(self) -> str:
        pre, derived = self.parameters()
        for line in derived:
            self.body.append(line)
        for rec in self.tr.features:
            fn = getattr(self, f"f_{rec.feature_type}", None)
            if fn is None:
                raise PasteError(f"\"{rec.name}\": {rec.feature_type} is not supported in the paste export")
            self.emit(f"// {rec.name}")
            fn(rec)
        if self.sketches:
            qs = ", ".join(f'qCreatedBy(id + "{s}", EntityType.BODY)' for s in self.sketches)
            self.emit("// remove the construction sketches and curves")
            self.emit(f'opDeleteBodies(context, id + "cleanupSketches", {{ "entities" : qUnion([{qs}]) }});')
        fname = _ident(self.title) or "generatedPart"
        precond = "\n".join(pre) if pre else ""
        return (HEADER + "// Generated by fsgen (paste export). Paste into a Feature Studio, then add the feature\n"
                + "// to a Part Studio from the toolbar. Parameters appear in the feature dialog.\n\n" + HELPERS
                + f'annotation {{ "Feature Type Name" : "{self.title}" }}\n'
                + f"export const {fname} = defineFeature(function(context is Context, id is Id, definition is map)\n"
                + "    precondition\n    {\n" + precond + "\n    }\n    {\n"
                + "\n".join(self.body) + "\n    });\n")


def paste_export(tr: TraceResult, title: str) -> tuple[str, dict]:
    """Return (Feature Studio code, check) where check compares the custom feature's local volume with the
    local build of the native script."""
    from ..fslite.runner import run_source

    w = PasteWriter(tr, title)
    code = w.write()
    run = run_source(code)
    native = w.loc.metrics()
    check = {"native_volume_mm3": native["volume_mm3"], "native_parts": native["parts"]}
    if not run.ok:
        check.update(ok=False, error=run.error)
        return code, check
    m = run.metrics()
    same = m["bodies"] == native["parts"] and abs(m["volume_mm3"] - native["volume_mm3"]) <= 1e-3 * max(
        1.0, native["volume_mm3"])
    check.update(ok=bool(same), paste_volume_mm3=m["volume_mm3"], paste_parts=m["bodies"],
                 parameters=[p["name"] for p in run.params], output=run.output)
    return code, check
