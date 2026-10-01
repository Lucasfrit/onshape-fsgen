#!/usr/bin/env python3
"""cad_verify.py — check a STEP exported by a CAD target against the IR that generated it.

The Fusion and SolidWorks backends run inside those programs, where this repo cannot test them,
so each generated script exports <part>.<target>.step and a per-feature report. This closes the
loop with the same evidence standard as the recogniser: build the IR with build123d (the
reference backend), then take the Boolean-free IoU of the target's solid against it.

    python cad_verify.py <part>.fusion.step <part>.ir.json [--report <part>.fusion.report.json]
    python cad_verify.py <part>.SLDPRT <part>.ir.json      # a NATIVE file: geometry AND tree
    python cad_verify.py <part>.f3d <part>.ir.json
    python cad_verify.py <part>.SLDPRT <part>.ir.json --onshape   # + Onshape as a 2nd decoder
    python cad_verify.py <part>.SLDPRT --decoders-only            # no IR: do the decoders agree?

A native .sldprt / .f3d is read with cadmpeg (github.com/cadmpeg/cadmpeg, Apache-2.0; set
CADMPEG or put it on PATH). That checks two things a STEP cannot: the geometry cadmpeg decodes
from the saved file goes through the same IoU gate, AND the file's stored feature history is
matched to the IR by feature name — each IR feature must exist as a native feature of the right
kind carrying the right driving value (blind length, fillet radius). That is the editability
claim itself: the part opens as named, dimensioned operations, not a dead solid.

--onshape also imports the native file into Onshape (licensed commercial translators, see
onshape_translate.py). When Onshape is used, ITS STEP decides the geometry verdict and cadmpeg's is
only reported. On NIST's eleven native SolidWorks 2018 test parts, Onshape's STEP matched NIST's
own STEP on all eleven (IoU >= 99.93%, volume within 0.17%), while cadmpeg 0.6.0's matched on one
(the others 0-81% IoU, invalid solids). cadmpeg's FEATURE decoding is still used for the tree
check. Without --onshape the geometry verdict rests on cadmpeg alone, and a MISMATCH may be a
decoder failure rather than a wrong part: rerun with --onshape before believing it.
Free Onshape plans make the upload PUBLIC.

VERIFIED needs volume within 0.5% and IoU >= 99.5% in the IR's own frame (the targets build in
it, so no registration search is needed). The report pinpoints the first feature whose volume
left the reference, which is where to look when a target disagrees.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VOL_TOL, IOU_TOL = 0.005, 0.995
NATIVE = (".sldprt", ".f3d")
# IR kind -> (cadmpeg neutral definition, allowed Boolean ops)
KIND = {"pad": ("extrude", {"join", "new_body"}), "pocket": ("extrude", {"cut"}),
        "prism_cut": ("extrude", {"cut"}), "revolve": ("revolve", {"join", "new_body"}),
        "fillet": ("fillet", None)}


def cadmpeg(*args):
    import os
    import shutil
    import subprocess
    exe = os.environ.get("CADMPEG") or shutil.which("cadmpeg")
    if not exe:
        raise SystemExit("cadmpeg not found: build github.com/cadmpeg/cadmpeg, set CADMPEG or PATH")
    return subprocess.run([exe, *map(str, args)], capture_output=True, text=True)


def native_step(path):
    """Decode a native file's geometry to STEP with cadmpeg. Its model check is strict (it flags
    unsolved procedural carriers even when the solved solids are valid), so errors are allowed
    through and counted; the IoU gate is what decides."""
    out = Path(path).with_suffix(Path(path).suffix + ".cadmpeg.step")
    r = cadmpeg("convert", path, "-o", out, "--allow-errors")
    if not out.exists():
        raise SystemExit(f"cadmpeg could not convert {path}:\n{r.stderr[-1500:]}")
    return out, r.stderr.count("[error/")


def native_features(path):
    out = Path(path).with_suffix(Path(path).suffix + ".cadir.json")
    r = cadmpeg("dump", path, "-o", out)
    if not out.exists():
        raise SystemExit(f"cadmpeg could not dump {path}:\n{r.stderr[-1500:]}")
    return json.loads(out.read_text())["model"].get("features", [])


def _blind(defn):
    """The travel of an extrude definition: its blind length in mm (a symmetric extent's is the
    total), 'through_all' when every side runs through all, else None."""
    ext = defn.get("extent") or {}
    sides = [ext[k] for k in ("side", "first", "second") if ext.get(k)]
    terms = [sd.get("termination") or {} for sd in sides]
    if terms and all(t.get("kind") == "through_all" for t in terms):
        return "through_all"
    lengths = [t.get("length") for t in terms if t.get("kind") == "blind"]
    return lengths[0] if len(lengths) == 1 else None


def _problems(f, d, tol):
    """What is wrong with native definition `d` as the realisation of IR feature `f` ([] = OK)."""
    want_def, ops = KIND["pocket"] if f["kind"] == "polar_pocket" else KIND[f["kind"]]
    got = d.get("definition")
    if got != want_def:
        return [f"is {got}, expected {want_def}"]
    if ops and d.get("op") not in ops:
        return [f"op {d.get('op')}, expected {'/'.join(sorted(ops))}"]
    if f["kind"] == "fillet":
        radii = [(g.get("radius") or {}).get("radius") for g in d.get("groups", [])]
        if not any(r is not None and abs(r - f["radius"]) <= tol for r in radii):
            return [f"radius {radii}, expected {f['radius']}"]
    elif f["kind"] != "revolve":
        want = ("through_all" if f.get("through") else
                f["depth"] if f["kind"] == "prism_cut" else f["length"])
        got_len = _blind(d)
        if "through_all" in (want, got_len):
            if want != got_len:
                return [f"extent {got_len}, expected {want}"]
        elif got_len is None:
            return ["blind length not decoded"]
        elif abs(got_len - want) > tol:
            return [f"length {got_len}, expected {want}"]
    return []


def tree_check(spec, features, tol=1e-3):
    """Match each solid-producing IR feature to a native feature of the same name. Names can repeat
    in a native file (one per component, say), so a feature passes if ANY same-named native
    feature is the right kind with the right value. Rows are {name, kind, status, detail} with
    status OK / WRONG / MISSING."""
    by_name = {}
    for f in features:
        by_name.setdefault((f.get("name") or "").strip(), []).append(f.get("definition", {}))
    rows = []
    for f in spec["features"]:
        if f["kind"] == "sketch":
            continue
        names = ([f"{f['name']}_{i}" for i in range(int(f["count"]))] if f["kind"] == "polar_pocket"
                 else [f["name"]])
        for n in names:
            cands = by_name.get(n, [])
            if not cands:
                rows.append({"name": n, "kind": f["kind"], "status": "MISSING",
                             "detail": "no native feature by this name"})
                continue
            found = [(_problems(f, d, tol), d) for d in cands]
            ok = [d for p, d in found if not p]
            if ok:
                rows.append({"name": n, "kind": f["kind"], "status": "OK", "detail": ok[0]["definition"]})
            else:
                why = sorted({"; ".join(p) for p, _ in found})
                rows.append({"name": n, "kind": f["kind"], "status": "WRONG", "detail": " | ".join(why)})
    return rows


def verify(step, spec, samples=20000):
    from build123d import import_step

    import b3d_emit
    from iou_check import iou, solid_part
    ref, res = b3d_emit.emit(spec)
    got = solid_part(import_step(str(step)))
    dvol = abs(got.volume - ref.volume) / ref.volume
    p, ci = iou(ref, got, n=samples)
    return {"volume": round(got.volume, 3), "reference_volume": round(ref.volume, 3),
            "dvol_pct": round(100 * dvol, 4), "iou_pct": round(100 * p, 3), "ci_pct": round(100 * ci, 3),
            "verdict": "VERIFIED" if dvol <= VOL_TOL and p >= IOU_TOL else "MISMATCH"}


def first_divergence(report):
    for r in report["features"]:
        if not r.get("ok"):
            return r, "failed: " + r.get("error", "")
        if r.get("volume_ok") is False:
            return r, f"volume {r['volume']:.3f} vs reference {r['expected']:.3f} mm^3"
    return None, None


def decoder_agreement(a, b, samples):
    """IoU of two decoders' STEPs of the same file (same frame: both keep the file's coordinates)."""
    from build123d import import_step

    from iou_check import iou, solid_part
    p, ci = iou(solid_part(import_step(str(a))), solid_part(import_step(str(b))), n=samples)
    return round(100 * p, 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", help="a STEP, or a native .sldprt / .f3d")
    ap.add_argument("ir", nargs="?")
    ap.add_argument("--report")
    ap.add_argument("--samples", type=int, default=20000)
    ap.add_argument("--onshape", action="store_true", help="also decode a native file with Onshape")
    ap.add_argument("--decoders-only", action="store_true",
                    help="no IR: decode a native file with cadmpeg AND Onshape and compare them")
    a = ap.parse_args()
    native = Path(a.step).suffix.lower() in NATIVE
    if a.decoders_only:
        if not native:
            raise SystemExit("--decoders-only needs a native .sldprt / .f3d")
        import onshape_translate
        cm, n_err = native_step(a.step)
        osh, url = onshape_translate.translate(a.step)
        print(f"cadmpeg -> {cm} ({n_err} model-check errors)\nonshape -> {osh}  ({url})")
        agree = decoder_agreement(cm, osh, a.samples)
        print(f"decoder agreement (IoU, cadmpeg vs Onshape): {agree}%")
        return 0 if agree >= 100 * IOU_TOL else 1
    if not a.ir:
        raise SystemExit("an IR is required (or use --decoders-only)")
    spec = json.loads(Path(a.ir).read_text())
    steps, tree_ok = {"step": a.step}, True
    if native:
        rows = tree_check(spec, native_features(a.step))
        bad = [r for r in rows if r["status"] != "OK"]
        print(f"feature tree (cadmpeg): {len(rows) - len(bad)}/{len(rows)} IR features found native, "
              "right kind and value")
        for r in bad[:20]:
            print(f"  {r['status']:8} {r['name']} ({r['kind']}): {r['detail']}")
        tree_ok = not bad
        cm, n_err = native_step(a.step)
        print(f"geometry decoded by cadmpeg -> {cm}  ({n_err} model-check errors, allowed through)")
        steps = {"cadmpeg": cm}
        if a.onshape:
            import onshape_translate
            osh, url = onshape_translate.translate(a.step)
            print(f"geometry decoded by Onshape -> {osh}  ({url})")
            steps["onshape"] = osh
    if a.report:
        rep = json.loads(Path(a.report).read_text())
        n_ok = sum(1 for r in rep["features"] if r.get("ok"))
        print(f"{rep['target']}: {n_ok}/{len(rep['features'])} features built")
        r, why = first_divergence(rep)
        if r:
            print(f"  first divergence: {r['name']} ({r['kind']}) — {why}")
    out = {name: verify(st, spec, a.samples) for name, st in steps.items()}
    if len(steps) > 1:
        out["decoder_agreement_iou_pct"] = decoder_agreement(steps["cadmpeg"], steps["onshape"], a.samples)
    judge = "onshape" if "onshape" in out else next(iter(steps))
    out["geometry_decided_by"] = judge
    if judge == "cadmpeg":
        print("  note: geometry decoded by cadmpeg alone, which failed on 10/11 real NIST .SLDPRT "
              "files; a MISMATCH here needs --onshape before it means anything")
    geom_ok = out[judge]["verdict"] == "VERIFIED"
    out["tree"] = "OK" if tree_ok else "MISMATCH"
    out["verdict"] = "VERIFIED" if geom_ok and tree_ok else "MISMATCH"
    print(json.dumps(out, indent=1))
    return 0 if out["verdict"] == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
