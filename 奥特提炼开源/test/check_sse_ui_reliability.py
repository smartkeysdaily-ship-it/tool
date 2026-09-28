#!/usr/bin/env python3
"""Assert UI has seq-gap, poll fallback, resync, visibility sync."""
from __future__ import annotations

from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"
JOBS = Path(__file__).resolve().parent.parent / "app" / "routes" / "jobs.py"
SSE = Path(__file__).resolve().parent.parent / "app" / "sse.py"


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    jobs = JOBS.read_text(encoding="utf-8")
    sse = SSE.read_text(encoding="utf-8")
    need = [
        (html, "pollSync", "poll fallback"),
        (html, "forceSync", "force sync"),
        (html, "seqGap", "seq gap detect"),
        (html, "addEventListener('resync'", "resync listener"),
        (html, "visibilitychange", "visibility sync"),
        (html, "X-Jobs-Seq", "read jobs seq header"),
        (html, "_lastSeq", "track last seq"),
        (jobs, "X-Jobs-Seq", "emit jobs seq header"),
        (sse, '"seq": seq', "sse seq field"),
        (sse, '_format("resync"', "resync event"),
    ]
    failed = [label for src, needle, label in need if needle not in src]
    if failed:
        print("FAIL missing:", ", ".join(failed))
        return 1
    print("OK sse reliability markers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
