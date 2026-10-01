"""fsgen command line.

  fsgen generate "a 60x40x5 mm plate with four M4 holes" [--cloud]
  fsgen run part.fs [-p width=80mm] [--png] [--out DIR]
  fsgen watch part.fs                  # rebuild + live 3D viewer on every save
  fsgen push part.fs [-p ...]          # Onshape: upload, regenerate, compare, export STEP/STL
  fsgen eval part.fs                   # Onshape error messages for a failing file
  fsgen compare examples/compat/*.fs   # local engine vs Onshape
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .fslite.values import UNITS, Q


def parse_param(text: str):
    name, _, raw = text.partition("=")
    raw = raw.strip()
    m = re.fullmatch(r"([-+]?[0-9.eE+-]+)\s*(mm|cm|m|in|deg)?", raw)
    if raw.lower() in ("true", "false"):
        return name, raw.lower() == "true"
    if not m:
        return name, raw
    v, unit = float(m.group(1)), m.group(2)
    if unit is None:
        return name, v
    u = {"mm": UNITS["millimeter"], "cm": UNITS["centimeter"], "m": UNITS["meter"], "in": UNITS["inch"],
         "deg": UNITS["degree"]}[unit]
    return name, Q(v * u.v, u.dims)


def params_of(args) -> dict:
    return dict(parse_param(p) for p in (args.param or []))


def cmd_run(args):
    from .fslite.runner import describe, export, render_png, run_source

    path = Path(args.file)
    res = run_source(path.read_text(), params=params_of(args))
    print(describe(res))
    print(f"({res.seconds * 1000:.0f} ms)")
    if not res.ok:
        return 1
    out = Path(args.out or Path("out") / path.stem)
    files = export(res, out, path.stem)
    if args.png:
        files.append(render_png(res, out / f"{path.stem}.png", path.stem))
    print("wrote", ", ".join(str(f) for f in files))
    return 0


def cmd_generate(args):
    if not args.custom:
        from .native.generate import generate_native

        res = generate_native(args.request, Path(args.out) if args.out else None, name=args.name, paste=args.paste,
                              backend=args.backend, model=args.model, max_fixes=args.max_fixes,
                              review_rounds=0 if args.no_review else args.reviews, web=not args.no_web,
                              llm_command=args.llm_command,
                              export_formats=tuple(f for f in (args.export or "").split(",") if f),
                              budget=args.budget)
        print(json.dumps({k: v for k, v in res.items() if k != "history"}, indent=2, default=str))
        return 0 if res["ok"] else 1
    from .generate import generate

    res = generate(args.request, Path(args.out) if args.out else None, backend=args.backend, model=args.model,
                   max_fixes=args.max_fixes, review=not args.no_review, cloud=args.cloud)
    print(json.dumps({"ok": res.ok, "out": str(res.out_dir), "attempts": len(res.attempts),
                      "local": res.local_metrics, "cloud": res.cloud}, indent=2, default=str))
    return 0 if res.ok else 1


def paste_command(path, tr, args) -> int:
    """Write the script as a parametric custom feature to paste into a Feature Studio (0 API calls)."""
    import subprocess
    from .native.paste import PasteError, paste_export
    from .native.script import trace as _trace
    from .outputs import to_dropbox

    title = args.name or path.stem.replace("_", " ").title()
    try:
        code, chk = paste_export(_trace(path.read_text(), "studio"), title)
    except PasteError as e:
        print(f"paste export not possible: {e}")
        return 1
    out = Path("out") / "paste" / f"{path.stem}.fs"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(code)
    print(f"wrote {out}  (parameters: {', '.join(chk.get('parameters', []))})")
    print(f"local check: custom feature {chk.get('paste_volume_mm3')} mm^3 vs native build {chk['native_volume_mm3']} mm^3 -> "
          + ("same" if chk["ok"] else f"DIFFERENT {chk.get('error', '')}"))
    copied = to_dropbox(path.stem, [out])
    if copied:
        print("copied to Dropbox:", copied[0])
    try:
        subprocess.run(["pbcopy"], input=code, text=True, check=True)
        print("copied to the clipboard: in Onshape create a Feature Studio, select all, paste, then insert the "
              f"feature \"{title}\" in a Part Studio (0 API calls)")
    except (OSError, subprocess.CalledProcessError):
        pass
    return 0 if chk["ok"] else 1


def cmd_studio(args):
    from .native import build as nb
    from .native.script import trace

    path = Path(args.file)
    tr = trace(path.read_text())
    if not tr.ok:
        print(f"script error: {tr.error}")
        return 1
    for f in tr.features:
        print(f"  {f.feature_type:16s} {f.name}")
    from .native import build as nb
    from .usage import estimate_push
    states = nb.load_states()
    sname = args.name or path.stem
    st0 = None if args.fresh or sname not in states else nb.StudioState(**states[sname])
    est = estimate_push(tr, st0, metrics=args.metrics, exports=tuple(f for f in (args.export or "").split(",") if f))
    print(f"push to \"{sname}\" ({'new' if st0 is None else 'update'}): " + est.text())
    if args.action == "paste":
        return paste_command(path, tr, args)
    if args.action in ("check", "build"):
        from .native.local import build_local, render
        loc = build_local(tr)
        print(loc.report() if loc.ok else f"local build error: {loc.error}")
        if args.action == "build" and loc.ok:
            from .fslite.runner import export as local_export
            out = Path("out") / path.stem
            files = local_export(loc.run, out, path.stem) + [render(loc, out / f"{path.stem}.png", path.stem)]
            print("wrote", ", ".join(map(str, files)))
            from .outputs import open_in_freecad, to_dropbox
            if args.dropbox:
                copied = to_dropbox(path.stem, files + [path])
                print("copied to Dropbox:", copied[0].parent if copied else "(set FSGEN_DROPBOX_DIR)")
            if args.open:
                open_in_freecad(files[0])
        print(f"({loc.seconds * 1000:.0f} ms, 0 Onshape calls)")
        return 0 if loc.ok else 1
    from .onshape import Onshape

    if args.budget is not None and est.total > args.budget:
        print(f"refusing to push: estimate {est.total} exceeds --budget {args.budget}")
        return 1
    o = Onshape(purpose=f"studio push {path.name}")
    from .cloud import workspace_ids
    ws = workspace_ids(o)
    st = nb.get_studio(o, args.name or path.stem, ws["did"], ws["wid"], fresh=args.fresh)
    r = nb.push_trace(o, st, tr)
    from .native import catalog
    if r.ok:
        learned = catalog.record_success(tr.features, path.read_text(), str(path))
        if learned:
            print("catalog: learned", ", ".join(learned))
    elif r.failed:
        f = next((f for f in tr.features if f.name == r.failed), None)
        if f is not None:
            catalog.record_failure(f.feature_type, r.error or "", str(path))
    print(f"built {r.built}, reused {r.reused}: {r.url}")
    for w in r.warnings:
        print("warning:", w)
    if not r.ok:
        print(r.error)
        print(o.summary())
        return 1
    if args.metrics:
        print(nb.studio_metrics(o, st))
    out = Path("out") / st.name.replace(" ", "_")
    files = []
    if args.export:
        files += nb.export_studio(o, st, out, "part", tuple(args.export.split(",")))
    if args.view:
        files.append(nb.shaded_view(o, st, out / "onshape_view.png"))
    if files:
        print("wrote", ", ".join(map(str, files)))
    print(o.summary())
    return 0


def cmd_recognize(args):
    """STEP -> Part Studio script via featuretree recognition; checked locally, 0 Onshape calls."""
    from .recognize import RecognizeError, recognize_step
    from .native.script import trace
    from .usage import estimate_push

    path = Path(args.step)
    try:
        script, rep = recognize_step(path)
    except RecognizeError as e:
        print(f"could not translate: {e}")
        return 1
    out = Path(args.out or Path("out") / "recognized" / f"{path.stem}.fs")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script)
    r = rep["report"] if isinstance(rep["report"], dict) else {}
    print(f"wrote {out} ({rep['features']} features, method: {r.get('method')})")
    print(f"volume: STEP {rep['step_volume_mm3']} mm^3, recognized script {rep['script_volume_mm3']} mm^3 -> "
          + ("MATCH" if rep["match"] else "PARTIAL (featuretree could not capture every feature)"))
    for w in r.get("warnings", []):
        print("  featuretree:", w)
    if "rotated onto Z" in " ".join(r.get("warnings", [])):
        print("  note: the part was re-oriented so its extrude axis is Z")
    print("to push: fsgen studio push", out, " (" + estimate_push(trace(script), None).text() + ")")
    return 0 if rep["match"] else 2


def cmd_spec(args):
    """The instructions the LLM gets (dialect spec + rules.md + feature catalog + verified examples)."""
    from .native import prompts
    print(prompts.system_prompt())
    return 0


def cmd_usage(args):
    """Counted Onshape calls from the local ledger, plus the baseline from Onshape's usage report."""
    from .usage import allocation_status, breakdown, set_official

    if args.set_official is not None:
        set_official(args.set_official)
        print(f"recorded official count {args.set_official} (from the MCP get_api_usage tool) as the new baseline")
    a = allocation_status()
    print(f"Onshape API allocation: {a.used} / {a.allocation} used ({a.used / a.allocation:.0%}); {a.left} left")
    if a.logged:
        purposes, calls = breakdown(a.logged, args.top)
        print("logged since baseline by command:", ", ".join(f"{k} {v}" for k, v in purposes))
        print("top endpoints:")
        for k, v in calls:
            print(f"  {v:5d}  {k}")
    return 0


