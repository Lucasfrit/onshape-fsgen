"""fc_read.py — read a .FCStd back (the round-trip inbound direction).

RUNS UNDER freecadcmd. Env: FC_IN (.FCStd to open). Optional FC_EDIT (json
{label:{key:val}}) applies a human dimension edit, then re-saves. Prints
"RESULT:" + json (tree, volume, params) keyed by feature name.
"""

import json
import os
import sys

import FreeCAD as App

sys.path.insert(0, os.environ.get("FC_LIBDIR", os.path.dirname(os.path.abspath(__file__))))
import fc_common  # noqa: E402

doc = App.openDocument(os.environ["FC_IN"])
edits = json.loads(os.environ.get("FC_EDIT", "{}"))
if edits:
    fc_common.apply_edits(doc, edits)
    doc.save()
print("RESULT:" + json.dumps(fc_common.result(doc)))
