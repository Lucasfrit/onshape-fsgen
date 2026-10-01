"""Minimal Onshape REST client using API-key basic auth."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

API_VERSION = "v10"


@dataclass
class ElementRef:
    did: str
    wid: str
    eid: str

    @classmethod
    def from_url(cls, url: str) -> "ElementRef":
        m = re.search(r"documents/([0-9a-f]{24})/w/([0-9a-f]{24})/e/([0-9a-f]{24})", url)
        if not m:
            raise ValueError(f"not an Onshape element URL: {url}")
        return cls(*m.groups())

    @property
    def dwe(self) -> str:
        return f"d/{self.did}/w/{self.wid}/e/{self.eid}"

    def url(self, base: str = "https://cad.onshape.com") -> str:
        return f"{base}/documents/{self.did}/w/{self.wid}/e/{self.eid}"


class OnshapeError(RuntimeError):
    pass


class RateLimited(OnshapeError):
    def __init__(self, url, retry_after):
        hours = retry_after / 3600
        super().__init__(f"Onshape rate limit hit for {url.split('/api/')[-1].split('?')[0]}; "
                         f"retry after {retry_after:.0f}s ({hours:.1f} h). Use the local engine meanwhile.")
        self.retry_after = retry_after


class BudgetExceeded(OnshapeError):
    pass


LEDGER = Path(".fsgen_api_ledger.jsonl")
_ID = re.compile(r"/[0-9a-f]{24}(?=/|$)")


def endpoint_key(method: str, url: str) -> str:
    """'POST partstudios/d/{id}/w/{id}/e/{id}/features' style key for accounting."""
    path = url.split("?")[0]
    path = path.split(f"/api/{API_VERSION}/")[-1] if "/api/" in path else "external:" + path.split("/")[2]
    path = _ID.sub("/{id}", "/" + path).lstrip("/")
    path = re.sub(r"featureid/[^/]+", "featureid/{fid}", path)
    path = re.sub(r"translations/[^/]+", "translations/{tid}", path)
    path = re.sub(r"externaldata/[^/]+", "externaldata/{xid}", path)
    return f"{method} {path}"


class Onshape:
    """Onshape REST client with call accounting.

    Onshape's annual allocation (2500 calls/year on EDU) counts every request answered with 2xx or 3xx
    (each redirect hop counts). 4xx/5xx are free. Every hop is appended to .fsgen_api_ledger.jsonl and
    counted in `self.calls`; `budget` (or ONSHAPE_RUN_BUDGET) stops a run before it overspends.
    """

    def __init__(self, access_key: str | None = None, secret_key: str | None = None,
                 base_url: str | None = None, budget: int | None = None, purpose: str = ""):
        load_dotenv()
        self.base = (base_url or os.environ.get("ONSHAPE_BASE_URL", "https://cad.onshape.com")).rstrip("/")
        ak = access_key or os.environ.get("ONSHAPE_ACCESS_KEY")
        sk = secret_key or os.environ.get("ONSHAPE_SECRET_KEY")
        if not (ak and sk):
            raise OnshapeError("ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY not set (see .env.example)")
        self.remaining: dict[str, str | None] = {}  # endpoint family -> X-Rate-Limit-Remaining
        self.auth = (ak, sk)
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json;charset=UTF-8; qs=0.09"})
        self.calls = 0  # counted (2xx/3xx) calls made by this client
        self.by_endpoint: dict[str, int] = {}
        env_budget = os.environ.get("ONSHAPE_RUN_BUDGET")
        self.budget = budget if budget is not None else (int(env_budget) if env_budget else None)
        self.purpose = purpose
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def _record(self, method, url, status):
        counted = status < 400 and url.startswith(self.base)
        if counted:
            self.calls += 1
            key = endpoint_key(method, url)
            self.by_endpoint[key] = self.by_endpoint.get(key, 0) + 1
        try:
            with LEDGER.open("a") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": self.run_id,
                                    "purpose": self.purpose, "call": endpoint_key(method, url),
                                    "status": status, "counted": counted}) + "\n")
        except OSError:
            pass

    def summary(self) -> str:
        top = sorted(self.by_endpoint.items(), key=lambda kv: -kv[1])
        return f"Onshape API calls this run: {self.calls}" + "".join(f"\n  {n:4d}  {k}" for k, n in top)

    def request(self, method: str, path: str, *, json=None, params=None, raw=False,
                accept: str | None = None, allow_redirects=True):
        url = path if path.startswith("http") else f"{self.base}/api/{API_VERSION}/{path.lstrip('/')}"
        headers = {"Accept": accept} if accept else None
        for attempt in range(5):
            if self.budget is not None and self.calls >= self.budget and url.startswith(self.base):
                raise BudgetExceeded(f"run budget of {self.budget} Onshape calls reached ({self.summary()})")
            # Exports redirect to another host: follow manually, and only send credentials to Onshape.
            r = self.s.request(method, url, json=json, params=params, headers=headers,
                               auth=self.auth if url.startswith(self.base) else None,
                               allow_redirects=False, timeout=120)
            self._record(method, url, r.status_code)
            if r.status_code in (301, 302, 303, 307, 308) and allow_redirects:
                url, params = r.headers["Location"], None
                if r.status_code == 303:
                    method, json = "GET", None
                continue
            if r.status_code == 429:  # rate limited; limits are per endpoint and can be daily
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                if wait > 30:
                    raise RateLimited(url, wait)
                time.sleep(wait)
                continue
            break
        self.remaining[url.split(f"/api/{API_VERSION}/")[-1].split("/")[0]] = r.headers.get("X-Rate-Limit-Remaining")
        if r.status_code >= 400:
            raise OnshapeError(f"{method} {url} -> {r.status_code}: {r.text[:2000]}")
        if raw:
            return r
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)
