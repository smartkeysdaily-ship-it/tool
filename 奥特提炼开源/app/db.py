from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.models import BlocklistEntry, JobRecord, JobState, TimelineEntry

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    state           TEXT NOT NULL,
    attempt_login   INTEGER NOT NULL DEFAULT 0,
    attempt_pipeline INTEGER NOT NULL DEFAULT 0,
    held_flag       INTEGER NOT NULL DEFAULT 0,
    origin_job_id   TEXT,
    last_error      TEXT,
    access_token    TEXT NOT NULL DEFAULT '',
    timeline_json   TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL,
    started_at      REAL,
    ended_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_email ON jobs(email);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS blocklist (
    email             TEXT PRIMARY KEY COLLATE NOCASE,
    reason            TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    fail_count_at_add INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS stat_events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stat_events_at ON stat_events(at);
"""


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        cur = await self.conn.execute("PRAGMA table_info(jobs)")
        cols = {row[1] for row in await cur.fetchall()}
        if "access_token" not in cols:
            await self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN access_token TEXT NOT NULL DEFAULT ''"
            )
        # 一次性回填统计事件（首次建表时，把已有任务的历史计入统计）
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM stat_events")
        row = await cur.fetchone()
        if row and int(row["c"]) == 0:
            await self.conn.execute(
                """
                INSERT INTO stat_events (kind, at)
                SELECT 'done', COALESCE(ended_at, created_at) FROM jobs WHERE state = 'done'
                """
            )
            await self.conn.execute(
                """
                INSERT INTO stat_events (kind, at)
                SELECT 'failed', COALESCE(ended_at, created_at) FROM jobs WHERE state IN ('failed','stale')
                """
            )
        await self.conn.commit()

    async def record_stat_event(self, kind: str) -> None:
        """记录一次成功/失败事件（用于持久化统计，不受清任务影响）。"""
        try:
            await self.conn.execute(
                "INSERT INTO stat_events (kind, at) VALUES (?, ?)",
                (str(kind), time.time()),
            )
            await self.conn.commit()
        except Exception:
            pass

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    async def upsert_job(self, job: JobRecord) -> None:
        timeline = json.dumps(
            [t.model_dump(mode="json") for t in job.timeline], ensure_ascii=False
        )
        await self.conn.execute(
            """
            INSERT INTO jobs (
                job_id, email, state, attempt_login, attempt_pipeline,
                held_flag, origin_job_id, last_error, access_token, timeline_json,
                created_at, started_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                email=excluded.email,
                state=excluded.state,
                attempt_login=excluded.attempt_login,
                attempt_pipeline=excluded.attempt_pipeline,
                held_flag=excluded.held_flag,
                origin_job_id=excluded.origin_job_id,
                last_error=excluded.last_error,
                access_token=excluded.access_token,
                timeline_json=excluded.timeline_json,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at
            """,
            (
                job.job_id,
                job.email,
                job.state.value,
                job.attempt_login,
                job.attempt_pipeline,
                1 if job.held_flag else 0,
                job.origin_job_id,
                job.last_error,
                job.access_token,
                timeline,
                job.created_at,
                job.started_at,
                job.ended_at,
            ),
        )
        await self.conn.commit()

    def _row_to_job(self, row: aiosqlite.Row) -> JobRecord:
        timeline_raw = json.loads(row["timeline_json"] or "[]")
        timeline = [TimelineEntry.model_validate(t) for t in timeline_raw]
        return JobRecord(
            job_id=row["job_id"],
            email=row["email"],
            password="",
            totp_secret="",
            access_token=row["access_token"] or "",
            state=JobState(row["state"]),
            attempt_login=row["attempt_login"],
            attempt_pipeline=row["attempt_pipeline"],
            held_flag=bool(row["held_flag"]),
            origin_job_id=row["origin_job_id"],
            last_error=row["last_error"],
            timeline=timeline,
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            last_progress_at=row["ended_at"] or row["started_at"] or row["created_at"] or 0.0,
        )

    async def list_jobs(self) -> list[JobRecord]:
        cur = await self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [self._row_to_job(r) for r in rows]

    async def get_job(self, job_id: str) -> Optional[JobRecord]:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = await cur.fetchone()
        return self._row_to_job(row) if row else None

    async def stats_summary(self) -> dict[str, int]:
        """成功/失败统计：总量 + 最近 24 小时（来自持久化事件表，清任务不丢）。"""
        now = time.time()
        since = now - 86400

        async def count(kind: str, since_ts: float | None = None) -> int:
            sql = "SELECT COUNT(*) AS c FROM stat_events WHERE kind = ?"
            params: list = [kind]
            if since_ts is not None:
                sql += " AND at >= ?"
                params.append(since_ts)
            cur = await self.conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            return int(row["c"]) if row else 0

        done_total = await count("done")
        fail_total = await count("failed")
        done_24h = await count("done", since)
        fail_24h = await count("failed", since)
        return {
            "done_total": done_total,
            "fail_total": fail_total,
            "done_24h": done_24h,
            "fail_24h": fail_24h,
            "success_rate": round(
                (done_total / (done_total + fail_total) * 100), 1
            ) if (done_total + fail_total) else 0.0,
        }

    async def delete_job(self, job_id: str) -> bool:
        cur = await self.conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def blocklist_emails(self) -> set[str]:
        cur = await self.conn.execute("SELECT email FROM blocklist")
        rows = await cur.fetchall()
        return {str(r["email"]).lower() for r in rows}

    async def add_blocklist(
        self,
        email: str,
        reason: str,
        fail_count_at_add: int = 0,
        notes: str | None = None,
    ) -> BlocklistEntry:
        entry = BlocklistEntry(
            email=email.lower().strip(),
            reason=reason,
            created_at=int(time.time()),
            fail_count_at_add=fail_count_at_add,
            notes=notes,
        )
        await self.conn.execute(
            """
            INSERT INTO blocklist (email, reason, created_at, fail_count_at_add, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                entry.email,
                entry.reason,
                entry.created_at,
                entry.fail_count_at_add,
                entry.notes,
            ),
        )
        await self.conn.commit()
        return entry

    async def remove_blocklist(self, email: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM blocklist WHERE email = ? COLLATE NOCASE",
            (email.lower().strip(),),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_blocklist(self) -> list[BlocklistEntry]:
        cur = await self.conn.execute("SELECT * FROM blocklist ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [
            BlocklistEntry(
                email=r["email"],
                reason=r["reason"],
                created_at=r["created_at"],
                fail_count_at_add=r["fail_count_at_add"] or 0,
                notes=r["notes"],
            )
            for r in rows
        ]

    async def consecutive_fail_count(self, email: str) -> int:
        cur = await self.conn.execute(
            """
            SELECT state FROM jobs
            WHERE email = ? COLLATE NOCASE
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (email.lower().strip(),),
        )
        rows = await cur.fetchall()
        streak = 0
        for r in rows:
            if r["state"] == JobState.FAILED.value:
                streak += 1
            else:
                break
        return streak

    async def get_blocklist_entry(self, email: str) -> Optional[BlocklistEntry]:
        cur = await self.conn.execute(
            "SELECT * FROM blocklist WHERE email = ? COLLATE NOCASE",
            (email.lower().strip(),),
        )
        r = await cur.fetchone()
        if not r:
            return None
        return BlocklistEntry(
            email=r["email"],
            reason=r["reason"],
            created_at=r["created_at"],
            fail_count_at_add=r["fail_count_at_add"] or 0,
            notes=r["notes"],
        )
