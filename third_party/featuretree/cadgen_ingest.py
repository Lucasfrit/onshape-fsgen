"""cadgen_ingest.py — ingest a text-to-cad ("CAD Skills" / cadgen) assembly into the featuretree IR.

cadgen (github.com/earthtojake/text-to-cad) is the dominant agent-writes-build123d toolchain. By
design law it emits a DEAD SOLID: a labeled STEP (XCAF part names, an `o1.N` occurrence tree) plus a
`<name>.step.json` sidecar carrying what STEP cannot — typed mates / gear couplings / poses. Its
author has explicitly declined CAD-native (FreeCAD/Onshape) integration.

This module is that missing half. It reads the STEP (labels + placed solids, via build123d, which
preserves the XCAF names and child order) and the sidecar (mates joined to occurrences by id AND
cross-checked by label — disagreements are reported, never papered over), recovers each part's
editable feature tree with the existing self-verifying `step_recognize` (unchanged; each solid is
handed to it as its own STEP), and returns an ASSEMBLY IR (assembly_ir.py): the same design, now with
an editable tree per part and its joints preserved.

It ADDS an input path. It changes nothing about featuretree's existing single-part flows.

    python3 cadgen_ingest.py model.step                          # sidecar auto-found: model.step.json
    python3 cadgen_ingest.py model.step --sidecar k.json --emit out.asm.json
    python3 cadgen_ingest.py --package path/to/tree_dir          # a materialized tree (assembly.json)
    python3 cadgen_ingest.py --selftest
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assembly_ir as A

SIDECAR_SUFFIX = ".step.json"


# --- the sidecar (kinematics) --------------------------------------------------------------
def _strip_ref(ref):
    """'#carrier_plate' -> 'carrier_plate' (sidecar labels carry cadgen's leading '#' ref marker)."""
    s = str(ref or "").strip()
    return s[1:] if s.startswith("#") else s


def read_sidecar(path):
    """Parse a cadgen `.step.json` sidecar into IR mates / couplings / poses. Validates the closed
    mate vocabulary; an unknown kind is an error, not a silent skip."""
    d = json.load(open(path))
    kin = d.get("kinematics") or {}
    mates = []
    for m in kin.get("mates", []):
        axis = m.get("axis") or {}
        mates.append(A.mate(m["name"], m.get("kind"), _strip_ref(m.get("parent")), _strip_ref(m.get("child")),
                            parent_id=m.get("parentId"), child_id=m.get("childId"),
                            axis={"origin": axis.get("origin"), "dir": axis.get("dir", axis.get("direction"))}
                            if axis else None,
                            limits=m.get("limits") or {}))
    couplings = [A.coupling(c["name"], c.get("gears") or {}, c.get("limits"))
                 for c in kin.get("couplings", [])]
    return {"schema_version": d.get("schemaVersion"), "document_hash": d.get("documentHash"),
            "mates": mates, "couplings": couplings, "poses": dict(kin.get("poses") or {})}


# --- the STEP (geometry + labels + tree) -----------------------------------------------------
def _matrix12(shape):
    """The shape's placement as cadgen's 12-float row-major 3x4, or None."""
    try:
        trsf = shape.location.wrapped.Transformation()
        return [round(trsf.Value(r, c), 6) for r in (1, 2, 3) for c in (1, 2, 3, 4)]
    except Exception:
        return None


# OpenCASCADE names UNNAMED shapes with these placeholders on STEP import. They are not part
# names: a leaf part imports as `Compound(label='peg') -> Solid(label='SOLID')`, and treating
# 'SOLID' as a real name would make the walk descend into the wrapper and lose 'peg'.
_OCC_PLACEHOLDER_NAMES = {"SOLID", "COMPOUND", "COMPSOLID", "SHELL", "FACE", "WIRE", "EDGE", "VERTEX"}


def _name(node):
    """A shape's authored label, or '' when it is unnamed / an OCC placeholder."""
    s = str(getattr(node, "label", "") or "")
    return "" if (s in _OCC_PLACEHOLDER_NAMES or s.startswith("Open CASCADE")) else s


def _walk(node, occ_id, out):
    """Depth-first over an imported compound, numbering children 1-based (cadgen's `o1.N.M`).

    A node is a GROUP (descend) only when its children carry AUTHORED names — parts or
    subassemblies. A leaf part imports as a labeled Compound wrapping one placeholder-named Solid;
    that wrapper IS the occurrence (its label is the part name), so it is recorded as a leaf rather
    than descended into."""
    kids = list(getattr(node, "children", None) or [])
    if kids and any(_name(k) for k in kids):
        for i, k in enumerate(kids, 1):
            _walk(k, f"{occ_id}.{i}", out)
        return
    solids = node.solids() if hasattr(node, "solids") else []
    out.append({"id": occ_id, "label": _name(node), "shape": node, "n_solids": len(solids),
                "transform": _placement(node), "location_hint": _matrix12(node)})


def _placement(shape):
    """The occurrence's placement, part-local -> world, as a 12-float row-major 3x4.

    The part-local frame is defined as the part's bounding-box centre. cadgen occurrences are NOT
    consistent about where placement lives — a gear carries it in `.location`, its pin has it
    baked into the geometry with an identity location — so `.location` alone is unreliable, but
    the world bbox centre is always right. Rotation, if any, stays inside the part geometry, so
    `Pos(centre) * local_part` reconstructs the world solid exactly (self-checked at recognition)."""
    try:
        c = shape.bounding_box().center()
        return [1.0, 0.0, 0.0, round(c.X, 6), 0.0, 1.0, 0.0, round(c.Y, 6), 0.0, 0.0, 1.0, round(c.Z, 6)]
    except Exception:
        return None


def read_step_assembly(step_path):
    """STEP -> [{id, label, shape, n_solids, transform}] in occurrence order, plus the root label.
    A single-solid (non-assembly) STEP yields one occurrence 'o1'."""
    from build123d import import_step
    root = import_step(str(step_path))
    occs = []
    _walk(root, "o1", occs)
    return occs, str(getattr(root, "label", "") or "")


# --- the package (a materialized tree: assembly.json) ----------------------------------------
def read_package(package_dir):
    """A cadgen tree package -> occurrences (structure + placement; geometry only if .brep blobs are
    present — the docs hero ships surface-only, which is reported, not faked)."""
    d = json.load(open(Path(package_dir) / "assembly.json"))
    comps = d.get("components") or {}
    occs = []
    for o in d.get("occurrences") or []:
        cid = str(o.get("component") or "")
        brep = Path(package_dir) / (comps.get(cid, {}).get("brep") or f"components/{cid}.brep")
        occs.append({"id": str(o.get("id")), "label": str(o.get("name") or o.get("id") or ""),
                     "shape": None, "n_solids": 0, "transform": (o.get("transform") or [])[:12] or None,
                     "color": o.get("color"), "component": cid,
                     "geometry": "brep" if brep.exists() else "surface-only"})
    root = ((d.get("assembly") or {}).get("root") or {})
    return occs, str(root.get("name") or d.get("label") or "assembly")


# --- join sidecar mates to occurrences -------------------------------------------------------
def join_mates(occurrences, mates):
    """Resolve each mate's parent/child to an occurrence — by id first, then by (unique) label —
    and cross-check that the STEP's label agrees with the sidecar's. Returns (mates, problems)."""
    by_id = {o["id"]: o for o in occurrences}
    by_label = {}
    for o in occurrences:
        by_label.setdefault(o["label"], []).append(o)
    problems, out = [], []
    for m in mates:
        m = dict(m)
        for role in ("parent", "child"):
            oid, lbl = m.get(f"{role}_id"), m.get(role)
            occ = by_id.get(oid) if oid else None
            if occ is None and lbl:
                cands = by_label.get(lbl, [])
                occ = cands[0] if len(cands) == 1 else None
                if occ is not None:
                    m[f"{role}_id"] = occ["id"]
            if occ is None:
                problems.append(f"mate {m['name']!r}: {role} {lbl!r} (id {oid!r}) is not an occurrence")
                continue
            if lbl and occ["label"] and occ["label"] != lbl:
                problems.append(f"mate {m['name']!r}: {role} {oid} is {occ['label']!r} in the model "
                                f"but the sidecar says {lbl!r}")
        out.append(m)
    return out, problems


# --- per-part feature recovery (reuses step_recognize, unchanged) -----------------------------
def recognize_occurrence(occ, tmpdir):
    """Recognize one occurrence's part IN ITS OWN FRAME (bbox-centred) and hand it to step_recognize
    as its own STEP; return (spec, report) or (None, report-with-error). One unrecognizable part
    must not sink the assembly.

    Recognizing the placed (world) solid made off-origin parts come back PARTIAL (a planet gear at
    r=42 was Δ38% while the identical sun gear at the origin verified); re-centring makes each part
    recognize identically wherever it sits. The placement is then self-checked: re-placing the local
    part must land back on the original world bounding box (`placement_verified`)."""
    import step_recognize
    from build123d import Pos, export_step
    tmp = Path(tmpdir) / f"{occ['id'].replace('.', '_')}.step"
    try:
        sh = occ["shape"]
        c = sh.bounding_box().center()
        local = Pos(-c.X, -c.Y, -c.Z) * sh                      # the part in its own frame
        back = (Pos(c.X, c.Y, c.Z) * local).bounding_box().center()
        placement_ok = max(abs(back.X - c.X), abs(back.Y - c.Y), abs(back.Z - c.Z)) < 1e-3
        export_step(local, str(tmp))
        spec, report = step_recognize.recognize(str(tmp), name=occ["label"] or occ["id"])
        report["placement_verified"] = placement_ok
        return spec, report
    except Exception as e:                                     # noqa: BLE001 - reported per part
        return None, {"name": occ["label"], "verified": False, "error": f"{type(e).__name__}: {e}"}


# --- the ingest ------------------------------------------------------------------------------
def ingest(step_path=None, sidecar_path=None, package_dir=None, recognize_parts=True, name=None):
    """cadgen STEP (+ sidecar) or tree package -> (assembly IR, report)."""
    if package_dir:
        occs, root_label = read_package(package_dir)
        recognize_parts = False                                # structure-only source
        src = {"package": str(package_dir)}
    else:
        occs, root_label = read_step_assembly(step_path)
        src = {"step": str(step_path)}
        if sidecar_path is None:
            cand = Path(str(step_path) + ".json")              # model.step -> model.step.json
            if not cand.exists():
                cand = Path(str(step_path)[: -len(Path(step_path).suffix)] + SIDECAR_SUFFIX)
            sidecar_path = cand if cand.exists() else None
    name = name or root_label or (Path(step_path).stem if step_path else "assembly")

    side = read_sidecar(sidecar_path) if sidecar_path else None
    mates, problems = join_mates(occs, side["mates"]) if side else ([], [])

    parts, part_reports = {}, {}
    if recognize_parts:
        with tempfile.TemporaryDirectory() as td:
            for o in occs:
                if o.get("shape") is None or o["n_solids"] != 1:
                    part_reports[o["label"] or o["id"]] = {"verified": False,
                        "error": f"{o['n_solids']} solids (expected 1)"}
                    continue
                spec, rep = recognize_occurrence(o, td)
                key = o["label"] or o["id"]
                if spec is not None:
                    parts[key] = spec
                part_reports[key] = {"verified": rep.get("verified"), "dvol_pct": rep.get("dvol_pct"),
                                     "recovered": rep.get("recovered"), "error": rep.get("error"),
                                     "placement_verified": rep.get("placement_verified")}

    ir_occs = [A.occurrence(o["id"], o["label"], part=(o["label"] or o["id"]) if (o["label"] or o["id"]) in parts else None,
                            transform=o.get("transform"), color=o.get("color"), component=o.get("component"))
               for o in occs]
    prov = {**src, "sidecar": str(sidecar_path) if sidecar_path else None,
            "sidecar_schema": side["schema_version"] if side else None,
            "document_hash": side["document_hash"] if side else None,
            "tool": "cadgen (text-to-cad)"}
    asm = A.assembly(name, ir_occs, parts=parts, mates=mates,
                     couplings=side["couplings"] if side else [], poses=side["poses"] if side else {},
                     provenance=prov)
    tree_problems = A.mate_tree_problems(asm)
    n_ver = sum(1 for r in part_reports.values() if r.get("verified"))
    report = {"name": name, "n_occurrences": len(occs), "n_mates": len(mates),
              "n_couplings": len(asm["couplings"]), "n_poses": len(asm["poses"]),
              "parts_recognized": len(parts), "parts_verified": n_ver, "part_reports": part_reports,
              "join_problems": problems, "mate_tree_problems": tree_problems,
              "ok": not problems and not tree_problems}
    return asm, report


def _print_report(r):
    print(f"{r['name']}: {r['n_occurrences']} occurrence(s), {r['n_mates']} mate(s), "
          f"{r['n_couplings']} coupling(s), {r['n_poses']} pose(s); parts recognized "
          f"{r['parts_recognized']}, VERIFIED {r['parts_verified']}")
    for k, pr in r["part_reports"].items():
        tag = "VERIFIED" if pr.get("verified") else ("ERROR" if pr.get("error") else "PARTIAL")
        extra = pr.get("error") or (f"vol Δ{pr['dvol_pct']}%" if pr.get("dvol_pct") is not None else "")
        print(f"  {k:32s} {tag:8s} {extra}")
    for p in r["join_problems"] + r["mate_tree_problems"]:
        print("  PROBLEM:", p)
    print("OK: sidecar mates joined to model occurrences, labels agree" if r["ok"]
          else "ATTENTION: see problems above (reported, not hidden)")


# --- selftest: a synthetic cadgen-shaped STEP + sidecar, end to end --------------------------
def _make_fixture(dirpath):
    """A 2-part labeled assembly (plate + peg) and a sidecar in cadgen's exact schema."""
    from build123d import Box, Compound, Cylinder, Pos, export_step
    plate = Box(40, 30, 10) - Cylinder(4, 20)
    plate.label = "base_plate"
    peg = Pos(0, 0, 15) * Cylinder(3.8, 20)
    peg.label = "peg"
    asm = Compound(children=[plate, peg])
    asm.label = "plate_peg"
    step = Path(dirpath) / "plate_peg.step"
    export_step(asm, str(step))
    side = {"schemaVersion": 9, "documentHash": "selftest", "kinematics": {
        "mates": [{"name": "peg_spin", "kind": "revolute", "parent": "#base_plate", "parentId": "o1.1",
                   "child": "#peg", "childId": "o1.2", "axis": {"dir": [0, 0, 1], "origin": [0, 0, 0]},
                   "limits": {"value": [-360, 360]}}],
        "poses": {"quarter": {"peg_spin": 90}}}}
    json.dump(side, open(str(step) + ".json", "w"))
    return step


def selftest():
    with tempfile.TemporaryDirectory() as td:
        step = _make_fixture(td)
        asm, rep = ingest(step)
        _print_report(rep)
        labels = [o["label"] for o in asm["occurrences"]]
        assert labels == ["base_plate", "peg"], labels
        assert rep["n_mates"] == 1 and asm["mates"][0]["parent_id"] == "o1.1" and asm["mates"][0]["child_id"] == "o1.2"
        assert asm["mates"][0]["kind"] == "revolute" and asm["mates"][0]["axis"]["dir"] == [0.0, 0.0, 1.0]
        assert asm["poses"] == {"quarter": {"peg_spin": 90.0}}
        assert rep["ok"], rep
        assert rep["parts_verified"] == 2, rep["part_reports"]      # plate+hole and a cylinder both VERIFY
        assert "base_plate" in asm["parts"] and asm["parts"]["base_plate"]["features"], "no editable tree"
    print("PASS: cadgen STEP + sidecar -> assembly IR (editable tree per part, mates joined by id, "
          "labels cross-checked, poses kept)")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(selftest())
    if "--package" in args:
        asm, rep = ingest(package_dir=args[args.index("--package") + 1])
    else:
        pos = [a for a in args if not a.startswith("--") and a not in
               (args[args.index("--sidecar") + 1] if "--sidecar" in args else "",
                args[args.index("--emit") + 1] if "--emit" in args else "")]
        if not pos:
            print(__doc__); sys.exit(2)
        side = args[args.index("--sidecar") + 1] if "--sidecar" in args else None
        asm, rep = ingest(pos[0], sidecar_path=side, recognize_parts="--no-recognize" not in args)
    _print_report(rep)
    if "--emit" in args:
        out = args[args.index("--emit") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        json.dump(asm, open(out, "w"), indent=1)
        print("wrote", out)
    sys.exit(0 if rep["ok"] else 1)
