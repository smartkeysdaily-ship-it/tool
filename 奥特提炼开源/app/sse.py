from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Callable


class JobStream:
    """SSE fan-out đáng tin cậy: seq monotonic, snapshot, ping+seq, resync khi backpressure."""

    HEARTBEAT_SEC = 10.0
    QUEUE_SIZE = 512

    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.Queue[str | None], str]] = []
        self._lock = asyncio.Lock()
        self._seq = 0
        self._snapshot_fn: Callable[[], list[dict[str, Any]]] | None = None

    @property
    def seq(self) -> int:
        return self._seq

    def set_snapshot(self, fn: Callable[[], list[dict[str, Any]]]) -> None:
        self._snapshot_fn = fn

    def _snapshot(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if not self._snapshot_fn:
            return []
        snap = self._snapshot_fn()
        snap = snap if isinstance(snap, list) else []
        if session_id is None:
            return snap
        sid = session_id or ""
        return [
            j for j in snap
            if isinstance(j, dict) and (j.get("session_id") or "") == sid
        ]

    def _bump(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _format(event: str, data: Any) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        # id: cho Last-Event-ID (browser EventSource)
        eid = ""
        if isinstance(data, dict) and data.get("seq") is not None:
            eid = f"id: {data['seq']}\n"
        return f"{eid}event: {event}\ndata: {payload}\n\n"

    async def subscribe(self, session_id: str | None = None) -> AsyncIterator[str]:
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        async with self._lock:
            self._subs.append((q, session_id or ""))
            seq = self._seq
        try:
            # Snapshot luôn authoritative lúc connect — che gap khi reconnect
            yield self._format(
                "snapshot",
                {"type": "snapshot", "seq": seq, "jobs": self._snapshot(session_id)},
            )
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=self.HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    async with self._lock:
                        cur = self._seq
                    yield self._format("ping", {"type": "ping", "seq": cur})
                    continue
                if item is None:
                    break
                yield item
        finally:
            async with self._lock:
                self._subs = [(qq, sid) for (qq, sid) in self._subs if qq is not q]

    def _resync_chunk(self, seq: int) -> str:
        return self._format("resync", {"type": "resync", "seq": seq})

    async def _fanout(self, chunk: str, seq: int, session_id: str | None = None) -> None:
        async with self._lock:
            subs = list(self._subs)
        for q, sid in subs:
            if session_id is not None and (sid or "") != (session_id or ""):
                continue
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                # Backpressure: bỏ event cũ, bắt client full-resync — không silent-drop
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(self._resync_chunk(seq))
                except asyncio.QueueFull:
                    pass

    async def publish(self, record: dict[str, Any], session_id: str | None = None) -> int:
        async with self._lock:
            seq = self._bump()
        body = {**record, "seq": seq}
        # deleted giữ type; job thường không có type
        event = "job"
        if record.get("type") == "deleted":
            event = "job"
        # 若 record 自带 session_id 且未显式指定，按其路由
        if session_id is None and isinstance(record, dict):
            session_id = record.get("session_id", None)
        await self._fanout(self._format(event, body), seq, session_id)
        return seq

    async def publish_bulk(self, summary: dict[str, Any], session_id: str | None = None) -> int:
        jobs = self._snapshot(session_id if session_id is not None else None)
        async with self._lock:
            seq = self._bump()
        body = {**summary, "type": "bulk", "seq": seq, "jobs": jobs}
        await self._fanout(self._format("bulk", body), seq, session_id)
        return seq
