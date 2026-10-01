"""iou_check.py — Boolean-FREE intersection-over-union between two solids.

Why not just use a Boolean? IoU from an OpenCASCADE union (|A∩B| = |A|+|B|-|A∪B|)
is unreliable on exactly the cases you most want to measure. On the NIST corpus it
returned a "union" equal to the larger operand, which makes IoU collapse to the
volume ratio B/A and passes every range check (U >= max(A,B) holds with
equality). Four of eight PARTIAL recoveries were mis-scored that way, one by
56 points.

This module never calls a Boolean. It draws points uniformly in the union of the
two bounding boxes and classifies each against both solids with
BRepClass3d_SolidClassifier; IoU = #(in A and in B) / #(in A or in B). The box
volume cancels, and the estimate carries a 95% binomial interval.

    from iou_check import iou, registered_iou
    p, ci = iou(a, b)                       # a, b already in the same frame
    r = registered_iou(original, recovered) # recovered in its own frame

Registration: step_recognize works in the part's own frame (it rotates the extrude
axis onto Z), so a recovery is generally rotated relative to its input.
registered_iou searches the 24 axis-aligned rotations under bounding-box-min-corner
alignment and returns the best. That is not an optimal registration, so the result
is a LOWER bound on how well the recovery can be made to overlap its input: it can
certify that a recovery is largely right, and can never show that one is poor.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN


def _distinct_rotations():
    """The 24 distinct axis-aligned orientations, as (rx, ry, rz) triples in build123d's own
    Rot convention. The 64 Euler triples of multiples of 90 degrees repeat each orientation
    ~2.7x; deduplicating on the actual rotation matrix removes the wasted work."""
    from build123d import Rot
    seen, out = set(), []
    for t in itertools.product((0, 90, 180, 270), repeat=3):
        tr = Rot(*t).wrapped.Transformation()
        key = tuple(round(tr.Value(i, j)) for i in (1, 2, 3) for j in (1, 2, 3))
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


ROTATIONS = _distinct_rotations()


def _solids(shape):
    s = shape.solids() if hasattr(shape, "solids") else []
    return list(s) if s else [shape]


def solid_part(shape):
    """Reduce any shape to ITS SOLIDS ONLY, as one object. A STEP import is a Compound that
    can carry stray wires, edges and points: NIST CTC-01's import has a 1170 x 650 mm
    bounding box around an 800 x 450 mm solid. Registering against that inflated box put a
    VERIFIED recovery at 20% IoU. Every public function normalises through here, so callers
    can pass an import_step() result directly."""
    from build123d import Compound
    s = _solids(shape)
    return s[0] if len(s) == 1 else Compound(s)


def _inside(shape, pts):
    """Boolean mask: which points lie strictly inside ANY solid of `shape`."""
    clfs = [BRepClass3d_SolidClassifier(s.wrapped) for s in _solids(shape)]
    out = np.zeros(len(pts), dtype=bool)
    for i, (x, y, z) in enumerate(pts):
        p = gp_Pnt(float(x), float(y), float(z))
        for c in clfs:
            c.Perform(p, 1e-6)
            if c.State() == TopAbs_IN:
                out[i] = True
                break
    return out


def iou(a, b, n=60000, seed=0):
    """(IoU, 95% half-width), both as fractions, for two shapes in the SAME frame."""
    a, b = solid_part(a), solid_part(b)
    ba, bb = a.bounding_box(), b.bounding_box()
    lo = np.minimum([ba.min.X, ba.min.Y, ba.min.Z], [bb.min.X, bb.min.Y, bb.min.Z])
    hi = np.maximum([ba.max.X, ba.max.Y, ba.max.Z], [bb.max.X, bb.max.Y, bb.max.Z])
    pts = np.random.default_rng(seed).uniform(lo, hi, size=(n, 3))
    ia, ib = _inside(a, pts), _inside(b, pts)
    u, i = int((ia | ib).sum()), int((ia & ib).sum())
    if u == 0:
        return 0.0, 0.0
    p = i / u
    return p, 1.96 * math.sqrt(max(p * (1 - p), 1e-12) / u)


def register(rec, orig, rot):
    """`rec` rotated by `rot` (degrees about X, Y, Z), then translated so its bbox-min corner
    meets `orig`'s. The same registration family the paper's figures use."""
    from build123d import Pos, Rot
    rec, orig = solid_part(rec), solid_part(orig)
    r = Rot(*rot) * rec
    bo, br = orig.bounding_box(), r.bounding_box()
    return Pos(bo.min.X - br.min.X, bo.min.Y - br.min.Y, bo.min.Z - br.min.Z) * r


def registered_iou(orig, rec, n=60000, n_search=4000, seed=0):
    """Best IoU of `rec` against `orig` over the 24 axis-aligned rotations.

    Searches coarsely (`n_search` points per rotation), then re-measures the winner with
    `n` points. Returns {"iou", "ci", "rot", "lower_bound": True} with fractions in [0, 1].
    """
    orig, rec = solid_part(orig), solid_part(rec)
    best_rot, best_p = None, -1.0
    for rot in ROTATIONS:
        p, _ = iou(orig, register(rec, orig, rot), n=n_search, seed=seed + 1)
        if p > best_p:
            best_rot, best_p = list(rot), p
    p, ci = iou(orig, register(rec, orig, best_rot), n=n, seed=seed + 2)
    return {"iou": p, "ci": ci, "rot": best_rot, "lower_bound": True}


def registration_transform(rec, orig, rot):
    """The rigid map register() applies, as functions on points and directions, so features of a
    recovered tree (hole axes, sketch planes) can be carried into the ORIGINAL part's frame."""
    from build123d import Rot
    rec, orig = solid_part(rec), solid_part(orig)
    tr = Rot(*rot).wrapped.Transformation()
    R = [[tr.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)]
    rb, ob = (Rot(*rot) * rec).bounding_box(), orig.bounding_box()
    delta = (ob.min.X - rb.min.X, ob.min.Y - rb.min.Y, ob.min.Z - rb.min.Z)

    def direction(v):
        return tuple(sum(R[i][j] * v[j] for j in range(3)) for i in range(3))

    def point(p):
        q = direction(p)
        return tuple(q[i] + delta[i] for i in range(3))

    return point, direction
