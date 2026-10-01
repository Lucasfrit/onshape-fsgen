"""Offline cost model: run the real client code against a fake Onshape and count billable calls.

Billable = 2xx/3xx answers from cad.onshape.com (Onshape's rule); the fake answers like the real API
(export redirects to an external host, translations take a few seconds) and time.sleep is simulated.
"""
from __future__ import annotations

import base64
import itertools
import json
import re
from contextlib import contextmanager
from unittest import mock

import requests

_PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()


class FakeOnshape:
    def __init__(self, translation_seconds: float = 6.0, fail_features: set[str] | None = None):
        self.ids = itertools.count(1)
        self.clock = 0.0
        self.translation_seconds = translation_seconds
        self.started: dict[str, float] = {}
        self.fail_features = fail_features or set()

    def sleep(self, s):
        self.clock += s

    def _resp(self, status=200, body=None, headers=None, content=None):
        r = requests.Response()
        r.status_code = status
        r._content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        r.headers.update(headers or {})
        return r

    def request(self, method, url, json=None, params=None, headers=None, auth=None, allow_redirects=False,
                timeout=None):
        if not url.startswith("https://cad.onshape.com"):
            return self._resp(content=b"solid fake")  # external storage download
        path = url.split("/api/v10/")[-1]
        if method == "POST" and re.fullmatch(r"(partstudios/d/\w+/w/\w+|variables/d/\w+/w/\w+/variablestudio)", path):
            return self._resp(body={"id": f"{next(self.ids):024x}"})
        if path.endswith("/features") and method == "POST" or "/featureid/" in path and method == "POST":
            name = (json or {}).get("feature", {}).get("name", "")
            fid = path.split("featureid/")[1] if "featureid/" in path else f"F{next(self.ids)}"
            status = "ERROR" if name in self.fail_features else "OK"
            return self._resp(body={"feature": {"featureId": fid}, "featureState": {"featureStatus": status}})
        if method == "DELETE":
            return self._resp(body={})
        if path.endswith("/featurescript"):
            return self._resp(body={"result": None, "notices": []})
        if path.endswith("/massproperties"):
            return self._resp(body={"bodies": {"P1": {"volume": [1e-6], "periphery": [1e-4]}}})
        if path.endswith("/shadedviews"):
            return self._resp(body={"images": [_PNG]})
        if "/stl" in path:
            return self._resp(307, headers={"Location": "https://storage.example.com/stl"})
        if path.endswith("/translations"):
            tid = f"t{next(self.ids)}"
            self.started[tid] = self.clock
            return self._resp(body={"id": tid})
        if path.startswith("translations/"):
            tid = path.split("/")[1]
            done = self.clock - self.started[tid] >= self.translation_seconds
            return self._resp(body={"requestState": "DONE" if done else "ACTIVE", "resultExternalDataIds": ["x"]})
        if "/externaldata/" in path:
            return self._resp(307, headers={"Location": "https://storage.example.com/step"})
        return self._resp(body={})


@contextmanager
def fake_onshape(**kw):
    fake = FakeOnshape(**kw)
    with mock.patch("requests.Session.request", lambda self, *a, **k: fake.request(*a, **k)), \
            mock.patch("time.sleep", fake.sleep), mock.patch("fsgen.onshape.LEDGER", mock.MagicMock()):
        yield fake
