"""assembly_ir.py — the ASSEMBLY half of the neutral IR: occurrences + parts + typed mates.

Additive to ir.py (which stays single-part). An assembly is a set of named OCCURRENCES, each placing
a PART (a single-part feature IR from ir.py / step_recognize) by a transform, plus typed MATES between
occurrences, gear COUPLINGS and named POSES. The mate vocabulary is deliberately the same closed set
cadgen (text-to-cad "CAD Skills") carries in its `.step.json` sidecar — revolute / slider /
cylindrical / fastened, an axis {origin, dir}, limits — so a cadgen assembly ingests losslessly and
can round-trip back out. Plain JSON-able dicts, like ir.py; nothing here is consumed by the existing
single-part emitters, so they cannot be affected by it.

LOOP CLOSURES. `mates` is always a forward-kinematic TREE (so it stays cadgen-compatible). A closed
linkage (four-bar, parallelogram, slider-crank) adds CLOSURES: extra constraints between two
occurrences that are ALREADY connected through the tree. They live in their own list so nothing that
walks `mates` ever sees a cycle; consumers that understand them (assembly_kin's pose solver, the MJCF
emitter) read `closures`, and a cadgen export drops them (cadgen forbids cycles).

Conventions (cadgen's): millimetres; a mate/closure `axis` is given in ASSEMBLY coordinates at the
authored pose (the pose the occurrence transforms describe); revolute limits are degrees, slider mm.
"""

MATE_KINDS = ("revolute", "slider", "cylindrical", "fastened")
CLOSURE_KINDS = ("revolute", "spherical", "fastened")
MATE_DOF = {"revolute": 1, "slider": 1, "cylindrical": 2, "fastened": 0}
CLOSURE_CONSTRAINTS = {"revolute": 5, "spherical": 3, "fastened": 6}   # spatial


def occurrence(id, label, part=None, transform=None, color=None, component=None):
    """One placed instance of a part. `id` is the tree address ("o1", "o1.3", "o1.2.1" — depth-first,
    1-based, the same grammar cadgen uses so `#o1.3.f2` refs stay meaningful). `part` is the part's
    single-part IR spec (or None when only structure is known). `transform` is a 12-float row-major
    3x4 placement matrix, or None for identity."""
    return {"id": id, "label": label, "part": part,
            "transform": [round(float(v), 6) for v in transform] if transform else None,
            "color": list(color) if color else None, "component": component}


def mate(name, kind, parent, child, *, parent_id=None, child_id=None, axis=None, limits=None):
    """A typed joint from `parent` to `child` (occurrence LABELS; `*_id` are the resolved occurrence
    ids). `axis` = {"origin": [x,y,z], "dir": [x,y,z]} (None for fastened). `limits` = a mapping of DOF
    -> [lo, hi] (cadgen spells a 1-DOF mate's range as {"value": [lo, hi]})."""
    if kind not in MATE_KINDS:
        raise ValueError(f"mate {name!r}: kind must be one of {MATE_KINDS}, got {kind!r}")
    return {"name": name, "kind": kind, "parent": parent, "child": child,
            "parent_id": parent_id, "child_id": child_id,
            "axis": ({"origin": [float(v) for v in axis["origin"]], "dir": [float(v) for v in axis["dir"]]}
                     if axis and axis.get("origin") is not None and axis.get("dir") is not None else None),
            "limits": {str(k): [float(x) for x in v] for k, v in (limits or {}).items()}}


def closure(name, kind, a, b, *, a_id=None, b_id=None, axis=None):
    """A LOOP-CLOSING constraint between occurrences `a` and `b` (labels; `*_id` resolved ids), which
    must already be connected through the mate tree. kind: revolute (a and b share an axis), spherical
    (share the point axis.origin; dir ignored), fastened (their relative placement is frozen at the
    authored pose; axis None). `axis` is in assembly coordinates at the authored pose, like a mate's."""
    if kind not in CLOSURE_KINDS:
        raise ValueError(f"closure {name!r}: kind must be one of {CLOSURE_KINDS}, got {kind!r}")
    if kind != "fastened" and not (axis and axis.get("origin") is not None):
        raise ValueError(f"closure {name!r}: a {kind} closure needs axis.origin")
    if kind == "revolute" and axis.get("dir") is None:
        raise ValueError(f"closure {name!r}: a revolute closure needs axis.dir")
    return {"name": name, "kind": kind, "a": a, "b": b, "a_id": a_id, "b_id": b_id,
            "axis": ({"origin": [float(v) for v in axis["origin"]],
                      "dir": [float(v) for v in axis["dir"]] if axis.get("dir") is not None else None}
                     if axis else None)}


def coupling(name, gears, limits=None):
    """A gear ratio tying several mate DOFs to one driver (cadgen `couple`): gears = {dof: ratio}."""
    return {"name": name, "gears": {str(k): float(v) for k, v in dict(gears).items()},
            "limits": [float(x) for x in limits] if limits else None}


def assembly(name, occurrences, parts=None, mates=(), couplings=(), poses=None, provenance=None,
             closures=()):
    """The assembly spec. `parts` maps a part key (usually the occurrence label) -> single-part IR."""
    return {"kind": "assembly", "name": name, "occurrences": list(occurrences),
            "parts": dict(parts or {}), "mates": list(mates), "couplings": list(couplings),
            "closures": list(closures),
            "poses": {str(k): {str(d): float(v) for d, v in dict(vals).items()}
                      for k, vals in (poses or {}).items()},
            "provenance": dict(provenance or {})}


