#!/usr/bin/env python3
"""Smoke: pause/resume API + done results endpoint."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def main() -> int:
    st, d = call("GET", "/api/jobs/pause-status")
    assert st == 200, d
    assert "paused" in d
    print("OK pause-status", d)

    st, d = call("POST", "/api/jobs/pause")
    assert st == 200 and d.get("paused") is True, d
    print("OK pause", d)

    st, d = call("GET", "/api/jobs/pause-status")
    assert d.get("paused") is True, d

    st, d = call("POST", "/api/jobs/resume")
    assert st == 200 and d.get("paused") is False, d
    print("OK resume", d)

    st, d = call("GET", "/api/results/done")
    assert st == 200 and "content" in d, d
    print("OK results/done")

    html = urllib.request.urlopen(BASE + "/", timeout=10).read().decode()
    assert "filter==='done'" in html or "id:'done'" in html
    assert "Pause" in html
    assert "{id:'free'" not in html
    assert "doneContent" in html
    print("OK UI markers")
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
