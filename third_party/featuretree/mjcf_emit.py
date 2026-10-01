"""mjcf_emit.py — emit a MuJoCo model (MJCF) from an ASSEMBLY IR. No geometry is re-described.

The assembly IR already says everything a rigid-body sim needs, so the MJCF is DERIVED from it:
  * bodies     = connected components of `fastened` mates (one rigid body per welded group)
  * joints     = the remaining mates (revolute -> hinge, slider -> slide, cylindrical -> both), with
                 the mate's axis and limits
  * loops      = `closures` -> <equality>: revolute -> connect (one if the loop is planar, two along
                 the axis if spatial), spherical -> connect, fastened -> weld
  * geometry   = each occurrence's OWN mesh: built from its part IR (b3d_emit) or supplied as an STL
                 (bought parts, parts not yet in IR). Visual and contact share it.
  * mass       = per-occurrence mass if given (kg), else density x the mesh's exact volume; MuJoCo
                 integrates the inertia from the real mesh (`inertia="exact"`), never a box guess.

Sim-only facts that are not design facts ride in a small `sim` dict, never in the IR:
    meshes     {occ: stl path, part-local mm}      geometry for occurrences without a part IR
    mass_kg    {occ: kg}                           bought parts / measured masses
    density    kg/m^3 (default 1250)               for everything else
    collision  {occ: "hull" | "none" | [stl, ..]}  default "hull" (MuJoCo collides a mesh's convex
                                                   hull — a concave part lists convex PROXY meshes,
                                                   authored by the part's own code)
    friction   {occ: mu}
    rgba       {occ: "r g b a"}                    else the occurrence colour
    actuators  [{"mate": name, "kind": "position"|"motor"|"velocity", **attrs}]
    joint_attrs {mate: {attr: value}}              e.g. gearbox frictionloss, damping, armature
    free_root  bool — root bodies get a freejoint (a robot) instead of being fixed (a fixture)

Units: IR millimetres -> MJCF metres. Keys in `sim` dicts may be occurrence ids or labels.

    xml = emit(asm, "out/model_dir", sim)            # complete <mujoco> model + meshes/
    frag = emit_fragment(asm, "out/dir", sim, prefix="A_", root_pos=(0,0,0), root_yaw_deg=0)
"""
from __future__ import annotations

import math
import os
import re

import numpy as np

import assembly_ir as A
import assembly_kin as K

MM = 1e-3


def _san(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s))


def _quat(R):
    """3x3 rotation -> MuJoCo quaternion (w x y z)."""
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return (0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s)
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(1.0 + R[i, i] - R[j, j] - R[k, k]) * 2
    q = [0.0] * 4
    q[0] = (R[k, j] - R[j, k]) / s
    q[i + 1] = 0.25 * s
    q[j + 1] = (R[j, i] + R[i, j]) / s
    q[k + 1] = (R[k, i] + R[i, k]) / s
    return tuple(q)


def _f(v):
    return " ".join(f"{x:.6g}" for x in v)


def _lookup(d, occ):
    if not d:
        return None
    for k in (occ["id"], occ["label"]):
        if k in d:
            return d[k]
    return None


def rigid_groups(asm):
    """Union-find over fastened mates -> {occurrence id: group root id}, groups in occurrence order."""
    parent = {o["id"]: o["id"] for o in asm["occurrences"]}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    order = {o["id"]: i for i, o in enumerate(asm["occurrences"])}
    for m in asm["mates"]:
        if m["kind"] == "fastened":
            a, b = find(A._endpoint(asm, m, "parent")), find(A._endpoint(asm, m, "child"))
            if a != b:
                keep, drop = (a, b) if order[a] < order[b] else (b, a)
                parent[drop] = keep
    return {o["id"]: find(o["id"]) for o in asm["occurrences"]}


def _mesh_for(occ, out_dir, sim):
    """STL path (part-local mm) for an occurrence: sim override, else built from its part IR."""
    p = _lookup(sim.get("meshes"), occ)
    if p:
        return os.path.abspath(p)
    if occ.get("part"):
        from build123d import export_stl
        import b3d_emit
        solid, _ = b3d_emit.emit(occ["part"])
        os.makedirs(os.path.join(out_dir, "meshes"), exist_ok=True)
        path = os.path.abspath(os.path.join(out_dir, "meshes", f"{_san(occ['id'])}_{_san(occ['label'])}.stl"))
        export_stl(solid, path)
        return path
    return None


