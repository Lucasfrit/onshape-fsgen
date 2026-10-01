"""featuretree_fusion_read — Fusion script: read the active design's featuretree parameters back.

Every length / depth / radius / angle a featuretree script creates is a user parameter whose
comment is "featuretree:<feature>:<key>". This writes them, keyed by IR feature name, to
<design>.fusion.params.json next to this script; then on the featuretree side:

    python script_emit.py apply <part>.ir.json <design>.fusion.params.json
"""
import json
import math
import os
import traceback

import adsk.core
import adsk.fusion


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        params = {}
        for p in design.userParameters:
            parts = (p.comment or "").split(":")
            if len(parts) != 3 or parts[0] != "featuretree":
                continue
            _, feat, key = parts
            v = p.value                             # internal units: cm or radians
            params.setdefault(feat, {})[key] = round(math.degrees(v) if p.unit == "deg" else v * 10.0, 6)
        name = design.rootComponent.name
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".fusion.params.json")
        with open(out, "w") as fh:
            json.dump(params, fh, indent=1)
        ui.messageBox("%d featuretree parameters -> %s" % (sum(len(v) for v in params.values()), out))
    except Exception:
        ui.messageBox("featuretree read failed:\n" + traceback.format_exc())
