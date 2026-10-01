# fsgen — notes for Claude Code

Follow **`AGENTS.md`** (the shared instructions for all AI assistants: making a part, API budget,
documentation). Claude-specific additions:

- `/make-part <description>` (`.claude/commands/make-part.md`) runs the part workflow from `AGENTS.md`.
- `fsgen generate` uses Claude by default: the logged-in `claude` CLI, or the Anthropic API if
  `ANTHROPIC_API_KEY` is set.

## FeatureScript MCP server (`onshape-featurescript`)

Every MCP tool call uses the same Onshape allocation (OAuth). Use it only where it adds something fsgen can't:

- At the start of a session that will use the API: call `get_api_usage` once and record the result with
  `fsgen usage --set-official <used>` so the ledger baseline includes MCP and other usage.
- Before writing or changing generated FeatureScript (paste export `fsgen/native/paste.py`, prompts in
  `fsgen/native/prompts.py`), check unfamiliar std signatures with `search_featurescript_documentation`
  instead of guessing. Note in `docs/FINDINGS.md` whether a doc search changed the official count.
- Never call tools that write to the user's Onshape (`put_featurescript`, `create_feature_studio`,
  `test_feature`, `create_geometry`, `set_api_allocation`) without asking first.
