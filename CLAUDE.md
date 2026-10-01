# fsgen — working rules for Claude

Prompt → Onshape parts. Read `README.md`, `docs/CAPABILITIES.md` and `docs/FINDINGS.md` before changing things.

## Making a part in chat (the usual way; `/make-part <description>`)

The chat model is the designer; fsgen is the toolchain. No second LLM is involved.

1. `.venv/bin/fsgen spec` — the dialect, the user's `rules.md`, the feature catalog and verified examples.
2. Write `parts/<name>.fs` (a Part Studio script).
3. `.venv/bin/fsgen studio build parts/<name>.fs` — trace + local build: errors, geometry report, STEP/STL and a
   4-view preview `out/<name>/<name>.png` (read it to check the shape), plus the push estimate. 0 Onshape calls.
   Iterate with the user here.
4. Deliver: `fsgen studio paste parts/<name>.fs --name "…"` (custom feature to paste, 0 calls; clipboard +
   Dropbox) or `fsgen studio push parts/<name>.fs --name "…" --metrics --budget N` (native editable tree; ask
   first, show the estimate). Edits to a pushed part: change the script and push again (1–2 calls).

`fsgen generate "…"` does the same unattended with a background `claude -p`; use it only when asked.

## Onshape API budget (EDU: 2500 calls/year — the scarcest resource here)

- Develop and test offline: local builder, `tests/` (fake Onshape in `fsgen/costmodel.py`). Never use the real
  API to test code changes.
- Before anything that costs calls, show the estimate (`fsgen studio check …` prints it) and ask if it's more
  than a few calls. Prefer the free paste workflow (`fsgen studio paste`, `generate --paste`) unless the user
  wants an editable native tree.
- `fsgen usage` shows calls used/left from the local ledger. It cannot see calls made through the
  FeatureScript MCP server.

## FeatureScript MCP server (`onshape-featurescript`)

Every MCP tool call uses the same Onshape allocation (OAuth). Use it only where it adds something fsgen can't:

- At the start of a session that will use the API: call `get_api_usage` once and record the result with
  `fsgen usage --set-official <used>` so the ledger baseline includes MCP and other usage.
- Before writing or changing generated FeatureScript (paste export `fsgen/native/paste.py`, prompts in
  `fsgen/native/prompts.py`), check unfamiliar std signatures with `search_featurescript_documentation`
  instead of guessing. Note in `docs/FINDINGS.md` whether a doc search changed the official count.
- Never call tools that write to the user's Onshape (`put_featurescript`, `create_feature_studio`,
  `test_feature`, `create_geometry`, `set_api_allocation`) without asking first.

## Documentation

- Append findings to `docs/FINDINGS.md` as they happen (dated, with how they were established: tested /
  measured / docs). Keep `docs/CAPABILITIES.md` current; regenerate its feature table with
  `fsgen features --markdown`.
- New feature types: verified example in `examples/native/`, local support in `fsgen/native/local.py`, and an
  Onshape-measured volume in `tests/test_native_local.py`.
