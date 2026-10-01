#!/usr/bin/env python3
"""onshape_translate.py — use Onshape's own translators as an independent CAD-file -> STEP decoder.

cad_verify.py reads native .sldprt / .f3d files with cadmpeg, an open-source clean-room decoder
that rates its own SolidWorks support L1. Onshape imports those formats with licensed commercial
translators. Running a file through both and requiring each STEP to pass the IoU gate means the
geometry verdict does not rest on either decoder alone, and when they disagree it says which one to
suspect.

    python onshape_translate.py part.SLDPRT [-o part.onshape.step]

Uploads go to one reusable document ("featuretree-verify"; its ids are cached in
~/.config/featuretree/onshape_verify.json). FREE ONSHAPE ACCOUNTS CAN ONLY CREATE PUBLIC
DOCUMENTS: anything uploaded there is publicly visible. Upload only files you may publish.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onshape_client as oc  # noqa: E402

CACHE = Path.home() / ".config" / "featuretree" / "onshape_verify.json"
DOC_NAME = "featuretree-verify"


def verify_doc():
    """(did, wid) of the reusable verification document, creating it on first use."""
    if CACHE.exists():
        c = json.loads(CACHE.read_text())
        return c["did"], c["wid"]
    try:
        d = oc.create_document(DOC_NAME, public=False)
    except SystemExit as e:                  # free plan: private documents are refused
        print(f"  (private document refused: {str(e)[:120]}) -> creating a PUBLIC one")
        d = oc.create_document(DOC_NAME, public=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"did": d["did"], "wid": d["wid"]}))
    print(f"  verification document: https://cad.onshape.com/documents/{d['did']}")
    return d["did"], d["wid"]


def _wait(tid, what, timeout=600):
    t0 = time.time()
    while True:
        st = oc.request("GET", f"/api/translations/{tid}")
        state = st.get("requestState")
        if state == "DONE":
            return st
        if state == "FAILED":
            raise SystemExit(f"Onshape {what} failed: {st.get('failureReason') or st}")
        if time.time() - t0 > timeout:
            raise SystemExit(f"Onshape {what} still {state} after {timeout}s")
        time.sleep(5)                        # status polls count against the rate limit


def import_file(path, did, wid):
    """Upload + translate a CAD file; returns the id of the Part Studio it became. The file's own
    coordinates are kept (yAxisIsUp false), so the part stays in the frame the IR was built in.
    (An import must be stored in the document: storeInDocument=false makes Onshape answer 500.)"""
    body, ctype = oc.multipart({"translate": "true", "flattenAssemblies": "true", "yAxisIsUp": "false",
                                "encodedFilename": Path(path).name},
                               "file", Path(path).name, Path(path).read_bytes())
    r = json.loads(oc.request_raw("POST", f"/api/translations/d/{did}/w/{wid}", data=body, ctype=ctype))
    st = _wait(r["id"], f"import of {Path(path).name}")
    ids = st.get("resultElementIds") or []
    if not ids:
        raise SystemExit(f"Onshape import produced no element: {st}")
    els = {e["id"]: e for e in oc.request("GET", f"/api/documents/d/{did}/w/{wid}/elements")}
    studios = [i for i in ids if els.get(i, {}).get("elementType") == "PARTSTUDIO"]
    return (studios or ids)[0]


def export_step(did, wid, eid, out):
    r = oc.request("POST", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/translations",
                   body={"formatName": "STEP", "storeInDocument": False})
    st = _wait(r["id"], "STEP export")
    fid = (st.get("resultExternalDataIds") or [None])[0]
    if not fid:
        raise SystemExit(f"Onshape STEP export returned no file: {st}")
    Path(out).write_bytes(oc.request_raw("GET", f"/api/documents/d/{did}/externaldata/{fid}",
                                         accept="application/octet-stream"))
    return Path(out)


def translate(path, out=None):
    """CAD file -> STEP via Onshape. Returns (step path, Onshape element URL)."""
    out = out or Path(path).with_suffix(Path(path).suffix + ".onshape.step")
    did, wid = verify_doc()
    eid = import_file(path, did, wid)
    return export_step(did, wid, eid, out), f"https://cad.onshape.com/documents/{did}/w/{wid}/e/{eid}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    step, url = translate(a.file, a.out)
    print(f"wrote {step}\nimported as {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
