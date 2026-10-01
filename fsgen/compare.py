"""Compare local FS-Lite geometry with Onshape's regeneration of the same source."""
from __future__ import annotations

import json
from pathlib import Path

from . import cloud
from .fslite.runner import run_source
from .onshape import Onshape, OnshapeError, RateLimited


def close(a, b, rel=1e-4, abs_=1e-3) -> bool:
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


def compare_metrics(local: dict, remote: dict, bbox_tol_mm=0.05) -> list[str]:
    issues = []
    if local.get("bodies") != remote.get("bodies"):
        issues.append(f"bodies {local.get('bodies')} vs {remote.get('bodies')}")
    for k in ("volume_mm3", "area_mm2"):
        if not close(local.get(k, 0), remote.get(k, 0)):
            issues.append(f"{k} {local.get(k)} vs {remote.get(k)}")
    lb, rb = local.get("bbox_mm"), remote.get("bbox_mm")
    if lb and rb and any(abs(x - y) > bbox_tol_mm for x, y in zip(lb, rb)):
        # Onshape's /boundingboxes is not tight on curved geometry; only flag if it fails to enclose ours.
        encloses = all(r <= l + bbox_tol_mm for l, r in zip(lb[:3], rb[:3])) and \
            all(r >= l - bbox_tol_mm for l, r in zip(lb[3:], rb[3:]))
        if not encloses:
            issues.append(f"bbox {lb} vs {rb}")
    return issues


def compare_file(o: Onshape, ws: cloud.Workspace, path: Path, diagnose: bool = True) -> dict:
    src = path.read_text()
    local = run_source(src)
    row = {"file": path.name, "path": str(path), "local_ok": local.ok, "local_error": local.error,
           "local": local.metrics() if local.ok else None, "local_s": round(local.seconds, 3)}
    try:
        push = cloud.push(o, ws, src, feature_name=path.stem)
        row["cloud_status"] = push.get("status") or push.get("error")
        if push.get("ok"):
            row["cloud"] = cloud.part_metrics(o, ws)
        elif diagnose:
            ev = cloud.evaluate(o, ws, src)
            row["cloud_errors"] = ev.errors
    except RateLimited as e:
        row["cloud_status"] = "RATE_LIMITED"
        row["cloud_errors"] = [str(e)]
    except OnshapeError as e:
        row["cloud_status"] = "API_ERROR"
        row["cloud_errors"] = [str(e)[:500]]
    if row.get("local") and row.get("cloud"):
        row["issues"] = compare_metrics(row["local"], row["cloud"])
        row["match"] = not row["issues"]
    return row


def main(paths: list[Path], out: Path | None = None):
    o = Onshape()
    ws = cloud.ensure_workspace(o)
    rows = []
    for p in paths:
        row = compare_file(o, ws, p)
        rows.append(row)
        status = "MATCH" if row.get("match") else ("DIFF" if "match" in row else row.get("cloud_status"))
        print(f"{p.name:32s} local={'ok' if row['local_ok'] else 'ERR'} cloud={row.get('cloud_status')} -> {status}")
        for issue in row.get("issues", []):
            print("    ", issue)
        if row.get("local_error"):
            print("     local error:", row["local_error"])
        for e in row.get("cloud_errors", []):
            print("     cloud error:", e)
    print("quota remaining:", o.remaining)
    if out:
        out.write_text(json.dumps(rows, indent=2, default=str))
    # Record Onshape's numbers as golden values for the offline regression tests.
    golden_path = Path(__file__).resolve().parent.parent / "examples" / "compat" / "golden.json"
    golden = json.loads(golden_path.read_text()) if golden_path.exists() else {}
    for r in rows:
        if r.get("match") and str(Path(r["path"]).resolve().parent) == str(golden_path.parent):
            golden[r["file"]] = r["cloud"]
    golden_path.write_text(json.dumps(dict(sorted(golden.items())), indent=2))
    return rows


if __name__ == "__main__":
    import sys
    main([Path(p) for p in sys.argv[1:]], Path("out/compare.json"))
