"""onshape_client.py — minimal Onshape REST client (API-key HMAC auth), stdlib only.

The featuretree Onshape backend: drives a Part Studio over the REST API so the IR can be
emitted as a native Onshape feature tree (and exported / round-tripped). Auth per Onshape's
scheme: sign (method, nonce, date, content-type, path, query) with HMAC-SHA256(secret), header
`Authorization: On <accessKey>:HmacSHA256:<b64sig>`.

Credentials via env (never commit):
    ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY        (from https://dev-portal.onshape.com)
    or, failing those, onpy's ~/.onpy/config.json {"dev_access": ..., "dev_secret": ...}
    ONSHAPE_BASE   (default https://cad.onshape.com)

    python onshape_client.py whoami               # validate auth
    python onshape_client.py create "flexisette"  # new doc -> prints did/wid/eid
"""
import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import urllib.request
from email.utils import formatdate

BASE = os.environ.get("ONSHAPE_BASE", "https://cad.onshape.com")


def _creds():
    ak = os.environ.get("ONSHAPE_ACCESS_KEY")
    sk = os.environ.get("ONSHAPE_SECRET_KEY")
    cfg = os.path.expanduser("~/.onpy/config.json")
    if (not ak or not sk) and os.path.exists(cfg):      # the same key pair onpy already uses
        with open(cfg) as fh:
            c = json.load(fh)
        ak, sk = c.get("dev_access"), c.get("dev_secret")
    if not ak or not sk:
        raise SystemExit("set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY (dev-portal.onshape.com)")
    return ak, sk


def _nonce():
    return "".join(random.choices(string.ascii_letters + string.digits, k=25))


def _auth_header(method, path, query, ctype, date, nonce):
    ak, sk = _creds()
    s = "\n".join([method, nonce, date, ctype, path, query]).lower() + "\n"
    sig = base64.b64encode(hmac.new(sk.encode(), s.encode(), hashlib.sha256).digest()).decode()
    return f"On {ak}:HmacSHA256:{sig}"


def _open(req, tries=6):
    """urlopen with back-off on 429 (Onshape's API rate limit), honouring Retry-After. Onshape
    signs the Date/nonce into every request, so each retry is re-signed by the caller's rebuild."""
    import time
    for i in range(tries):
        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            wait = float(e.headers.get("Retry-After") or 0) or min(60, 5 * 2 ** i)
            if wait > 600:                       # a daily/annual quota, not a burst limit: stop
                raise SystemExit(f"Onshape API quota exhausted; retry after {wait / 3600:.1f} h") from None
            print(f"  (Onshape rate limit: waiting {wait:.0f}s)", flush=True)
            time.sleep(wait)
            req = req._resign()


def _signed(method, path, query, data, ctype, accept):
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    nonce = _nonce()
    url = BASE + path + (("?" + query) if query else "")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Date", date)
    req.add_header("On-Nonce", nonce)
    req.add_header("Authorization", _auth_header(method, path, query, ctype, date, nonce))
    req.add_header("Content-Type", ctype)
    req.add_header("Accept", accept)
    req._resign = lambda: _signed(method, path, query, data, ctype, accept)
    return req


def request(method, path, query="", body=None):
    """method e.g. 'GET'/'POST'; path begins with /api/...; query is the raw (encoded) string.
    Accept is */* — NOT application/json, which forces the regen-hostile envelope form."""
    data = json.dumps(body).encode() if body is not None else None
    try:
        with _open(_signed(method, path, query, data, "application/json", "*/*")) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt.strip() else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Onshape {method} {path} -> {e.code}: {e.read().decode()[:600]}")


def request_raw(method, path, query="", data=None, ctype="application/json", accept="*/*"):
    """Like request() but for a raw body (e.g. a multipart upload) and a raw bytes response. Onshape
    answers some downloads with a redirect to a presigned URL; urllib follows it, re-sending the
    Authorization header only on the same host."""
    try:
        with _open(_signed(method, path, query, data, ctype, accept)) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Onshape {method} {path} -> {e.code}: {e.read().decode(errors='replace')[:600]}")


def multipart(fields, file_field, filename, payload):
    """(body bytes, content-type) for a multipart/form-data upload of one file plus text fields."""
    boundary = "ft" + _nonce()
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    out.append(payload)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# --- convenience wrappers ---

def whoami():
    return request("GET", "/api/users/sessioninfo")


def create_document(name, public=False):
    doc = request("POST", "/api/documents", body={"name": name, "isPublic": bool(public)})
    did = doc["id"]
    wid = doc["defaultWorkspace"]["id"]
    eid = default_partstudio(did, wid)
    return {"did": did, "wid": wid, "eid": eid, "name": doc["name"]}


def default_partstudio(did, wid):
    els = request("GET", f"/api/documents/d/{did}/w/{wid}/elements")
    ps = [e for e in els if e.get("elementType") == "PARTSTUDIO"]
    if not ps:
        raise SystemExit("no Part Studio in document")
    return ps[0]["id"]


def get_features(did, wid, eid):
    return request("GET", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features")


def eval_featurescript(did, wid, eid, script):
    return request("POST", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/featurescript",
                   body={"script": script, "queries": []})


def face_transient_ids(did, wid, eid, feature_id):
    """Transient ids of the FACE(s) a feature created (e.g. the default plane 'Top') —
    Onshape sketches reference their plane by transient id, not by a query string."""
    script = ("function(context is Context, queries){ return transientQueriesToStrings("
              "evaluateQuery(context, qCreatedBy(makeId(\"%s\"), EntityType.FACE))); }" % feature_id)
    r = eval_featurescript(did, wid, eid, script)
    res = r["result"]
    vals = res.get("message", res).get("value", [])     # robust to envelope or flat form
    return [v.get("message", v).get("value") for v in vals]


def add_feature(did, wid, eid, feature):
    # flat btType feature (with Accept: */*), wrapped as {"feature": ...} — the onpy form.
    return request("POST", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features",
                   body={"feature": feature})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    if cmd == "whoami":
        info = whoami()
        print("authenticated as:", info.get("name"), "<" + str(info.get("email")) + ">")
    elif cmd == "create":
        name = sys.argv[2] if len(sys.argv) > 2 else "featuretree"
        print(json.dumps(create_document(name), indent=2))
    else:
        print(__doc__)
