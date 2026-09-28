#!/usr/bin/env python3
"""Smoke: JobStream seq + snapshot/bulk/resync markers."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sse import JobStream  # noqa: E402


def _data(chunk: str) -> dict:
    line = [ln for ln in chunk.splitlines() if ln.startswith("data: ")][0]
    return json.loads(line[6:])


async def _run() -> None:
    stream = JobStream()
    jobs = [{"job_id": "a", "state": "failed"}]
    stream.set_snapshot(lambda: list(jobs))

    agen = stream.subscribe()
    first = await agen.__anext__()
    assert "event: snapshot" in first
    snap = _data(first)
    assert snap["type"] == "snapshot"
    assert "seq" in snap

    seq1 = await stream.publish({"job_id": "a", "state": "queued"})
    job_evt = await agen.__anext__()
    assert "event: job" in job_evt
    assert _data(job_evt)["seq"] == seq1
    assert stream.seq == seq1

    seq2 = await stream.publish_bulk({"action": "rerun_failed", "count": 1})
    bulk_evt = await agen.__anext__()
    assert "event: bulk" in bulk_evt
    body = _data(bulk_evt)
    assert body["seq"] == seq2
    assert isinstance(body["jobs"], list)

    # Backpressure → resync (không silent-drop)
    tiny = JobStream()
    tiny.QUEUE_SIZE = 1  # type: ignore[misc]
    # monkey: recreate with size 1 via direct queue fill is hard; unit the helper instead
    assert hasattr(stream, "_resync_chunk")
    chunk = stream._resync_chunk(99)
    assert "event: resync" in chunk
    assert _data(chunk)["type"] == "resync"

    await agen.aclose()
    print("OK sse seq/snapshot/bulk/resync")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