def cmd_features(args):
    from .native import catalog
    from .native.script import SPECS

    if args.refresh:
        from .native.specs_tool import refresh
        from .cloud import workspace_ids
        from .onshape import Onshape
        o = Onshape(purpose="features --refresh")
        ws = workspace_ids(o)
        print("refreshed", refresh(o, ws["did"], ws["wid"], ws["part"]), "feature types")
    cat = catalog.load()
    if args.markdown:
        print(catalog.markdown_table())
        return 0
    if args.feature:
        ft = args.feature
        if ft not in SPECS:
            print(f"unknown feature type {ft}")
            return 1
        e = cat.get(ft, {})
        print(f"{ft} ({SPECS[ft]['name']}): {e.get('status', 'available')}")
        for n in e.get("notes", []):
            print("  note:", n)
        if e.get("example"):
            print("  example:", e["example"])
        for n in e.get("failures", []):
            print("  failure:", n)
        print("  settable parameters:")
        for k in catalog.settable_params(ft):
            p = SPECS[ft]["params"][k]
            extra = f" {p['enum']}{{{', '.join(p['options'])}}}" if p["type"] == "Enum" else \
                f" {p.get('quantity', '')}" if p["type"] == "Quantity" else ""
            print(f"    {k:32s} {p['type']}{extra}")
        return 0
    groups = {"verified": [], "learned": [], "available": []}
    for ft in sorted(SPECS):
        groups.setdefault(cat.get(ft, {}).get("status", "available"), []).append(ft)
    for g in ("verified", "learned", "available"):
        names = groups.get(g, [])
        if g == "available" and not args.all:
            names = [n for n in names if not n.startswith("sheetMetal")] + ["(sheetMetal*: 17 more, use --all)"]
        print(f"{g} ({len(groups.get(g, []))}): {', '.join(names)}\n")
    return 0


