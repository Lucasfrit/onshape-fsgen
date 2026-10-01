"""API allocation status and offline cost estimates for pushing a Part Studio script."""
from __future__ import annotations

import collections
import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .onshape import LEDGER

STEP_EXPORT_CALLS = 4  # translation request + ~2 status polls + download
STL_EXPORT_CALLS = 1


@dataclass
class Allocation:
    allocation: int
    used: int
    logged: list = field(default_factory=list)

    @property
    def left(self) -> int:
        return self.allocation - self.used


def allocation_status() -> Allocation:
    load_dotenv()
    allocation = int(os.environ.get("ONSHAPE_API_ALLOCATION", 2500))
    baseline = int(os.environ.get("ONSHAPE_API_BASELINE", 0))
    since = os.environ.get("ONSHAPE_API_BASELINE_DATE", "")
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines()] if LEDGER.exists() else []
    after = [r for r in rows if r["counted"] and (not since or r["t"] >= since)]
    return Allocation(allocation, baseline + len(after), after)


def breakdown(rows, top=10):
    by_purpose = collections.Counter((r.get("purpose") or "?").split(":")[0] for r in rows)
    by_call = collections.Counter(r["call"] for r in rows)
    return by_purpose.most_common(), by_call.most_common(top)


@dataclass
class Estimate:
    items: list[tuple[str, int]]

    @property
    def total(self) -> int:
        return sum(n for _, n in self.items)

    def text(self, alloc: Allocation | None = None) -> str:
        alloc = alloc or allocation_status()
        parts = ", ".join(f"{label} {n}" for label, n in self.items if n)
        pct_left = self.total / alloc.left * 100 if alloc.left > 0 else float("inf")
        return (f"estimated Onshape API calls: {self.total} ({parts}) = {pct_left:.1f}% of the {alloc.left} left "
                f"({self.total / alloc.allocation * 100:.1f}% of the yearly {alloc.allocation})")


def estimate_push(tr, state=None, metrics: bool = True, exports: tuple = ()) -> Estimate:
    """Offline estimate of what pushing traced script `tr` will cost, given the Part Studio's saved state.

    Mirrors fsgen.native.build.push_trace: unchanged features are free, changed ones are updated in place
    (1 call), new ones added (1 call); the Variable Studio costs 3 the first time and 1 when values change.
    """
    from .native.build import feature_hash

    items = []
    if state is None:
        items.append(("new Part Studio", 1))
    vs = 0
    if tr.variable_list:
        if state is None or state.vs_eid is None:
            vs = 3
        else:
            import hashlib
            h = hashlib.sha1(json.dumps(tr.variable_list, sort_keys=True).encode()).hexdigest()[:16]
            vs = 0 if h == state.vs_hash else 1
    items.append(("Variable Studio", vs))
    old = state.features if state else []
    send, updated, reused_after = 0, False, False
    for i, rec in enumerate(tr.features):
        e = old[i] if i < len(old) else None
        if e and e.get("type", rec.feature_type) == rec.feature_type and e["hash"] == feature_hash(rec) \
                and e.get("status") in ("OK", "WARNING", "INFO"):
            reused_after = reused_after or updated
            continue
        send += 1
        updated = updated or e is not None
    extra_deletes = max(0, len(old) - len(tr.features))
    items.append(("features", send + extra_deletes))
    items.append(("status check", 1 if (reused_after or (vs == 1 and old)) else 0))
    if metrics:
        items.append(("volume check", 1))
    for f in exports:
        items.append((f"{f.upper()} export", STEP_EXPORT_CALLS if f == "step" else STL_EXPORT_CALLS))
    return Estimate(items)


def set_official(used: int, env_path: str = ".env"):
    """Make Onshape's official count the new baseline; later ledger entries are added on top of it.

    The ledger only sees fsgen's own calls; calls made through the FeatureScript MCP server (or other apps)
    are only in Onshape's official number, so recalibrate from it now and then.
    """
    import re
    import time
    from pathlib import Path

    p = Path(env_path)
    text = p.read_text() if p.exists() else ""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, val in (("ONSHAPE_API_BASELINE", str(used)), ("ONSHAPE_API_BASELINE_DATE", stamp)):
        if re.search(rf"^{key}=.*$", text, re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={val}", text, flags=re.M)
        else:
            text += f"\n{key}={val}\n"
        os.environ[key] = val
    p.write_text(text)
