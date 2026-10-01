"""assembly_kin.py — kinematics for the ASSEMBLY IR: pose an assembly, including CLOSED loops.

The occurrence transforms in an assembly IR describe ONE pose (the authored pose). To render, export
or check the assembly anywhere else you need its kinematics: set some mate values (the drivers) and
find the rest so every loop closure still holds. That is this module. numpy only.

Model. Work in DISPLACEMENTS from the authored pose: D(occ) is the world->world motion of an
occurrence, identity at the authored pose (so posed transform = D @ T_authored). Down the mate tree,
    D(child) = D(parent) @ J(mate, q)
where J is the joint's motion about its axis AS AUTHORED (axes are given in assembly coordinates at
the authored pose, which is exactly why this composition is right). A closure between a and b holds
when D(a) and D(b) move the closure's feature (axis line / point / frame) identically.

Joint values (`q`, keyed by mate name): revolute degrees, slider mm, cylindrical (deg, mm).
Gear couplings are not solved here (drive the coupled mates explicitly).

    q, T = solve(asm, {"crank": 90})          # T: {occurrence id: 4x4 posed transform}
"""
from __future__ import annotations

import math

import numpy as np

import assembly_ir as A

FEATURE_MM = 10.0        # lever length for closure residual points (mm): balances angle vs position


def mat4(t12):
    """12-float row-major 3x4 (or None) -> 4x4."""
    M = np.eye(4)
    if t12:
        M[:3, :4] = np.asarray(t12, float).reshape(3, 4)
    return M


def t12(M):
    return [round(float(v), 6) for v in np.asarray(M)[:3, :4].reshape(-1)]


def _rot(axis, ang):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(ang) * K + (1 - math.cos(ang)) * K @ K


def joint_motion(m, val):
    """J(mate, value): the 4x4 motion of the child relative to the parent, in assembly coords."""
    J = np.eye(4)
    kind = m["kind"]
    if kind == "fastened":
        return J
    o, d = np.asarray(m["axis"]["origin"], float), np.asarray(m["axis"]["dir"], float)
    d = d / np.linalg.norm(d)
    ang, lin = 0.0, 0.0
    if kind == "revolute":
        ang = math.radians(val)
    elif kind == "slider":
        lin = val
    elif kind == "cylindrical":
        ang, lin = math.radians(val[0]), val[1]
    R = _rot(d, ang)
    J[:3, :3] = R
    J[:3, 3] = o - R @ o + d * lin
    return J


def displacements(asm, q):
    """{occurrence id: D} for joint values q (missing mates = 0 = authored)."""
    pm = A.parent_map(asm)
    D = {}

    def get(n):
        if n in D:
            return D[n]
        if n not in pm:
            D[n] = np.eye(4)
        else:
            p, m = pm[n]
            zero = (0.0, 0.0) if m["kind"] == "cylindrical" else 0.0
            D[n] = get(p) @ joint_motion(m, q.get(m["name"], zero))
        return D[n]
    for o in asm["occurrences"]:
        get(o["id"])
    return D


def posed(asm, q):
    """{occurrence id: 4x4 posed transform} = D @ T_authored."""
    D = displacements(asm, q)
    return {o["id"]: D[o["id"]] @ mat4(o.get("transform")) for o in asm["occurrences"]}


def _closure_points(c, asm):
    if c["kind"] == "fastened":
        T = mat4(next(o for o in asm["occurrences"] if o["id"] == A._endpoint(asm, c, "a")).get("transform"))
        o = T[:3, 3]
        return [o, o + [FEATURE_MM, 0, 0], o + [0, FEATURE_MM, 0]]
    o = np.asarray(c["axis"]["origin"], float)
    if c["kind"] == "spherical":
        return [o]
    d = np.asarray(c["axis"]["dir"], float)
    return [o, o + FEATURE_MM * d / np.linalg.norm(d)]


def closure_residual(asm, q):
    """Stacked (D_a p - D_b p) over every closure feature point, mm. Zero = all loops closed."""
    D = displacements(asm, q)
    r = []
    for c in asm.get("closures", []):
        Da, Db = D[A._endpoint(asm, c, "a")], D[A._endpoint(asm, c, "b")]
        for p in _closure_points(c, asm):
            ph = np.append(p, 1.0)
            r.extend((Da @ ph - Db @ ph)[:3])
    return np.asarray(r, float)


def _free_dofs(asm, fixed):
    """[(mate name, component)] for every DOF not fixed by the caller."""
    out = []
    for m in asm["mates"]:
        if m["name"] in fixed or m["kind"] == "fastened":
            continue
        out += [(m["name"], 0), (m["name"], 1)] if m["kind"] == "cylindrical" else [(m["name"], None)]
    return out


def _assemble(asm, fixed, dofs, x):
    q = dict(fixed)
    for (name, comp), v in zip(dofs, x):
        if comp is None:
            q[name] = float(v)
        else:
            cur = list(q.get(name, (0.0, 0.0)))
            cur[comp] = float(v)
            q[name] = tuple(cur)
    return q


def solve(asm, fixed, guess=None, steps=12, tol=1e-6, iters=60):
    """Pose the assembly with `fixed` mate values held; solve every other DOF so all closures hold.

    Continuation: the drivers move from the authored pose to `fixed` in `steps` increments, each
    solved by damped least squares (Levenberg-Marquardt, finite-difference Jacobian) from the last —
    so the solution stays on the authored ASSEMBLY BRANCH (a four-bar won't flip open<->crossed).
    Returns (q, transforms). Raises if a step can't close the loops (driver past a dead point)."""
    dofs = _free_dofs(asm, fixed)
    x = np.zeros(len(dofs))                    # authored pose
    if guess:
        x = np.array([guess.get(n, 0.0) if c is None else guess.get(n, (0, 0))[c] for n, c in dofs], float)
    for k in range(1, steps + 1):
        drv = {}
        for n, v in fixed.items():
            if isinstance(v, (tuple, list)):
                drv[n] = tuple(k / steps * float(vi) for vi in v)
            else:
                drv[n] = k / steps * float(v)
        lam = 1e-3
        for _ in range(iters):
            r = closure_residual(asm, _assemble(asm, drv, dofs, x))
            err = float(np.linalg.norm(r))
            if err < tol or not dofs:
                break
            Jm = np.empty((len(r), len(dofs)))
            h = 1e-6
            for i in range(len(dofs)):
                dx = np.zeros(len(dofs))
                dx[i] = h
                Jm[:, i] = (closure_residual(asm, _assemble(asm, drv, dofs, x + dx)) -
                            closure_residual(asm, _assemble(asm, drv, dofs, x - dx))) / (2 * h)
            step = np.linalg.solve(Jm.T @ Jm + lam * np.eye(len(dofs)), -Jm.T @ r)
            x_new = x + step
            if np.linalg.norm(closure_residual(asm, _assemble(asm, drv, dofs, x_new))) < err:
                x, lam = x_new, max(lam * 0.3, 1e-9)
            else:
                lam *= 10
        if err > max(tol * 1e3, 1e-4):
            raise ValueError(f"cannot close the loops at step {k}/{steps} (residual {err:.3g} mm): "
                             f"driver past a dead point or out of the mechanism's range")
    q = _assemble(asm, fixed, dofs, x)
    return q, posed(asm, q)