def cmd_push(args):
    from . import cloud
    from .compare import compare_metrics
    from .fslite.runner import run_source
    from .onshape import Onshape

    path = Path(args.file)
    src = path.read_text()
    o = Onshape()
    ws = cloud.ensure_workspace(o)
    p = cloud.push(o, ws, src, feature_name=path.stem, params=params_of(args))
    print(json.dumps(p, indent=2))
    if not p["ok"]:
        ev = cloud.evaluate(o, ws, src, params=params_of(args))
        print("\n".join(ev.errors))
        return 1
    m = cloud.part_metrics(o, ws)
    local = run_source(src, params=params_of(args))
    print("onshape:", m)
    if local.ok:
        print("local:  ", local.metrics())
        print("diff:   ", compare_metrics(local.metrics(), m) or "none")
    files = cloud.export(o, ws, Path(args.out or Path("out") / path.stem), f"{path.stem}_onshape",
                         formats=tuple(args.formats.split(",")))
    print("wrote", ", ".join(map(str, files)))
    return 0


def cmd_eval(args):
    from . import cloud
    from .onshape import Onshape

    o = Onshape()
    ws = cloud.ensure_workspace(o)
    r = cloud.evaluate(o, ws, Path(args.file).read_text(), params=params_of(args))
    print(json.dumps(r.__dict__, indent=2, default=str))
    return 0 if r.ok else 1


def cmd_compare(args):
    from .compare import main

    rows = main([Path(f) for f in args.files], Path("out/compare.json"))
    return 0 if all(r.get("match") for r in rows) else 1


