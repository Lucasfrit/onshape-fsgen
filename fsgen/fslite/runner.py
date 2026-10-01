"""Run an FS-Lite Feature Studio file locally and export the resulting geometry."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import geom as G
from .geom import Context
from .interp import FeatureFn, Interpreter
from .lexer import FSSyntaxError
from .stdlib import StdLib
from .values import FSError, Id


@dataclass
class RunResult:
    ok: bool
    error: str | None = None
    error_line: int = 0
    context: Context | None = None
    output: list[str] = field(default_factory=list)
    feature: str | None = None
    params: list = field(default_factory=list)
    definition: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def solids(self):
        return [b for b in self.context.bodies if b.kind == "solid"] if self.context else []

    def metrics(self) -> dict:
        """Volume (mm^3), area (mm^2), bbox (mm), body count: comparable with Onshape."""
        solids = self.solids
        if not solids:
            return {"bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
        vol = sum(G.props(b.shape, "volume").Mass() for b in solids) * 1e9
        area = sum(G.props(b.shape, "area").Mass() for b in solids) * 1e6
        lo, hi = G.bbox([b.shape for b in solids])
        return {"bodies": len(solids), "volume_mm3": round(vol, 4), "area_mm2": round(area, 4),
                "bbox_mm": [round(float(v) * 1000, 4) for v in list(lo) + list(hi)]}


def describe(res: RunResult) -> str:
    """Compact, LLM-readable summary of the built geometry."""
    from collections import Counter

    if not res.ok:
        return f"FAILED: {res.error}"
    m = res.metrics()
    lines = [f"solid bodies: {m['bodies']}"]
    if m["bodies"]:
        b = m["bbox_mm"]
        lines.append(f"bounding box (mm): x {b[0]:g}..{b[3]:g}, y {b[1]:g}..{b[4]:g}, z {b[2]:g}..{b[5]:g} "
                     f"(size {b[3]-b[0]:g} x {b[4]-b[1]:g} x {b[5]-b[2]:g})")
        lines.append(f"volume: {m['volume_mm3']:.1f} mm^3, surface area: {m['area_mm2']:.1f} mm^2")
    for i, body in enumerate(res.solids):
        faces = G.subshapes(body.shape, G.TopAbs_FACE)
        kinds = Counter(G.face_geom(f) for f in faces)
        cyl = Counter()
        for f in faces:
            if G.face_geom(f) == "CYLINDER":
                r = G.BRepAdaptor_Surface(G.TopoDS.Face(f)).Cylinder().Radius()
                cyl[round(2 * r * 1000, 3)] += 1
        lo, hi = G.bbox([body.shape])
        desc = f"body {i + 1}: {len(faces)} faces ({', '.join(f'{v} {k.lower()}' for k, v in kinds.most_common())})"
        desc += f", bbox {[round(float(v) * 1000, 2) for v in lo]}..{[round(float(v) * 1000, 2) for v in hi]}"
        if cyl:
            desc += "; cylindrical face diameters (mm): " + ", ".join(f"{d:g} x{n}" for d, n in sorted(cyl.items()))
        lines.append(desc)
    if res.params:
        lines.append("parameters: " + ", ".join(f"{p['name']}={res.definition.get(p['name'], p['default'])}"
                                               for p in res.params))
    if res.output:
        lines.append("log: " + " | ".join(res.output[:10]))
    return "\n".join(lines)


def render_png(res: RunResult, path: Path, title: str = "") -> Path | None:
    """Four-view shaded preview (iso, front, top, right) using matplotlib; no GPU needed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopLoc import TopLoc_Location

    if not res.solids:
        return None
    tris = []
    for b in res.solids:
        BRepMesh_IncrementalMesh(b.shape, 0.0002, False, 0.3, True)
        for f in G.subshapes(b.shape, G.TopAbs_FACE):
            loc = TopLoc_Location()
            tri = G.BRep_Tool.Triangulation_s(G.TopoDS.Face(f), loc)
            if tri is None:
                continue
            trsf = loc.Transformation()
            pts = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
            pts = np.array([[p.X(), p.Y(), p.Z()] for p in pts]) * 1000
            rev = G.TopoDS.Face(f).Orientation() == 1
            for i in range(1, tri.NbTriangles() + 1):
                a, b_, c = tri.Triangle(i).Get()
                tris.append(pts[[a - 1, c - 1, b_ - 1]] if rev else pts[[a - 1, b_ - 1, c - 1]])
    tris = np.array(tris)
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    ctr, span = (lo + hi) / 2, (hi - lo).max() / 2 * 1.05
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    fig = plt.figure(figsize=(10, 10))
    views = [("iso", 25, -55), ("front (-Y)", 0, -90), ("top (+Z)", 90, -90), ("right (+X)", 0, 0)]
    for k, (name, elev, azim) in enumerate(views):
        ax = fig.add_subplot(2, 2, k + 1, projection="3d")
        e, a = np.radians(elev), np.radians(azim)
        view = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
        light = view * 0.8 + np.array([0.2, 0.1, 0.5])
        light /= np.linalg.norm(light)
        shade = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
        colors = np.stack([shade * 0.55, shade * 0.68, shade * 0.85, np.ones_like(shade)], 1)
        pc = Poly3DCollection(tris, facecolors=colors, edgecolor="none")
        ax.add_collection3d(pc)
        for i, axis in enumerate("xyz"):
            getattr(ax, f"set_{axis}lim")(ctr[i] - span, ctr[i] + span)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(name)
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
    if title:
        fig.suptitle(title)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80)
    plt.close(fig)
    return path