def emit_fragment(asm, out_dir, sim=None, prefix="", root_pos=(0.0, 0.0, 0.0), root_yaw_deg=0.0):
    """-> dict(asset, body, equality, actuator, contact_exclude, bodies) XML fragments, names prefixed,
    so several instances (two robots in one arena) compose into one model."""
    sim = sim or {}
    probs = A.mate_tree_problems(asm) + A.closure_problems(asm)
    if probs:
        raise ValueError("assembly IR is not emit-ready: " + "; ".join(probs))
    occs = {o["id"]: o for o in asm["occurrences"]}
    group = rigid_groups(asm)
    members = {}
    for oid, g in group.items():
        members.setdefault(g, []).append(oid)

    # the moving mate INTO each group (at most one: mate tree), and the group tree
    joint_in, parent_group = {}, {}
    for m in asm["mates"]:
        if m["kind"] == "fastened":
            continue
        cg, pg = group[A._endpoint(asm, m, "child")], group[A._endpoint(asm, m, "parent")]
        if cg in joint_in:
            raise ValueError(f"rigid group of {cg!r} has two moving parent mates")
        joint_in[cg], parent_group[cg] = m, pg

    # body frame per group: world-aligned, origin at its joint's axis origin (roots: assembly origin)
    anchor = {g: (np.asarray(joint_in[g]["axis"]["origin"], float) if g in joint_in else np.zeros(3))
              for g in members}
    names, used = {}, set()
    for g in members:
        n = prefix + _san(occs[g]["label"])
        while n in used:
            n += "_"
        used.add(n)
        names[g] = n

    assets, eqs, acts = [], [], []
    density = sim.get("density", 1250.0)

    def geoms_for(g):
        out = []
        for oid in members[g]:
            o = occs[oid]
            path = _mesh_for(o, out_dir, sim)
            if not path:
                continue
            T = K.mat4(o.get("transform"))
            pos = (T[:3, 3] - anchor[g]) * MM
            quat = _quat(T[:3, :3])
            mname = f"{prefix}{_san(oid)}_{_san(o['label'])}"
            assets.append(f'<mesh name="{mname}" file="{path}" scale="{MM} {MM} {MM}" inertia="exact"/>')
            coll = _lookup(sim.get("collision"), o) or "hull"
            mass = _lookup(sim.get("mass_kg"), o)
            mu = _lookup(sim.get("friction"), o)
            rgba = _lookup(sim.get("rgba"), o) or (_f(list(o["color"][:3]) + [1.0]) if o.get("color") else "0.7 0.7 0.7 1")
            inert = f'mass="{mass:.6g}"' if mass is not None else f'density="{density:.6g}"'
            contact = "" if coll == "hull" else ' contype="0" conaffinity="0"'
            fr = f' friction="{mu} 0.005 0.0001"' if mu is not None else ""
            out.append(f'<geom name="{mname}" type="mesh" mesh="{mname}" pos="{_f(pos)}" quat="{_f(quat)}" '
                       f'{inert} rgba="{rgba}"{contact}{fr}/>')
            if isinstance(coll, (list, tuple)):
                for i, pp in enumerate(coll):
                    pn = f"{mname}_proxy{i}"
                    assets.append(f'<mesh name="{pn}" file="{os.path.abspath(pp)}" scale="{MM} {MM} {MM}"/>')
                    out.append(f'<geom name="{pn}" type="mesh" mesh="{pn}" pos="{_f(pos)}" quat="{_f(quat)}" '
                               f'density="0" group="3" rgba="1 0 1 0.3"{fr}/>')
        return out

    def joints_for(g):
        m = joint_in.get(g)
        if not m:
            return []
        d = np.asarray(m["axis"]["dir"], float)
        d = d / np.linalg.norm(d)
        lim = (m.get("limits") or {}).get("value")
        ja = "".join(f' {k}="{v}"' for k, v in ((sim.get("joint_attrs") or {}).get(m["name"]) or {}).items())
        js = []
        if m["kind"] in ("revolute", "cylindrical"):
            rng = f' range="{lim[0]:.6g} {lim[1]:.6g}"' if lim and m["kind"] == "revolute" else ""
            js.append(f'<joint name="{prefix}{_san(m["name"])}" type="hinge" axis="{_f(d)}"{rng}{ja}/>')
        if m["kind"] in ("slider", "cylindrical"):
            rng = f' range="{lim[0] * MM:.6g} {lim[1] * MM:.6g}"' if lim and m["kind"] == "slider" else ""
            sfx = "_slide" if m["kind"] == "cylindrical" else ""
            js.append(f'<joint name="{prefix}{_san(m["name"])}{sfx}" type="slide" axis="{_f(d)}"{rng}{ja}/>')
        return js

    def body_xml(g, depth):
        pad = "  " * depth
        if g in parent_group:
            pos = (anchor[g] - anchor[parent_group[g]]) * MM
            head = f'{pad}<body name="{names[g]}" pos="{_f(pos)}">'
            extra = []
        else:
            yaw = math.radians(root_yaw_deg)
            q = (math.cos(yaw / 2), 0, 0, math.sin(yaw / 2))
            head = f'{pad}<body name="{names[g]}" pos="{_f(root_pos)}" quat="{_f(q)}">'
            extra = [f'<freejoint name="{names[g]}_free"/>'] if sim.get("free_root") else []
        inner = extra + joints_for(g) + geoms_for(g)
        kids = [body_xml(c, depth + 1) for c in members if parent_group.get(c) == g]
        return "\n".join([head] + [f"{pad}  {x}" for x in inner] + kids + [f"{pad}</body>"])

    roots = [g for g in members if g not in parent_group]
    body = "\n".join(body_xml(g, 2) for g in roots)

    for c in asm.get("closures", []):
        ga, gb = group[A._endpoint(asm, c, "a")], group[A._endpoint(asm, c, "b")]
        if ga == gb:
            raise ValueError(f"closure {c['name']!r} joins two occurrences of one rigid group")
        cn = f"{prefix}{_san(c['name'])}"
        if c["kind"] == "fastened":
            eqs.append(f'<weld name="{cn}" body1="{names[ga]}" body2="{names[gb]}"/>')
            continue
        o = np.asarray(c["axis"]["origin"], float)
        pts = [o]
        if c["kind"] == "revolute" and not A.closure_is_planar(asm, c):
            d = np.asarray(c["axis"]["dir"], float)
            pts.append(o + K.FEATURE_MM * d / np.linalg.norm(d))
        for i, p in enumerate(pts):
            eqs.append(f'<connect name="{cn}{"_" + str(i) if i else ""}" body1="{names[ga]}" body2="{names[gb]}" '
                       f'anchor="{_f((p - anchor[ga]) * MM)}"/>')

    for a in sim.get("actuators", []):
        attrs = " ".join(f'{k}="{v}"' for k, v in a.items() if k not in ("mate", "kind", "name"))
        nm = a.get("name", f"{a['mate']}_{a['kind']}")
        acts.append(f'<{a["kind"]} name="{prefix}{_san(nm)}" joint="{prefix}{_san(a["mate"])}" {attrs}/>')

    return {"asset": "\n    ".join(dict.fromkeys(assets)), "body": body, "equality": "\n    ".join(eqs),
            "actuator": "\n    ".join(acts), "bodies": {oid: names[g] for oid, g in group.items()}}


def model(fragments, name="assembly", timestep=0.001, floor=False, extra_worldbody=""):
    """Wrap one or more fragments into a complete <mujoco> document."""
    fl = '<geom name="floor" type="plane" size="1 1 0.01" rgba=".8 .8 .8 1"/>' if floor else ""
    j = lambda k: "\n    ".join(f[k] for f in fragments if f[k])
    return f"""<mujoco model="{_san(name)}">
  <compiler angle="degree" autolimits="true"/>
  <option timestep="{timestep}" integrator="implicitfast"/>
  <asset>
    {j("asset")}
  </asset>
  <worldbody>
    {fl}
    {extra_worldbody}
{j("body")}
  </worldbody>
  <equality>
    {j("equality")}
  </equality>
  <actuator>
    {j("actuator")}
  </actuator>
</mujoco>"""


def emit(asm, out_dir, sim=None, **model_kw):
    """Complete MJCF for one assembly; writes meshes under out_dir/meshes. Returns the XML string."""
    os.makedirs(out_dir, exist_ok=True)
    return model([emit_fragment(asm, out_dir, sim)], name=asm["name"], **model_kw)
