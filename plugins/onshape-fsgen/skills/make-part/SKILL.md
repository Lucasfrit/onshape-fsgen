---
name: make-part
description: Design a mechanical part (bracket, mount, enclosure, adapter, printed part, ...) as a native, editable Onshape Part Studio. Writes an fsgen Part Studio script, checks and previews it locally at no Onshape API cost, then pastes it or pushes it to Onshape as a real feature tree. Use when the user asks to design, model or CAD a part for Onshape or for 3D printing.
argument-hint: <part description>
allowed-tools: Bash(fsgen --version), Bash(fsgen spec:*), Bash(fsgen rules), Bash(fsgen usage), Bash(fsgen studio check:*), Bash(fsgen studio build:*), Bash(fsgen studio fit:*), Bash(fsgen studio view:*)
---

Make this part: $ARGUMENTS

You are the designer: you write the Part Studio script; the `fsgen` command checks it, builds it locally and
delivers it to Onshape. Work in the user's current folder: scripts go in `parts/`, fsgen writes outputs to `out/`.

0. Run `fsgen --version`. If the command is missing, stop and tell the user to install it (Python 3.11–3.14):
   `uv tool install onshape-fsgen` or `pipx install onshape-fsgen`.
1. Run `fsgen spec` and follow it: the script dialect, the user's modelling rules, the feature catalog and verified
   examples.
2. If the request names a real product, look up its dimensions first. List your assumptions (every dimension or
   interpretation the request doesn't state) and show them to the user before designing. Briefly state the plan:
   variables, sketch planes, feature order. Write the script to `parts/<short-name>.fs`.
3. Run `fsgen studio build parts/<short-name>.fs`. Fix errors and warnings, then look at the preview
   `out/<short-name>/<short-name>.png` and check it against the request (size, holes, fillets, one part).
   Features listed as "not built locally" are NOT in the preview: say so, don't claim they are checked.
   For printed parts follow the 3D-printing rules from the spec and read the print check the build prints
   (overhangs flatter than 45° off the bed).
   Several parts that go together (hinge, snap fit, lid, a part around a real component the user supplied as STEP):
   write `parts/<short-name>.fit.toml` (format: `fsgen spec --fit`) and run `fsgen studio fit parts/<short-name>.fs`;
   give each motion an `expect`. Fix overlaps before showing the result. `fsgen studio view parts/<short-name>.fs`
   writes an interactive 3D viewer to show the user.
   Repeat until it is right. This step costs no Onshape calls.
4. Show the user the result (key dimensions, variables, preview path) and the push estimate the build printed.
   Then ask: paste (free) or push as a native, editable feature tree (costs the estimated calls)?
5. Paste: `fsgen studio paste parts/<short-name>.fs --name "<Part name>"` writes a custom feature (and copies it to
   the clipboard on macOS): in Onshape, create a Feature Studio, paste it, and insert the feature in a Part Studio.
   Push: `fsgen studio push parts/<short-name>.fs --name "<Part name>" --metrics --budget <estimate + 2>`.
   Later edits: change the script and push again; unchanged features are not re-sent.

## Onshape API budget

Onshape limits API calls per year (2500 on education plans), so calls are the scarcest resource. Steps 1–3 and
paste cost 0 calls. Never push without the user's go-ahead on the estimate, and always pass `--budget`.
`fsgen usage` shows the calls used and left.

## Setup the user does (never ask for API keys in the chat)

- Push needs Onshape API keys (Onshape: My Account → Developer → API keys). The user puts `ONSHAPE_ACCESS_KEY` and
  `ONSHAPE_SECRET_KEY` in `~/.config/fsgen/.env` (or a `.env` in the project folder); on macOS
  `fsgen keys store` then moves them into the Keychain.
- Their own modelling rules: `fsgen rules --init` copies the default rules to `~/.config/fsgen/rules.md` to edit;
  `fsgen rules` shows which file is used.
