#!/usr/bin/env python3
"""Assert UI + SSE source has draft persist, clear confirm, and live sync."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "static" / "index.html"
SSE = ROOT / "app" / "sse.py"
JOBS = ROOT / "app" / "routes" / "jobs.py"


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    sse = SSE.read_text(encoding="utf-8")
    jobs = JOBS.read_text(encoding="utf-8")
    checks = [
        (html, "plusqr.accountsDraft", "draft key"),
        (html, "restoreDraft()", "restore draft"),
        (html, "persistDraft", "persist draft"),
        (html, "confirmClearInput", "clear confirm"),
        (html, "clearInput()", "clear input"),
        (html, "replaceJobs", "replaceJobs helper"),
        (html, "await this.loadJobs()", "await loadJobs after actions"),
        (html, "addEventListener('snapshot'", "SSE snapshot listener"),
        (html, "addEventListener('bulk'", "SSE bulk listener"),
        (html, "addEventListener('resync'", "SSE resync listener"),
        (html, "pollSync", "poll fallback"),
        (html, "filter = 'active'", "switch to active after retry"),
        (html, "x-init=\"init()\"", "explicit x-init"),
        (sse, '"type": "snapshot"', "SSE snapshot event"),
        (sse, '_format("ping"', "SSE ping event"),
        (sse, '"type": "bulk"', "bulk includes jobs snapshot"),
        (jobs, "X-Accel-Buffering", "SSE no buffering header"),
        (jobs, "X-Jobs-Seq", "jobs seq header"),
    ]
    failed = [label for src, needle, label in checks if needle not in src]
    if failed:
        print("FAIL missing:", ", ".join(failed))
        return 1
    print("OK ui persist + SSE live sync markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
