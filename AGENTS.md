# fsgen — instructions for AI coding assistants

Read by any assistant that supports `AGENTS.md` (Codex, Cursor, Copilot, Gemini CLI, …); Claude Code reads
`CLAUDE.md`, which points here. fsgen turns part descriptions into Onshape parts. **You are the designer:** you
write the Part Studio script, fsgen checks it, builds it locally and delivers it to Onshape. Read `README.md`,
`docs/CAPABILITIES.md` and `docs/FINDINGS.md` before changing the code.

## Making a part

1. Run `.venv/bin/fsgen spec`: the script dialect, the user's `rules.md`, the feature catalog and verified
   examples. Follow it.
2. If the request names a real product, look up its dimensions first (if you can browse). List your
   assumptions (every dimension or interpretation the request doesn't state) and show them to the user.
3. Write `parts/<name>.fs` (a Part Studio script).
4. Run `.venv/bin/fsgen studio build parts/<name>.fs`: trace + local build (errors, geometry report, STEP/STL,
   a 4-view preview `out/<name>/<name>.png`) and the push estimate. 0 Onshape calls. Look at the preview if you
   can read images; iterate with the user. Features reported as "not built locally" are not in the preview:
   say so, don't claim they are checked. For printed parts follow the 3D-printing rules in `rules.md`.
5. Deliver: `fsgen studio paste parts/<name>.fs --name "…"` (custom feature to paste, 0 calls) or
   `fsgen studio push parts/<name>.fs --name "…" --metrics --budget N` (native editable tree; ask first and show
   the estimate). Edits to a pushed part: change the script and push again (1–2 calls).

`fsgen generate "…"` does the same unattended with a background LLM (`--backend`, or any CLI LLM via
`FSGEN_LLM_COMMAND`); use it only when asked.

## Onshape API budget (EDU plans: 2500 calls/year — the scarcest resource)

- Develop and test offline: local builder, `tests/` (fake Onshape in `fsgen/costmodel.py`). Never use the real
  API to test code changes.
- Before anything that costs calls, show the estimate (`fsgen studio check …` prints it) and ask if it's more
  than a few calls. Prefer the free paste workflow unless the user wants an editable native tree.
- `fsgen usage` shows calls used/left from the local ledger; `fsgen usage --set-official N` records Onshape's
  official count.

## Documentation

- Append findings to `docs/FINDINGS.md` as they happen (dated, with how they were established: tested /
  measured / docs). Keep `docs/CAPABILITIES.md` current; regenerate its feature table with
  `fsgen features --markdown`.
- New feature types: verified example in `examples/native/`, local support in `fsgen/native/local.py`, and an
  Onshape-measured volume in `tests/test_native_local.py`.
- Commits: no AI attribution lines (no "Co-Authored-By" for assistants).
