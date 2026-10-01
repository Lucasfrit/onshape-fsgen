#!/usr/bin/env python3
"""gen.py — emit a FreeCAD .FCStd feature tree from an IR spec.

    python gen.py <ir.json> [out.FCStd]     # emit a spec you authored (see ir.py)
    python gen.py --sample plate            # built-in samples: plate | poly

Writes <out>.FCStd (+ .stl) with the operations in FreeCAD's left-panel tree, and
prints the as-built tree / volume / editable params.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ir as IR  # noqa: E402
from runner import run_in_freecad  # noqa: E402

FC_BUILD = HERE / "fc_build.py"


def emit(spec, out_fcstd):
    out = Path(out_fcstd)
    out.parent.mkdir(parents=True, exist_ok=True)
    ir_json = out.with_suffix(".ir.json")
    ir_json.write_text(json.dumps(spec, indent=2))
    proc = run_in_freecad(str(FC_BUILD), {"FC_IR": ir_json, "FC_OUT": out, "FC_LIBDIR": HERE})
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")), None)
    if line is None:
        sys.stderr.write((proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:])
        raise SystemExit("FreeCAD emit failed (see output above)")
    return json.loads(line[len("RESULT:"):])


def main():
    args = sys.argv[1:]
    if args and args[0] == "--sample":
        name = args[1] if len(args) > 1 else "plate"
        spec = IR.SAMPLES[name]()
        out = HERE / "out" / f"{spec['name']}.FCStd"
    elif args:
        spec = json.loads(Path(args[0]).read_text())
        out = Path(args[1]) if len(args) > 1 else Path(args[0]).with_suffix(".FCStd")
    else:
        print(__doc__)
        return 0
    res = emit(spec, out)
    print(f"emitted {out}  (open in FreeCAD — operations are in the left-panel tree)")
    print("tree:")
    for label, tid in res["tree"]:
        print(f"  {label:16} {tid}")
    print("volume:", res["volume"], "mm^3")
    print("params:", json.dumps(res["params"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
