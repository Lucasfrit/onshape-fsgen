# ---- featuretree shared geometry (pure Python, no CAD imports) ----
# Inlined verbatim into every generated Fusion / SolidWorks script by script_emit.py, and imported
# directly by the tests. It turns IR sketches into loops of segments in a sketch's own 2D frame and
# says where that frame sits in the world, so each target runtime only has to (a) map a world point
# into its sketch space and (b) call its own line / arc / circle / extrude API. All lengths are mm.
import math

# A bulge smaller than this is a straight line (spec rule). At 1e-4 the sagitta of a 100 mm chord is
# 5 um; below it an "arc" has a km-scale radius that sketch solvers (FreeCAD) fail on.
BULGE_EPS = 1e-4


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


def poly_vertices(poly):
    """[(x, y, bulge), ...] with a duplicate closing vertex dropped (bulge 0 when absent)."""
    v = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0) for p in poly]
    if len(v) > 1 and abs(v[0][0] - v[-1][0]) < 1e-7 and abs(v[0][1] - v[-1][1]) < 1e-7:
        v = v[:-1]
    return v


def poly_segments(poly):
    """A closed wire as segments: ("line", p1, p2) or ("arc", p1, mid, p2). bulge is the DXF arc
    factor tan(theta/4) for the segment to the NEXT vertex (sign = CCW+); the arc's mid point is
    the chord midpoint pushed out by the sagitta along the chord's left normal (as fc_build)."""
    v = poly_vertices(poly)
    n = len(v)
    segs = []
    for i in range(n):
        x1, y1, b = v[i]
        x2, y2, _ = v[(i + 1) % n]
        if abs(b) < BULGE_EPS:
            segs.append(("line", (x1, y1), (x2, y2)))
        else:
            chord = math.hypot(x2 - x1, y2 - y1)
            sag = b * chord / 2.0
            ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
            mid = ((x1 + x2) / 2 - uy * sag, (y1 + y2) / 2 + ux * sag)
            segs.append(("arc", (x1, y1), mid, (x2, y2)))
    return segs


def arc_center(p1, mid, p2):
    """Centre, radius, start angle and CCW-signed sweep (radians) of the arc p1 -> mid -> p2."""
    ax, ay = p1
    bx, by = mid
    cx, cy = p2
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a1 = math.atan2(ay - uy, ax - ux)
    am = (math.atan2(by - uy, bx - ux) - a1) % (2 * math.pi)
    a2 = (math.atan2(cy - uy, cx - ux) - a1) % (2 * math.pi)
    sweep = a2 if am < a2 else a2 - 2 * math.pi       # the direction that passes through mid
    return (ux, uy), r, a1, sweep


def loop_points(loop, per_arc=32, per_circle=96):
    """A loop as a polyline (arcs and circles discretised), for 2D checks such as validation."""
    segs = loop["segments"]
    if segs[0][0] == "circle":
        (cx, cy), r = segs[0][1], segs[0][2]
        return [(cx + r * math.cos(2 * math.pi * k / per_circle), cy + r * math.sin(2 * math.pi * k / per_circle))
                for k in range(per_circle)]
    pts = []
    for s in segs:
        if s[0] == "line":
            pts.append(s[1])
        else:
            c, r, a1, sw = arc_center(s[1], s[2], s[3])
            pts += [(c[0] + r * math.cos(a1 + sw * k / per_arc), c[1] + r * math.sin(a1 + sw * k / per_arc))
                    for k in range(per_arc)]
    return pts


def rect_segments(w, h, cx, cy):
    hw, hh = w / 2.0, h / 2.0
    p = [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw, cy + hh)]
    return [("line", p[i], p[(i + 1) % 4]) for i in range(4)]


def _area(v):
    return abs(sum(v[i][0] * v[(i + 1) % len(v)][1] - v[(i + 1) % len(v)][0] * v[i][1]
                   for i in range(len(v)))) / 2.0


