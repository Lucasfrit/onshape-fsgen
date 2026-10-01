"""intent.py — infer DESIGN INTENT from a recovered feature tree.

Recognition recovers AN equivalent history, not THE designer's: a verified tree rebuilds the part,
but its holes are buried as arc outlines inside generic pockets, equal holes are unrelated, and
every size is a bare float. Intent inference re-expresses that tree the way a designer would have
drawn it:

  holes(spec)        every round cut as a Hole: diameter, axis line, through/blind, depth
  counterbores(...)  a blind round cut coaxial with a smaller through-hole
  patterns(holes)    equal holes grouped into linear / polar arrays and pairs
  infer_units(vals)  inch or metric, from which grid the dimensions land on
  nominal(d, units)  the standard size a diameter most plausibly is ("1/4\"", "letter A", "8 mm")

It never changes geometry, so it cannot break a VERIFIED tree. It is scored against the
designer's own semantic PMI where an AP242 file carries it (pmi.py, paper/figures/intent_eval.py).

Coordinates are those of the IR itself, i.e. of the part b3d_emit builds from it.
"""
from __future__ import annotations

import math
from collections import namedtuple

Hole = namedtuple("Hole", "diameter center axis kind depth feature")

# ------------------------------------------------------------------ standard sizes
# US letter and number drill diameters in inches, per the ASME B94.11M table as tabulated at
# https://en.wikipedia.org/wiki/Drill_bit_sizes (fetched 2026-09-29). Fractional-inch sizes are
# computed exactly rather than tabulated.
LETTER_DRILLS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (
    0.234, 0.238, 0.242, 0.246, 0.250, 0.257, 0.261, 0.266, 0.272, 0.277, 0.281, 0.290, 0.295,
    0.302, 0.316, 0.323, 0.332, 0.339, 0.348, 0.358, 0.368, 0.377, 0.386, 0.397, 0.404, 0.413)))
NUMBER_DRILLS = dict(enumerate((
    0.228, 0.221, 0.213, 0.209, 0.2055, 0.204, 0.201, 0.199, 0.196, 0.1935, 0.191, 0.189, 0.185,
    0.182, 0.180, 0.177, 0.173, 0.1695, 0.166, 0.161, 0.159, 0.157, 0.154, 0.152, 0.1495, 0.147,
    0.144, 0.1405, 0.136, 0.1285, 0.120, 0.116, 0.113, 0.111, 0.110, 0.1065, 0.104, 0.1015,
    0.0995, 0.098, 0.096, 0.0935, 0.089, 0.086, 0.082, 0.081, 0.0785, 0.076, 0.073, 0.070, 0.067,
    0.0635, 0.0595, 0.055, 0.052, 0.0465, 0.043, 0.042, 0.041, 0.040), start=1))
INCH = 25.4


