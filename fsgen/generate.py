"""Prompt -> FS-Lite -> local build (fast repair loop) -> optional Onshape push/export."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .fslite.runner import describe, export, render_png, run_source
from .llm import extract_code, make_llm


@dataclass
class Attempt:
    stage: str  # generate | repair-local | repair-cloud | review
    code: str | None
    ok: bool
    error: str | None = None
    seconds: float = 0.0


@dataclass
class GenResult:
    ok: bool
    code: str | None
    out_dir: Path
    attempts: list[Attempt] = field(default_factory=list)
    local_metrics: dict | None = None
    cloud: dict | None = None


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "part"


def log(msg: str):
    print(f"[fsgen] {msg}", flush=True)


def generate(request: str, out_dir: Path | None = None, backend: str = "auto", model: str | None = None,
             max_fixes: int = 4, review: bool = True, cloud: bool = False) -> GenResult:
    llm = make_llm(backend, model)
    out_dir = out_dir or Path("out") / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(request)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "request.txt").write_text(request)
    system = prompts.system_prompt()
    res = GenResult(False, None, out_dir)
    log(f"backend={llm.name} out={out_dir}")

    def ask(stage: str, prompt: str, image: Path | None = None) -> tuple[str, str | None]:
        t0 = time.time()
        text = llm.complete(system, prompt, image=image)
        n = len(res.attempts)
        (out_dir / f"llm_{n:02d}_{stage}.md").write_text(f"# prompt\n\n{prompt}\n\n# response\n\n{text}")
        log(f"{stage}: LLM answered in {time.time() - t0:.0f}s")
        return text, extract_code(text)

    text, code = ask("generate", prompts.generate_prompt(request))
    fixes = 0
    reviewed = False
    while True:
        if code is None:
            res.attempts.append(Attempt("generate", None, False, "no featurescript code block in reply"))
            if fixes >= max_fixes:
                break
            fixes += 1
            text, code = ask("repair-local", prompts.repair_prompt(request, text[-4000:], "Reply did not contain a "
                             "```featurescript block with defineFeature.", "parsing your reply"))
            continue
        (out_dir / "part.fs").write_text(code)
        run = run_source(code)
        res.attempts.append(Attempt("local", code, run.ok, run.error, run.seconds))
        if not run.ok:
            log(f"local build failed: {run.error.splitlines()[0]}")
            if fixes >= max_fixes:
                break
            fixes += 1
            text, code = ask("repair-local", prompts.repair_prompt(request, code, run.error, "the local FS-Lite engine"))
            continue
        report = describe(run)
        log("local build ok:\n" + report)
        png = render_png(run, out_dir / "preview.png", title=request[:80])
        if review and not reviewed:
            reviewed = True
            text, new_code = ask("review", prompts.review_prompt(request, code, report), image=png)
            if new_code and "LGTM" not in text.strip().splitlines()[-1:]:
                log("reviewer requested changes")
                code = new_code
                continue
        res.ok, res.code, res.local_metrics = True, code, run.metrics()
        export(run, out_dir, "part_local")
        (out_dir / "report.txt").write_text(report)
        break

    if res.ok and cloud:
        res.cloud = push_to_cloud(res, request, llm, ask, max_fixes - fixes)
    (out_dir / "result.json").write_text(json.dumps({
        "ok": res.ok, "local_metrics": res.local_metrics, "cloud": res.cloud,
        "attempts": [a.__dict__ for a in res.attempts]}, indent=2, default=str))
    return res


def push_to_cloud(res: GenResult, request: str, llm, ask, fixes_left: int) -> dict:
    from . import cloud
    from .compare import compare_metrics
    from .onshape import Onshape, OnshapeError

    o = Onshape()
    ws = cloud.ensure_workspace(o)
    code = res.code
    for _ in range(max(fixes_left, 0) + 1):
        try:
            p = cloud.push(o, ws, code, feature_name="Generated part")
        except OnshapeError as e:
            log(f"cloud push failed: {e}")
            return {"ok": False, "error": str(e)}
        if p["ok"]:
            metrics = cloud.part_metrics(o, ws)
            issues = compare_metrics(res.local_metrics, metrics)
            paths = cloud.export(o, ws, res.out_dir, "part_onshape", formats=("step", "stl"))
            log(f"Onshape regenerated OK: {p['url']}")
            log("local vs Onshape: " + ("identical" if not issues else "; ".join(issues)))
            return {"ok": True, "url": p["url"], "metrics": metrics, "issues": issues,
                    "files": [str(x) for x in paths]}
        ev = cloud.evaluate(o, ws, code)
        err = "\n".join(ev.errors) or f"feature status {p.get('status')}"
        log(f"Onshape rejected the feature: {err}")
        res.attempts.append(Attempt("cloud", code, False, err))
        text, new_code = ask("repair-cloud", prompts.repair_prompt(request, code, err, "Onshape"))
        if not new_code:
            break
        run = run_source(new_code)
        if not run.ok:
            log(f"cloud fix does not build locally: {run.error}")
            break
        code = res.code = new_code
        (res.out_dir / "part.fs").write_text(code)
        res.local_metrics = run.metrics()
        export(run, res.out_dir, "part_local")
    return {"ok": False, "error": "Onshape regeneration failed", "url": ws.url(o.base)}