def _pip(x, y, v):
    inside = False
    for i in range(len(v)):
        x1, y1 = v[i][0], v[i][1]
        x2, y2 = v[(i + 1) % len(v)][0], v[(i + 1) % len(v)][1]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def poly_depths(polys):
    """Nesting depth of each poly — the rule b3d_emit and FreeCAD's Pad apply: a poly inside
    another is a hole in it, an island in a hole is solid again. Even depth = material."""
    vs = [poly_vertices(p) for p in polys]
    areas = [_area(v) for v in vs]
    order = sorted(range(len(vs)), key=lambda i: -areas[i])
    depth = {}
    for n, i in enumerate(order):
        x, y = vs[i][0][0], vs[i][0][1]
        best = None
        for j in order[:n]:
            if areas[j] > areas[i] and _pip(x, y, vs[j]) and (best is None or areas[j] < areas[best]):
                best = j
        depth[i] = 0 if best is None else depth[best] + 1
    return [depth[i] for i in range(len(vs))]


def sketch_loops(circles=(), rects=(), polys=()):
    """Every closed loop of a sketch, in the sketch's own 2D frame, with whether the region it
    bounds is material. Circles and rects are each their own region (as in b3d_emit); polys follow
    the nesting rule. A loop is {"segments": [...], "include": bool}; a circle is the single
    segment ("circle", (cx, cy), r)."""
    loops = [{"segments": [("circle", (float(c[0]), float(c[1])), float(c[2]))], "include": True}
             for c in circles]
    loops += [{"segments": rect_segments(*r), "include": True} for r in rects]
    for poly, d in zip(polys, poly_depths(polys) if polys else []):
        loops.append({"segments": poly_segments(poly), "include": d % 2 == 0})
    return loops


def frame(origin, normal, xdir):
    """An orthonormal sketch frame (o, x, y, n) with y = n × x — build123d's Plane convention,
    which is what b3d_emit (the reference backend) uses for prism_cut."""
    n = _unit(normal)
    x = _unit(xdir)
    return (tuple(float(c) for c in origin), x, _cross(n, x), n)


def to_world(fr, u, v):
    o, x, y, _ = fr
    return _add(o, _add(_mul(x, u), _mul(y, v)))


XY = frame((0, 0, 0), (0, 0, 1), (1, 0, 0))       # IR sketch plane "XY": (u, v) -> (x, y, 0)
XZ = frame((0, 0, 0), (0, -1, 0), (1, 0, 0))      # "XZ" (revolve profile): (u, v) -> (u, 0, v)


def offset_xy(z):
    return frame((0, 0, z), (0, 0, 1), (1, 0, 0))


def polar_cuts(f):
    """polar_pocket as `count` placed cylinder cuts: at azimuth a the station is
    (mount_r cos a, mount_r sin a, z) and the bore runs along the tangent, centred on the station
    (b3d_emit's Rot(0,0,a)·Rot(90,0,0)·Cylinder). Each is (frame, loops, depth)."""
    n, r, L = int(f["count"]), float(f["radius"]), float(f["length"])
    mr, z, phase = float(f["mount_r"]), float(f.get("z", 0.0)), float(f.get("phase", 0.0))
    out = []
    for i in range(n):
        a = math.radians(phase + 360.0 * i / n)
        t = (-math.sin(a), math.cos(a), 0.0)
        station = (mr * math.cos(a), mr * math.sin(a), z)
        fr = frame(_add(station, _mul(t, -L / 2.0)), t, (0.0, 0.0, 1.0))
        out.append((fr, sketch_loops(circles=[(0.0, 0.0, r)]), L))
    return out


def param_name(feature, key):
    """A CAD-safe parameter identifier for (feature, key): letters, digits, underscore."""
    s = "".join(ch if ch.isalnum() else "_" for ch in "ft_%s_%s" % (feature, key))
    return s if not s[0].isdigit() else "_" + s


def volume_check(actual, expected, rel=1e-3, abs_tol=1e-3):
    """True when a target's volume after a feature matches the build123d reference."""
    if expected is None or actual is None:
        return None
    return abs(actual - expected) <= max(abs_tol, rel * abs(expected))
# ---- end shared geometry ----
