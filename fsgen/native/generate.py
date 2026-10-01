"""Prompt -> Part Studio script -> native Onshape feature tree (repair + review loop) -> exports."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..llm import make_llm
from ..onshape import Onshape, OnshapeError
from . import build as nb
from . import catalog, prompts
from .local import build_local, render
from .script import trace


class Heartbeat:
    """Prints a status line every `interval` seconds while nothing else was printed (long LLM waits)."""

    STAGES = {"generate": "LLM is writing the script", "review": "LLM is reviewing the local preview",
              "repair-script": "LLM is fixing a script error", "repair-local": "LLM is fixing a local build error",
              "repair-onshape": "LLM is fixing an Onshape error", "repair": "LLM is retrying"}

    def __init__(self, interval: float = 10.0):
        import threading
        self.interval, self.t0 = interval, time.time()
        self.stage, self.stage_t0, self.last_print = "starting", time.time(), time.time()
        self.calls = lambda: 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def set(self, stage: str):
        self.stage, self.stage_t0 = self.STAGES.get(stage, stage), time.time()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(1.0):
            now = time.time()
            if now - self.last_print >= self.interval:
                total = int(now - self.t0)
                print(f"[fsgen] … {total // 60:02d}:{total % 60:02d} elapsed | {self.stage} "
                      f"({now - self.stage_t0:.0f} s) | Onshape calls so far: {self.calls()}", flush=True)
                self.last_print = now


_HEARTBEAT: Heartbeat | None = None


def log(msg):
    print(f"[fsgen] {msg}", flush=True)
    if _HEARTBEAT is not None:
        _HEARTBEAT.last_print = time.time()


def stage(name: str):
    if _HEARTBEAT is not None:
        _HEARTBEAT.set(name)


def extract_script(text: str) -> str | None:
    blocks = re.findall(r"```(?:featurescript|fs)?\s*\n(.*?)```", text, re.S)
    blocks = [b for b in blocks if "function build" in b]
    return blocks[-1].strip() + "\n" if blocks else None


def studio_name(request: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", request).split()
    return " ".join(words[:5])[:40] or "Generated part"


def report_text(metrics: dict, push: nb.PushResult, n_features: int) -> str:
    lines = [f"features in tree: {n_features}", f"parts: {metrics.get('parts')}",
             f"volume: {metrics.get('volume_mm3')} mm^3, surface area: {metrics.get('area_mm2')} mm^2"]
    if metrics.get("bbox_mm"):
        b = metrics["bbox_mm"]
        lines.append(f"bounding box (mm, may be slightly loose on curved parts): x {b[0]}..{b[3]}, "
                     f"y {b[1]}..{b[4]}, z {b[2]}..{b[5]}")
    lines += [f"warning: {w}" for w in push.warnings]
    return "\n".join(lines)


def supported_features() -> tuple[set, set]:
    """(locally buildable feature types, feature types the paste export can write)."""
    from .local import LocalBuilder
    from .paste import PasteWriter
    local = {n[2:] for n in dir(LocalBuilder) if n.startswith("f_")}
    paste = {n[2:] for n in dir(PasteWriter) if n.startswith("f_")} & local
    return local, paste


def mode_note(paste: bool) -> str:
    local, pasteable = supported_features()
    if paste:
        return ("Delivery: this part is delivered as a pasted custom feature (paste mode). Use ONLY these standard "
                f"features (the others cannot be pasted): {', '.join(sorted(pasteable - {'newSketch'}))}, plus "
                "newSketch and variable().")
    return ("Delivery: native feature tree in Onshape. Prefer these features, which can be checked locally before "
            f"anything is sent: {', '.join(sorted(local - {'newSketch'}))}. Other standard features are allowed when "
            "the design needs them, but they can only be checked in Onshape.")


def extract_assumptions(text: str) -> str | None:
    m = re.search(r"Assumptions:\**\s*\n(.*?)(?=\n\s*\n\s*(?![-*\d])|```|\Z)", text, re.S)
    return m.group(1).strip() if m and m.group(1).strip() else None


def not_built_features(tr, loc) -> list[str]:
    local, _ = supported_features()
    out = [f'"{f.name}" ({f.feature_type})' for f in tr.features if f.feature_type not in local]
    out += [w.split(":")[0] for w in loc.warnings if "skipped locally" in w]
    return out


def _existing_state(name):
    states = nb.load_states()
    return nb.StudioState(**states[name]) if name in states else None


def generate_native(request: str, out_dir: Path | None = None, name: str | None = None, backend: str = "auto",
                    model: str | None = None, max_fixes: int = 5, review_rounds: int = 2,
                    export_formats: tuple = (), budget: int | None = None, paste: bool = False,
                    web: bool = True) -> dict:
    llm = make_llm(backend, model, web=web)
    label = re.sub(r"[^a-z0-9]+", "-", (name or studio_name(request)).lower()).strip("-") or "part"
    out_dir = out_dir or Path("out") / "native" / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "request.txt").write_text(request)
    system = prompts.system_prompt()
    o = Onshape(budget=budget, purpose=f"generate: {request[:60]}")
    ws = None  # the fsgen Onshape document; looked up (or created once) only when pushing
    st = None  # the Part Studio is created only when a locally verified script is ready (saves calls)
    global _HEARTBEAT
    _HEARTBEAT = Heartbeat(10.0).start()
    _HEARTBEAT.calls = lambda: o.calls
    log(f"backend={llm.name}; local-first: Onshape is called once the script builds and passes review locally")
    history = []
    n = 0

    def ask(stage_name, prompt, image=None):
        nonlocal n
        t0 = time.time()
        stage(stage_name)
        text = llm.complete(system, prompt, image=image)
        (out_dir / f"llm_{n:02d}_{stage_name}.md").write_text(f"# prompt\n\n{prompt}\n\n# response\n\n{text}")
        n += 1
        log(f"{stage_name}: LLM answered in {time.time() - t0:.0f}s")
        return text, extract_script(text)

    def fix(stage, code, error, where):
        nonlocal fixes
        history.append((stage, error))
        if fixes >= max_fixes:
            return None
        fixes += 1
        return ask(stage, prompts.repair_prompt(request, code, error, where))[1]

    text, code = ask("generate", prompts.generate_prompt(request, mode_note(paste)))
    assumptions = extract_assumptions(text)
    if assumptions:
        (out_dir / "assumptions.md").write_text(f"# Assumptions for: {request}\n\n{assumptions}\n")
        log("assumptions (also in assumptions.md):\n" + assumptions)
    else:
        log("warning: the design states no assumptions")
    fixes, reviews = 0, 0
    previous_review = None
    result = {"ok": False, "studio": name or studio_name(request)}
    while code is not None:
        (out_dir / "part_studio.fs").write_text(code)
        stage("checking the script")
        tr = trace(code)
        if not tr.ok:
            log(f"script check failed: {tr.error}")
            code = fix("repair-script", code, tr.error, "the script checker")
            continue
        if paste:  # catch features the paste export can't write now, not after the review
            _, pasteable = supported_features()
            bad = sorted({f.feature_type for f in tr.features} - pasteable)
            if bad:
                msg = (f"Paste mode cannot use: {', '.join(bad)}. Allowed: {', '.join(sorted(pasteable))}. Rebuild the "
                       "affected geometry with the allowed features.")
                log(f"mode check failed: {msg}")
                code = fix("repair-mode", code, msg, "the paste-mode feature check")
                continue
        # ---- local build: free, ~0.3 s
        stage("building locally")
        loc = build_local(tr)
        if not loc.ok:
            log(f"local build failed: {loc.error}")
            code = fix("repair-local", code, loc.error, "the local geometry builder (it reproduces Onshape "
                       "feature semantics)")
            continue
        img = render(loc, out_dir / f"local_view_{reviews}.png", request[:80])
        report = f"features in tree: {len(tr.features)}, variables: {len(tr.variable_list)}\n" + loc.report()
        unbuilt = not_built_features(tr, loc)
        log(f"local build ok ({loc.seconds:.2f}s):\n{report}")
        if unbuilt:
            log("NOT built locally (unverified until Onshape builds them): " + "; ".join(unbuilt))
        if reviews < review_rounds:
            reviews += 1
            text, new_code = ask("review", prompts.review_prompt(request, code, report, unbuilt, previous_review),
                                 image=img)
            verdict = text.strip().splitlines()[-1] if text.strip() else ""
            if new_code and "LGTM" not in verdict:
                log(f"reviewer requested changes (review {reviews}/{review_rounds}); the next review checks them")
                history.append(("review", text[:500]))
                previous_review = text.split("```")[0]
                code = new_code
                continue
            if new_code is None and "LGTM" not in verdict:
                log("warning: reviewer neither approved nor returned a fix")
        result["verified"] = not unbuilt
        result["reviews"] = reviews
        result["assumptions"] = assumptions
        # ---- always write the free paste version (custom feature) next to the script
        from .paste import PasteError, paste_export
        try:
            pcode, pchk = paste_export(tr, result["studio"])
            (out_dir / "paste_feature.fs").write_text(pcode)
            log(f"paste version: {out_dir / 'paste_feature.fs'} (local check: {'same volume' if pchk['ok'] else 'DIFFERENT'})")
        except PasteError as e:
            pchk = {"ok": False, "error": str(e)}
            log(f"no paste version: {e}")
        if paste:
            from ..fslite.runner import export as local_export
            from ..outputs import to_dropbox
            files = local_export(loc.run, out_dir, "part_local", formats=("step", "stl"))
            paste_file = out_dir / "paste_feature.fs"
            have_paste = pchk.get("ok", False) and paste_file.exists()
            to_dropbox(result["studio"], files + [out_dir / "part_studio.fs"] + ([paste_file] if have_paste else []))
            result.update(ok=have_paste, mode="paste", local_metrics=loc.metrics(), paste_check=pchk,
                          files=[str(f) for f in files] + ([str(paste_file)] if have_paste else []))
            if have_paste:
                log("paste mode: no Onshape calls. Paste paste_feature.fs into a Feature Studio.")
            else:
                log(f"paste mode: NO paste file - {pchk.get('error', 'the custom feature differs from the local build')}. "
                    f"The native script is in {out_dir / 'part_studio.fs'}; it can still be pushed "
                    "(fsgen studio push ...) if Onshape should build the unsupported features.")
            break
        # ---- one push to Onshape: estimate first (offline), refuse if over budget
        from ..usage import allocation_status, estimate_push
        est = estimate_push(tr, st if st is not None else (_existing_state(name) if name else None),
                            exports=export_formats)
        alloc = allocation_status()
        log(est.text(alloc))
        result["estimate"] = est.total
        limit = budget if budget is not None else o.budget
        if limit is not None and est.total > limit - o.calls:
            result["error"] = f"estimated {est.total} calls exceeds the remaining run budget {limit - o.calls}"
            log(result["error"] + " - not pushing (script and local STEP/STL are in the output folder)")
            from ..fslite.runner import export as local_export
            local_export(loc.run, out_dir, "part_local", formats=("step", "stl"))
            break
        stage("pushing to Onshape")
        try:
            if st is None:
                from ..cloud import workspace_ids
                ws = ws or workspace_ids(o)
                st = nb.get_studio(o, name or studio_name(request), ws["did"], ws["wid"], fresh=name is None)
                log(f"Part Studio \"{st.name}\": {st.url(o.base)}")
            push = nb.push_trace(o, st, tr, log=lambda s: log(s))
        except OnshapeError as e:
            history.append(("api", str(e)))
            result["error"] = str(e)
            break
        if not push.ok:
            log(push.error)
            failed = next((f for f in tr.features if f.name == push.failed), None)
            if failed is not None:
                catalog.record_failure(failed.feature_type, push.error, request)
            code = fix("repair-onshape", code, push.error, "Onshape (the local builder accepted it)")
            continue
        learned = catalog.record_success(tr.features, code, request)
        if learned:
            log(f"catalog: learned {', '.join(learned)}")
        stage("checking the Onshape result")
        metrics = nb.studio_metrics(o, st)  # 1 call: confirms Onshape matches the local build
        local_m = loc.metrics()
        same = (metrics["parts"] == local_m["parts"] and local_m["volume_mm3"] and
                abs(metrics["volume_mm3"] - local_m["volume_mm3"]) <= 1e-3 * max(1.0, metrics["volume_mm3"]))
        log(f"Onshape: {metrics}; local: parts {local_m['parts']}, volume {local_m['volume_mm3']} -> "
            + ("match" if same else "DIFFERENT" + (" (local build was partial)" if loc.partial else "")))
        view = None
        if not same or loc.partial:
            view = nb.shaded_view(o, st, out_dir / "onshape_view.png")  # 1 call, only when local can't be trusted
        # API exports cost calls (STL ~1, STEP ~4); exporting from the Onshape UI is free
        files = nb.export_studio(o, st, out_dir, "part", export_formats) if export_formats else []
        from ..fslite.runner import export as local_export
        files += local_export(loc.run, out_dir, "part_local", formats=("step", "stl"))
        from ..outputs import to_dropbox
        copied = to_dropbox(st.name, files + [out_dir / "part_studio.fs", out_dir / f"local_view_{max(reviews - 1, 0)}.png"])
        if copied:
            log(f"copied outputs to Dropbox: {copied[0].parent}")
        result.update(ok=True, url=st.url(o.base), metrics=metrics, local_metrics=local_m, local_matches=bool(same),
                      warnings=push.warnings + loc.warnings, files=[str(f) for f in files],
                      features=len(tr.features), onshape_view=str(view) if view else None)
        break
    if result.get("ok"):
        log("result: " + ("verified locally" if result.get("verified") else
                          "NOT fully verified - some features were not built locally (see above)"))
    result["history"] = history
    result["api_calls"] = o.calls
    result["api_calls_by_endpoint"] = o.by_endpoint
    log(o.summary())
    _HEARTBEAT.stop()
    _HEARTBEAT = None
    (out_dir / "result.json").write_text(json.dumps(result, indent=2, default=str))
    return result