def _frac(inches, denom=64):
    n = round(inches * denom)
    g = math.gcd(n, denom)
    whole, rem = divmod(n // g, denom // g)
    d = denom // g
    if rem == 0:
        return f'{whole}"'
    return (f'{whole}-{rem}/{d}"' if whole else f'{rem}/{d}"')


def nominal_value(d_mm, units, tol_mm=0.01):
    """(label, nominal size in mm) for `d_mm`, or (None, None). See nominal()."""
    cands = []
    if units == "inch":
        x = d_mm / INCH
        f = round(x * 64) / 64
        cands.append((abs(f - x) * INCH, 0, _frac(x), f * INCH))
        for name, table in (("letter ", LETTER_DRILLS), ("#", NUMBER_DRILLS)):
            for k, v in table.items():
                cands.append((abs(v - x) * INCH, 1, f'{name}{k} ({v:.4f}")', v * INCH))
        cands.append((abs(round(x, 3) - x) * INCH, 2, f'{round(x, 3):.3f}"', round(x, 3) * INCH))
    else:
        cands.append((abs(round(d_mm, 1) - d_mm), 0, f"{round(d_mm, 1):g} mm", round(d_mm, 1)))
    ok = [c for c in cands if c[0] <= tol_mm]
    if not ok:
        return None, None
    best = min(ok, key=lambda c: (round(c[0], 3), c[1]))
    return best[2], best[3]


def nominal(d_mm, units, tol_mm=0.01):
    """The standard size `d_mm` most plausibly is, in the given unit system, or None.

    Every candidate within tolerance competes and the CLOSEST wins — taking the first match would
    call 5.944 mm 15/64" (0.009 mm off) when it is letter drill A (0.0004 mm off), which is also
    what the designer's drawing says. Inch candidates: 1/64" fractions, letter and number drills,
    then 0.001" decimals. Metric: 0.1 mm sizes."""
    return nominal_value(d_mm, units, tol_mm)[0]


def infer_units(values_mm, tol_mm=0.005):
    """'inch' or 'metric': which grid EXPLAINS the numbers better.

    Each value earns log(g / 2t) for the coarsest grid step g it lands on (within t) — the
    surprise of hitting that grid by chance. A random value hits nothing and earns 0, so the score
    rewards systems in which the dimensions are round, and grids are weighted so that fine grids
    (which catch anything) are cheap. Returns (units, {"inch": s, "metric": s})."""
    grids = {"inch": [g * INCH for g in (1, .5, .25, 1 / 8, 1 / 16, 1 / 32, 1 / 64, .01, .005)],
             "metric": [10, 5, 1, .5, .1]}
    score = {}
    for sys_, steps in grids.items():
        s = 0.0
        for v in values_mm:
            for g in steps:                               # coarsest first
                if abs(round(v / g) * g - v) <= tol_mm:
                    s += math.log(g / (2 * tol_mm))
                    break
        score[sys_] = round(s, 2)
    return ("inch" if score["inch"] > score["metric"] else "metric"), score


# ------------------------------------------------------------------ holes
def _arc_center(p0, p1, bulge):
    """Center and radius of the arc p0->p1 with DXF bulge (tan(theta/4)), sign = CCW+."""
    (x0, y0), (x1, y1) = p0, p1
    c = math.hypot(x1 - x0, y1 - y0)
    s = bulge * c / 2.0                                   # signed sagitta, left of the chord
    r = (c * c / 4 + s * s) / (2 * abs(s))
    ux, uy = (x1 - x0) / c, (y1 - y0) / c
    nx, ny = -uy, ux                                      # left normal
    mx, my = (x0 + x1) / 2 + nx * s, (y0 + y1) / 2 + ny * s   # arc midpoint
    sg = 1 if s > 0 else -1
    return (mx - nx * sg * r, my - ny * sg * r), r


def circle_of(poly, tol=1e-3):
    """(cx, cy, diameter) if the closed poly is a full circle made of arcs, else None."""
    pts = [(p[0], p[1]) for p in poly]
    bul = [p[2] if len(p) > 2 else 0.0 for p in poly]
    if len(pts) < 2 or any(abs(b) < 1e-9 for b in bul):
        return None
    centers = [_arc_center(pts[i], pts[(i + 1) % len(pts)], bul[i]) for i in range(len(pts))]
    (cx, cy), r = centers[0]
    if all(math.hypot(c[0] - cx, c[1] - cy) < tol and abs(rr - r) < tol for c, rr in centers):
        sweep = sum(4 * math.atan(abs(b)) for b in bul)
        if abs(sweep - 2 * math.pi) < 1e-3:
            return cx, cy, 2 * r
    return None


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def holes(spec):
    """Every round cut in the tree as a Hole, in the IR's frame. Pads are profiles, not holes."""
    sketches, out = {}, []
    for f in spec["features"]:
        k = f["kind"]
        if k == "sketch":
            sketches[f["name"]] = f
        elif k == "pocket":
            sk = sketches.get(f["sketch"], {})
            if sk.get("plane", "XY") != "XY":
                continue
            kind = "through" if f.get("through") else "blind"
            depth = None if kind == "through" else f.get("length")
            rounds = [(c[0], c[1], 2 * c[2]) for c in sk.get("circles", [])]
            rounds += [c for c in (circle_of(p) for p in sk.get("polys", [])) if c]
            for cx, cy, d in rounds:
                out.append(Hole(d, (cx, cy, 0.0), (0.0, 0.0, 1.0), kind, depth, f["name"]))
        elif k == "pad":
            # A LAYERED tree (step_recognize._recognize_layers) draws each layer's holes as inner
            # loops of its pad sketch: a round loop nested an odd number of loops deep is a hole
            # through that layer. The same hole recurs in every layer it passes, so dedupe on axis.
            polys = sketches.get(f["sketch"], {}).get("polys", [])
            for i, p in enumerate(polys):
                c = circle_of(p)
                if not c:
                    continue
                depth_ = sum(1 for j, q in enumerate(polys) if j != i and _inside(c[:2], q))
                if depth_ % 2 == 1 and not any(h.kind == "layer" and abs(h.diameter - c[2]) < 0.02
                                               and math.hypot(h.center[0] - c[0], h.center[1] - c[1]) < 0.02
                                               for h in out):
                    out.append(Hole(c[2], (c[0], c[1], 0.0), (0.0, 0.0, 1.0), "layer", None, f["name"]))
        elif k == "revolve":
            # A revolved profile's constant-radius straight edges are cylinders about Z: the
            # innermost is a BORE, the outermost the OUTER diameter, anything between a STEP.
            # In intent terms a bore is a hole, even though no cut produced it.
            sk = sketches.get(f["sketch"], {})
            for poly in sk.get("polys", []):
                n = len(poly)
                segs = [(poly[m], poly[(m + 1) % n]) for m in range(n)]
                radii = sorted({round(abs(a[0]), 6) for a, b in segs
                                if abs((a[2] if len(a) > 2 else 0.0)) < 1e-9
                                and abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) > 1e-6
                                and abs(a[0]) > 1e-6})
                for r in radii:
                    kind = "bore" if r == radii[0] and len(radii) > 1 else \
                           "outer" if r == radii[-1] else "step"
                    out.append(Hole(2 * r, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), kind, None, f["name"]))
        elif k == "prism_cut":
            o, n, x = f["origin"], f["normal"], f["xdir"]
            y = _cross(n, x)
            for poly in f.get("polys", []):
                c = circle_of(poly)
                if c:
                    u, v, d = c
                    ctr = tuple(o[i] + u * x[i] + v * y[i] for i in range(3))
                    out.append(Hole(d, ctr, tuple(n), "cut", f["depth"], f["name"]))
    return out