def cmd_watch(args):
    from .watch import watch

    watch(Path(args.file), port=args.port, params=params_of(args))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fsgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="prompt -> FeatureScript -> geometry")
    g.add_argument("request")
    g.add_argument("--cloud", action="store_true", help="also push to Onshape, compare and export")
    g.add_argument("--backend", default="auto", choices=["auto", "anthropic", "claude-cli", "command"],
                   help="auto: FSGEN_LLM_COMMAND if set, else the Anthropic API if a key is set, else the claude CLI")
    g.add_argument("--llm-command", help="any LLM CLI that reads stdin and prints the answer, e.g. "
                                         "'ollama run qwen2.5-coder:32b' or 'gemini -p' (also FSGEN_LLM_COMMAND)")
    g.add_argument("--model")
    g.add_argument("--max-fixes", type=int, default=4)
    g.add_argument("--no-review", action="store_true")
    g.add_argument("--out")
    g.add_argument("--name", help="Part Studio name (reuses and updates it if it exists)")
    g.add_argument("--export", default="", help="export via API: comma list of step,stl (costs calls; the Onshape UI "
                                                "exports for free)")
    g.add_argument("--reviews", type=int, default=2, help="max review rounds (a follow-up checks the fixes)")
    g.add_argument("--no-web", action="store_true", help="don't let the background LLM search the web")
    g.add_argument("--paste", action="store_true", help="no Onshape calls: write a custom feature to paste "
                                                         "into a Feature Studio instead of pushing a native tree")
    g.add_argument("--budget", type=int, help="max Onshape API calls for this run (default ONSHAPE_RUN_BUDGET)")
    g.add_argument("--custom", action="store_true",
                   help="old mode: one custom feature (FS-Lite) instead of a native feature tree")
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("studio", help="native Part Studio scripts: check or push to Onshape")
    s.add_argument("action", choices=["check", "build", "paste", "push"],
                   help="check: trace + local build (0 calls); build: also export STEP/STL/PNG locally; "
                        "paste: custom feature to paste into Onshape (0 calls); push: native tree via the API")
    s.add_argument("file")
    s.add_argument("--name", help="Part Studio name (default: file name)")
    s.add_argument("--fresh", action="store_true", help="create a new Part Studio instead of updating")
    s.add_argument("--export", default="", help="also export via API: step,stl (STL ~1 call, STEP ~4)")
    s.add_argument("--metrics", action="store_true", help="fetch parts/volume/area (1 call)")
    s.add_argument("--view", action="store_true", help="save Onshape's isometric render (1 call)")
    s.add_argument("--budget", type=int, help="refuse to push if the estimate exceeds this many calls")
    s.add_argument("--dropbox", action="store_true", help="build: also copy outputs to FSGEN_DROPBOX_DIR")
    s.add_argument("--open", action="store_true", help="build: open the STEP in FreeCAD")
    s.set_defaults(fn=cmd_studio)

    for name, fn, h in (("run", cmd_run, "build locally and export STEP/STL"),
                        ("push", cmd_push, "upload to Onshape, regenerate, export"),
                        ("eval", cmd_eval, "Onshape diagnostics"),
                        ("watch", cmd_watch, "rebuild on save with a live viewer")):
        p = sub.add_parser(name, help=h)
        p.add_argument("file")
        p.add_argument("-p", "--param", action="append", help="override a parameter, e.g. width=80mm")
        if name in ("run", "push"):
            p.add_argument("--out")
        if name == "run":
            p.add_argument("--png", action="store_true")
        if name == "push":
            p.add_argument("--formats", default="step,stl", help="comma list of step,stl,x_t")
        if name == "watch":
            p.add_argument("--port", type=int, default=8765)
        p.set_defaults(fn=fn)

    rc = sub.add_parser("recognize", help="STEP -> editable Part Studio script (featuretree), 0 calls")
    rc.add_argument("step")
    rc.add_argument("--out")
    rc.set_defaults(fn=cmd_recognize)

    sp = sub.add_parser("spec", help="print the Part Studio script spec, rules, catalog and examples")
    sp.set_defaults(fn=cmd_spec)

    us = sub.add_parser("usage", help="Onshape API calls used vs. the annual allocation")
    us.add_argument("--top", type=int, default=12)
    us.add_argument("--set-official", type=int, metavar="N",
                    help="record Onshape's official used count (e.g. from the MCP get_api_usage tool) as the baseline")
    us.set_defaults(fn=cmd_usage)

    fe = sub.add_parser("features", help="feature catalog: verified / learned / available")
    fe.add_argument("feature", nargs="?", help="show one feature type with its settable parameters")
    fe.add_argument("--all", action="store_true")
    fe.add_argument("--refresh", action="store_true", help="re-download Onshape feature specs (1 call)")
    fe.add_argument("--markdown", action="store_true", help="print the feature/local-builder table")
    fe.set_defaults(fn=cmd_features)

    c = sub.add_parser("compare", help="local engine vs Onshape on files")
    c.add_argument("files", nargs="+")
    c.set_defaults(fn=cmd_compare)

    args = ap.parse_args(argv)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
