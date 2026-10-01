---
description: Design a part as an fsgen Part Studio script, check it locally, then paste or push to Onshape
argument-hint: <part description>
---

Make this part: $ARGUMENTS

Follow the "Making a part in chat" workflow in CLAUDE.md:

1. Run `.venv/bin/fsgen spec` and follow it (dialect, the user's rules, feature catalog, examples).
2. If the request names a real product, look up its dimensions online first. List your assumptions (every
   dimension or interpretation the request doesn't state) and show them to the user before designing.
   Write the script to `parts/<short-name>.fs`. Briefly state the plan first: variables, sketch planes, feature order.
3. Run `.venv/bin/fsgen studio build parts/<short-name>.fs`. Fix errors and warnings, then look at the
   preview `out/<short-name>/<short-name>.png` and check it against the request (size, holes, fillets, one part).
   Features listed as "not built locally" are NOT in the preview: say so, don't claim they are checked.
   For printed parts follow the 3D-printing rules (clearances, print orientation, snap fits).
   Repeat until it is right. This step costs no Onshape calls.
4. Show the user the result (key dimensions, parameters, preview path) and the push estimate the command printed.
   Then ask: paste (free) or push as a native tree (costs the estimated calls)?
5. Paste: `.venv/bin/fsgen studio paste parts/<short-name>.fs --name "<Part name>"` (copies to clipboard and Dropbox).
   Push: `.venv/bin/fsgen studio push parts/<short-name>.fs --name "<Part name>" --metrics --budget <estimate + 2>`.
6. Note anything new you learned in docs/FINDINGS.md.