def _inside(pt, poly):
    """Even-odd point-in-polygon on the loop's vertices (arcs taken as chords -- enough to nest
    a hole inside the outline that contains it)."""
    x, y = pt
    n, inside = len(poly), False
    for m in range(n):
        (x1, y1), (x2, y2) = poly[m][:2], poly[(m + 1) % n][:2]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _line_dist(p, q, axis):
    """Distance from point q to the line through p along unit `axis`."""
    d = [q[i] - p[i] for i in range(3)]
    t = sum(d[i] * axis[i] for i in range(3))
    return math.sqrt(max(sum(v * v for v in d) - t * t, 0.0))


def counterbores(hs, tol=0.02):
    """[(through_hole, bore)] where a larger blind round cut shares a through-hole's axis — a
    counterbore or spotface, i.e. ONE hole feature the designer drew, not two cuts."""
    out = []
    for b in (h for h in hs if h.kind == "blind"):
        for t in (h for h in hs if h.kind in ("through", "cut") and h.diameter < b.diameter):
            if abs(abs(sum(b.axis[i] * t.axis[i] for i in range(3))) - 1) < 1e-6 \
                    and _line_dist(t.center, b.center, t.axis) < tol:
                out.append((t, b))
    return out


# ------------------------------------------------------------------ patterns
def _plane_coords(hs):
    """Project hole centres onto the plane normal to their (shared) axis."""
    a = hs[0].axis
    ref = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _cross(a, ref)
    n = math.sqrt(sum(c * c for c in u))
    u = tuple(c / n for c in u)
    v = _cross(a, u)
    return [(sum(h.center[i] * u[i] for i in range(3)), sum(h.center[i] * v[i] for i in range(3)))
            for h in hs]


def _linear_runs(pts, idx, tol):
    """Maximal sets of >= 3 collinear, equally spaced points (by index)."""
    best = []
    for i in idx:
        for j in idx:
            if j <= i:
                continue
            (x0, y0), (x1, y1) = pts[i], pts[j]
            L = math.hypot(x1 - x0, y1 - y0)
            if L < tol:
                continue
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            on = sorted(((((pts[k][0] - x0) * ux + (pts[k][1] - y0) * uy), k) for k in idx
                         if abs(-(pts[k][0] - x0) * uy + (pts[k][1] - y0) * ux) < tol))
            if len(on) < 3:
                continue
            steps = [on[m + 1][0] - on[m][0] for m in range(len(on) - 1)]
            if max(steps) - min(steps) < tol:
                cand = [k for _, k in on]
                if len(cand) > len(best):
                    best = cand
    return best


def _polar(pts, idx, tol):
    """>= 3 points on one circle at equal angular spacing (a bolt circle), or []."""
    if len(idx) < 3:
        return []
    cx = sum(pts[k][0] for k in idx) / len(idx)
    cy = sum(pts[k][1] for k in idx) / len(idx)
    rs = [math.hypot(pts[k][0] - cx, pts[k][1] - cy) for k in idx]
    if min(rs) < tol or max(rs) - min(rs) > tol:
        return []
    ang = sorted(math.atan2(pts[k][1] - cy, pts[k][0] - cx) for k in idx)
    gaps = [(ang[(m + 1) % len(ang)] - ang[m]) % (2 * math.pi) for m in range(len(ang))]
    return list(idx) if max(gaps) - min(gaps) < tol / rs[0] else []


