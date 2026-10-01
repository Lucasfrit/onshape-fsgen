#!/usr/bin/env python3
"""roundtrip.py — read a .FCStd back, optionally apply a human edit, report the tree.

    python roundtrip.py <file.FCStd>                 # just read the tree/params back
    python roundtrip.py <file.FCStd> edits.json      # apply edits, re-save, report

edits.json: {"<feature label>": {"length": 12}}  or  {"<sketch/fillet label>": {"radius": 2}}
Edits are matched to features BY LABEL (= the IR name), never by kernel edge id — that is
what makes the round-trip survive rebuilds. Feed the result back into your IR with
ir.update_from_freecad(spec, result["params"]).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runner import run_in_freecad  # noqa: E402

FC_READ = HERE / "fc_read.py"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    fcstd = sys.argv[1]
    edits = Path(sys.argv[2]).read_text() if len(sys.argv) > 2 else "{}"
    proc = run_in_freecad(str(FC_READ), {"FC_IN": fcstd, "FC_EDIT": edits, "FC_LIBDIR": HERE})
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")), None)
    if line is None:
        sys.stderr.write((proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:])
        raise SystemExit("FreeCAD read failed (see output above)")
    print(json.dumps(json.loads(line[len("RESULT:"):]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
