"""Minimal Onshape REST client using API-key basic auth."""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass

import requests

from .paths import ledger_path, load_env

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


LEDGER = ledger_path()
KEYCHAIN_SERVICE = "fsgen-onshape"  # macOS Keychain item holding "access:secret" (fsgen keys store)


def _keychain(*args: str) -> subprocess.CompletedProcess | None:
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None


def keychain_has_keys() -> bool:
    """Whether the Keychain item exists (reading its attributes does not ask for permission)."""
    r = _keychain("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "onshape")
    return r is not None and r.returncode == 0


def keychain_keys() -> tuple[str, str] | None:
    """The API keys from the Keychain. The item trusts no app, so macOS asks every time (one prompt per run)."""
    r = _keychain("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "onshape", "-w")
    if r is None or r.returncode != 0 or ":" not in r.stdout:
        return None
    ak, sk = r.stdout.strip().split(":", 1)
    return ak, sk


def keychain_store(access_key: str, secret_key: str):
    r = _keychain("add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", "onshape", "-l", "fsgen Onshape API keys",
                  "-T", "", "-w", f"{access_key}:{secret_key}")
    if r is None or r.returncode != 0:
        raise OnshapeError(f"could not store the keys in the Keychain: {r.stderr.strip() if r else 'not macOS'}")


def calls_today() -> int:
    """Counted calls logged today by any fsgen run (the daily cap counts across processes)."""
    if not isinstance(LEDGER, Path) or not LEDGER.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    n = 0
    for line in LEDGER.read_text().splitlines():
        if line.startswith('{"t": "' + today) and '"counted": true' in line:
            n += 1
    return n


def notify(title: str, message: str):
    """macOS notification (off in tests, on other systems, or with FSGEN_NOTIFY=0)."""
    if sys.platform != "darwin" or os.environ.get("FSGEN_NOTIFY", "1") == "0" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


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
    (each redirect hop counts). 4xx/5xx are free. Every hop is appended to the ledger (paths.ledger_path) and
    counted in `self.calls`; `budget` (or ONSHAPE_RUN_BUDGET) stops a run before it overspends.
    """

    def __init__(self, access_key: str | None = None, secret_key: str | None = None,
                 base_url: str | None = None, budget: int | None = None, purpose: str = ""):
        load_env()
        self.base = (base_url or os.environ.get("ONSHAPE_BASE_URL", "https://cad.onshape.com")).rstrip("/")
        ak = access_key or os.environ.get("ONSHAPE_ACCESS_KEY")
        sk = secret_key or os.environ.get("ONSHAPE_SECRET_KEY")
        if not (ak and sk) and not keychain_has_keys():
            raise OnshapeError("Onshape API keys not found: set ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY in .env "
                               "(see .env.example) or store them with `fsgen keys store`")
        self.remaining: dict[str, str | None] = {}  # endpoint family -> X-Rate-Limit-Remaining
        self._auth = (ak, sk) if ak and sk else None  # Keychain keys are read at the first call, not before
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json;charset=UTF-8; qs=0.09"})
        self.calls = 0  # counted (2xx/3xx) calls made by this client
        self.by_endpoint: dict[str, int] = {}
        env_budget = os.environ.get("ONSHAPE_RUN_BUDGET")
        self.budget = budget if budget is not None else (int(env_budget) if env_budget else None)
        daily = os.environ.get("FSGEN_DAILY_BUDGET")
        self.daily_budget = int(daily) if daily else None
        self._today_before: int | None = None  # counted calls logged today before this client's first call
        self._notify_registered = False
        self.purpose = purpose
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    @property
    def auth(self) -> tuple[str, str]:
        if self._auth is None:
            self._auth = keychain_keys()
            if self._auth is None:
                raise OnshapeError("could not read the Onshape API keys from the Keychain (access denied?)")
        return self._auth

    def _notify_at_exit(self):
        if self.calls == 0:
            return
        msg = f"{self.calls} call{'s' if self.calls != 1 else ''}" + (f" ({self.purpose})" if self.purpose else "")
        try:
            from .usage import allocation_status
            a = allocation_status()
            msg += f"; today {calls_today()}; year {a.used}/{a.allocation}"
        except Exception:
            pass
        notify("fsgen used the Onshape API", msg)

    def _record(self, method, url, status):
        counted = status < 400 and url.startswith(self.base)
        if counted:
            self.calls += 1
            if not self._notify_registered:
                atexit.register(self._notify_at_exit)
                self._notify_registered = True
            key = endpoint_key(method, url)
            self.by_endpoint[key] = self.by_endpoint.get(key, 0) + 1
        try:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
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
            if self.daily_budget is not None and url.startswith(self.base):
                if self._today_before is None:
                    self._today_before = calls_today()
                if self._today_before + self.calls >= self.daily_budget:
                    raise BudgetExceeded(f"daily cap of {self.daily_budget} Onshape calls reached "
                                         f"({self._today_before + self.calls} today). Raise FSGEN_DAILY_BUDGET in "
                                         ".env if this is intended.")
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