def _rectangle(pts, idx, tol):
    """Four points at the corners of a rectangle (any orientation): the two diagonals share a
    midpoint and are equal in length. Returns the four indices, or []."""
    from itertools import combinations
    best = []
    for quad in combinations(idx, 4):
        for a, b, c, d in ((0, 1, 2, 3), (0, 2, 1, 3), (0, 3, 1, 2)):
            p, q, r, s_ = (pts[quad[a]], pts[quad[b]], pts[quad[c]], pts[quad[d]])
            m1 = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
            m2 = ((r[0] + s_[0]) / 2, (r[1] + s_[1]) / 2)
            if math.hypot(m1[0] - m2[0], m1[1] - m2[1]) < tol and \
                    abs(math.hypot(p[0] - q[0], p[1] - q[1]) - math.hypot(r[0] - s_[0], r[1] - s_[1])) < tol:
                return list(quad)
    return best


def patterns(hs, tol=0.02, dia_tol=0.01):
    """Group holes into the arrays a designer would have drawn.

    Holes are grouped by diameter, kind and axis direction; within a group, the largest regular
    subset is peeled off repeatedly — a bolt circle if the whole remainder is one, else the longest
    equally spaced line, else a rectangle of four — and whatever is left is a pair (2) or single
    holes. Returns a list of {"type", "diameter", "holes", "pitch"|"pcd"}."""
    groups = []
    for h in sorted(hs, key=lambda h: h.diameter):
        for g in groups:
            g0 = g[0]
            if abs(g0.diameter - h.diameter) < dia_tol and g0.kind == h.kind and \
                    abs(abs(sum(g0.axis[i] * h.axis[i] for i in range(3))) - 1) < 1e-6:
                g.append(h)
                break
        else:
            groups.append([h])

    out = []
    for g in groups:
        pts = _plane_coords(g)
        left = list(range(len(g)))
        while left:
            pol = _polar(pts, left, tol)
            if pol and len(pol) == len(left) and len(left) >= 3:
                cx = sum(pts[k][0] for k in pol) / len(pol)
                cy = sum(pts[k][1] for k in pol) / len(pol)
                r = math.hypot(pts[pol[0]][0] - cx, pts[pol[0]][1] - cy)
                out.append({"type": "polar", "diameter": g[0].diameter, "pcd": 2 * r,
                            "holes": [g[k] for k in pol]})
                left = []
                break
            run = _linear_runs(pts, left, tol)
            if not run:
                rect = _rectangle(pts, left, tol) if len(left) >= 4 else []
                if not rect:
                    break
                xs = sorted(math.hypot(pts[rect[0]][0] - pts[k][0], pts[rect[0]][1] - pts[k][1])
                            for k in rect[1:])
                out.append({"type": "rectangle", "diameter": g[0].diameter,
                            "pitch": xs[0], "pitch2": xs[1], "holes": [g[k] for k in rect]})
                left = [k for k in left if k not in rect]
                continue
            (x0, y0), (x1, y1) = pts[run[0]], pts[run[1]]
            out.append({"type": "linear", "diameter": g[0].diameter,
                        "pitch": math.hypot(x1 - x0, y1 - y0), "holes": [g[k] for k in run]})
            left = [k for k in left if k not in run]
        if len(left) == 2:
            (x0, y0), (x1, y1) = pts[left[0]], pts[left[1]]
            out.append({"type": "pair", "diameter": g[0].diameter,
                        "pitch": math.hypot(x1 - x0, y1 - y0), "holes": [g[k] for k in left]})
        else:
            out += [{"type": "single", "diameter": g[0].diameter, "holes": [g[k]]} for k in left]
    return out


def describe(spec, extents=()):
    """One call: holes, counterbores, patterns, inferred units and nominal sizes."""
    hs = holes(spec)
    cb = counterbores(hs)
    bores = {id(b) for _, b in cb}
    drilled = [h for h in hs if id(h) not in bores]        # a counterbore is part of its hole
    pats = patterns(drilled)
    vals = sorted({round(h.diameter, 4) for h in hs})
    vals += [round(p["pitch"], 4) for p in pats if "pitch" in p]
    vals += [round(p["pcd"], 4) for p in pats if "pcd" in p]
    vals += [round(e, 4) for e in extents]
    units, score = infer_units(vals)
    return {"holes": hs, "counterbores": cb, "patterns": pats, "units": units,
            "unit_score": score,
            "nominals": {round(d, 3): nominal(d, units) for d in sorted({h.diameter for h in hs})}}