def mate_tree_problems(asm):
    """The structural rules a mate graph must satisfy (the same ones cadgen enforces, so what we
    ingest is what it would accept back): every child has at most ONE parent mate, no self-mates, no
    cycles (closed-loop linkages need a solver; this is a forward-kinematic tree). Returns a list of
    human-readable problems — empty means well-formed."""
    problems, parent_of = [], {}
    for m in asm["mates"]:
        c, p = m.get("child_id") or m["child"], m.get("parent_id") or m["parent"]
        if c == p:
            problems.append(f"mate {m['name']!r} mates {c!r} to itself")
        if c in parent_of:
            problems.append(f"occurrence {c!r} has more than one parent mate")
        parent_of[c] = p
    for start in parent_of:
        node, hops = start, 0
        while node in parent_of:
            node, hops = parent_of[node], hops + 1
            if node == start or hops > len(parent_of):
                problems.append(f"mate graph has a cycle through {start!r}")
                break
    return problems


# --- tree + loop helpers ------------------------------------------------------------------------
def occurrence_key(asm, ref):
    """Resolve a mate/closure endpoint (an id OR a label) to the occurrence's id."""
    for o in asm["occurrences"]:
        if ref in (o["id"], o["label"]):
            return o["id"]
    return None


def _endpoint(asm, m, side):
    """A mate's parent/child or a closure's a/b, preferring the resolved id."""
    return occurrence_key(asm, m.get(f"{side}_id") or m[side])


def parent_map(asm):
    """child occurrence id -> (parent occurrence id, mate) over the mate TREE."""
    return {_endpoint(asm, m, "child"): (_endpoint(asm, m, "parent"), m) for m in asm["mates"]}


def tree_path(asm, a, b):
    """The mates on the tree path between occurrence ids a and b, or None if they are in different
    trees. Order: a up to the common ancestor, then down to b."""
    pm = parent_map(asm)

    def up(n):
        chain = [(n, None)]
        while n in pm:
            p, m = pm[n]
            chain.append((p, m))
            n = p
        return chain
    ua, ub = up(a), up(b)
    ids_b = [n for n, _ in ub]
    for i, (n, _) in enumerate(ua):
        if n in ids_b:
            j = ids_b.index(n)
            return [m for _, m in ua[1:i + 1]] + [m for _, m in ub[1:j + 1]]
    return None


def _parallel(u, v, tol=1e-6):
    import math
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    return math.sqrt(sum(x * x for x in cross)) <= tol * nu * nv


def _perpendicular(u, v, tol=1e-6):
    import math
    return abs(sum(a * b for a, b in zip(u, v))) <= tol * math.sqrt(sum(x * x for x in u)) * math.sqrt(sum(x * x for x in v))


def closure_is_planar(asm, c):
    """True when the loop a revolute closure makes is a PLANAR mechanism: every revolute on the tree
    path is parallel to the closure axis and every slider perpendicular to it. Planar loops need only
    2 closure constraints (not 5) — the spatial count over-constrains them."""
    if c["kind"] != "revolute":
        return False
    path = tree_path(asm, _endpoint(asm, c, "a"), _endpoint(asm, c, "b")) or []
    d = c["axis"]["dir"]
    for m in path:
        if m["kind"] == "fastened":
            continue
        if m["kind"] == "revolute" and not _parallel(m["axis"]["dir"], d):
            return False
        if m["kind"] == "slider" and not _perpendicular(m["axis"]["dir"], d):
            return False
        if m["kind"] == "cylindrical":
            return False
    return True


def closure_problems(asm):
    """Rules a closure must satisfy: both endpoints exist, differ, and are already joined by the mate
    tree (a closure between separate trees is just a mate), and the loop contains at least one moving
    mate. Empty list = well-formed."""
    problems = []
    for c in asm.get("closures", []):
        a, b = _endpoint(asm, c, "a"), _endpoint(asm, c, "b")
        if a is None or b is None:
            problems.append(f"closure {c['name']!r}: unknown occurrence {c['a'] if a is None else c['b']!r}")
            continue
        if a == b:
            problems.append(f"closure {c['name']!r} closes {a!r} on itself")
            continue
        path = tree_path(asm, a, b)
        if path is None:
            problems.append(f"closure {c['name']!r}: {a!r} and {b!r} are in separate trees — make it a mate")
        elif all(m["kind"] == "fastened" for m in path):
            problems.append(f"closure {c['name']!r}: the loop through {a!r}..{b!r} has no moving mate")
    return problems


def mobility(asm):
    """Degrees of freedom of the mechanism (fixed roots): tree DOF minus closure constraints, with
    planar revolute loops counted as 2 constraints (spatial Grubler would call a four-bar -2)."""
    tree_dof = sum(MATE_DOF[m["kind"]] for m in asm["mates"])
    cons, planar = 0, []
    for c in asm.get("closures", []):
        if closure_is_planar(asm, c):
            cons += 2
            planar.append(c["name"])
        else:
            cons += CLOSURE_CONSTRAINTS[c["kind"]]
    return {"tree_dof": tree_dof, "closure_constraints": cons, "mobility": tree_dof - cons,
            "planar_closures": planar}