def run_source(src: str, feature: str | None = None, params: dict | None = None) -> RunResult:
    t0 = time.time()
    lib = StdLib()
    interp = Interpreter(lib.b)
    lib.interp = interp
    ctx = Context()
    res = RunResult(ok=False, context=ctx)
    try:
        interp.load(src)
        if not interp.features:
            raise FSError("no feature found: export a feature with `export const myPart = defineFeature(...)`")
        name = feature or next(iter(interp.features))
        if name not in interp.features:
            raise FSError(f"feature {name} not found; available: {', '.join(interp.features)}")
        res.feature = name
        ff, _ann = interp.features[name]
        definition, res.params = interp.collect_parameters(ff, params)
        res.definition = dict(definition)
        interp.call(ff, [ctx, Id(("F",)), definition], None)
        res.ok = True
    except (FSError, FSSyntaxError) as e:
        res.error = str(e)
        res.error_line = getattr(e, "line", 0)
    except RecursionError:
        res.error = "recursion too deep"
    except Exception as e:  # internal error: surface it but keep running
        res.error = f"internal error: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    res.output = interp.output + ctx.log
    res.seconds = time.time() - t0
    return res


def feature_parameters(src: str, feature: str | None = None, overrides: dict | None = None):
    """(feature name, parameter list, values) without running any geometry."""
    lib = StdLib()
    interp = Interpreter(lib.b)
    lib.interp = interp
    interp.load(src)
    if not interp.features:
        raise FSError("no feature found: export a feature with `export const myPart = defineFeature(...)`")
    name = feature or next(iter(interp.features))
    definition, params = interp.collect_parameters(interp.features[name][0], overrides)
    return name, params, definition


def export(res: RunResult, out_dir: Path, stem: str = "part", formats=("step", "stl")) -> list[Path]:
    """Export solids in meters->millimeters (build123d works in mm by convention)."""
    import build123d as bd
    from OCP.gp import gp_Trsf
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    out_dir.mkdir(parents=True, exist_ok=True)
    solids = res.solids
    if not solids:
        return []
    t = gp_Trsf()
    t.SetScaleFactor(1000.0)
    shape = BRepBuilderAPI_Transform(G.compound([b.shape for b in solids]), t, True).Shape()
    comp = bd.Compound(shape)
    paths = []
    if "step" in formats:
        p = out_dir / f"{stem}.step"
        bd.export_step(comp, str(p))
        paths.append(p)
    if "stl" in formats:
        p = out_dir / f"{stem}.stl"
        bd.export_stl(comp, str(p), tolerance=0.01, angular_tolerance=0.1)
        paths.append(p)
    return paths
