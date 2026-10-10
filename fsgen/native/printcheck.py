"""Print check: faces that overhang flatter than the printable angle and are not on the bed.

Parts are laid out in print orientation on the Top plane (rules.md), so the bed is the lowest Z of the build.
Downward faces steeper than 45 degrees print fine; flatter ones need a bridge or support. Small ones (pin holes,
a 0.5 mm catch face) usually print anyway, so the check reports sizes and leaves the call to the reader.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..fslite import geom as G
from .meshing import face_triangles, to_mm


@dataclass
class Overhang:
    part: int           # 1-based, in build order (as in the build report)
    kind: str           # "flat underside", "hole or arch top", "sloped underside"
    area_mm2: float
    z_mm: float         # lowest point of the overhanging area
    size_mm: tuple      # x, y extent of the overhanging area
    flattest_deg: float  # angle of the flattest triangle from horizontal (0 = flat ceiling)

    def text(self) -> str:
        return (f"{self.area_mm2:7.1f} mm2  {self.kind:17s} z {self.z_mm:6.1f}  "
                f"{self.size_mm[0]:.1f} x {self.size_mm[1]:.1f} mm  {self.flattest_deg:4.0f} deg  part {self.part}")


def overhangs(solids_m, max_angle_deg=45.0, bed_tol_mm=0.05, min_area_mm2=0.05) -> list[Overhang]:
    """Overhanging faces of solids given in meters (the local builder's units), largest first."""
    shapes = [to_mm(s) for s in solids_m]
    if not shapes:
        return []
    lo, _ = G.bbox(shapes)
    bed = float(lo[2])
    limit = -np.cos(np.radians(max_angle_deg - 0.5))   # normal.z below this = flatter (0.5 deg for meshing)
    out = []
    for i, shape in enumerate(shapes, 1):
        for face, t in face_triangles(shape):
            if not len(t):
                continue
            n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            area = np.linalg.norm(n, axis=1) / 2
            nz = n[:, 2] / np.maximum(2 * area, 1e-12)
            bad = (nz < limit) & (t[:, :, 2].min(1) > bed + bed_tol_mm) & (area > 0)
            a = float(area[bad].sum())
            if a < min_area_mm2:
                continue
            pts = t[bad].reshape(-1, 3)
            geom = G.face_geom(face)
            kind = ("flat underside" if geom == "PLANE" and nz[bad].min() < -0.999 else
                    "hole or arch top" if geom in ("CYLINDER", "CONE", "TORUS", "SPHERE") else "sloped underside")
            out.append(Overhang(i, kind, round(a, 2), round(float(pts[:, 2].min()), 2),
                                (round(float(np.ptp(pts[:, 0])), 1), round(float(np.ptp(pts[:, 1])), 1)),
                                round(float(np.degrees(np.arccos(np.clip(-nz[bad].min(), -1, 1)))), 1)))
    out.sort(key=lambda o: -o.area_mm2)
    return out


def report(items: list[Overhang], top=8, max_angle_deg=45.0) -> str:
    if not items:
        return f"print check: no overhangs flatter than {max_angle_deg:g} deg off the bed"
    total = sum(o.area_mm2 for o in items)
    lines = [f"print check: {len(items)} overhang area(s) flatter than {max_angle_deg:g} deg off the bed, "
             f"{total:.1f} mm2 in total (small catch faces and pin holes usually print without support):"]
    lines += ["  " + o.text() for o in items[:top]]
    if len(items) > top:
        lines.append(f"  ... and {len(items) - top} smaller")
    return "\n".join(lines)
