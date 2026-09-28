from __future__ import annotations

from typing import Any

from app.config import ConfigStore
from app.db import Database
from app.models import AccountRecord, BlocklistEntry, JobRecord


class BlocklistService:
    def __init__(self, db: Database, config: ConfigStore) -> None:
        self.db = db
        self.config = config

    async def filter_submit(
        self, accounts: list[AccountRecord]
    ) -> tuple[list[AccountRecord], list[dict[str, str]]]:
        cfg = self.config.get()
        if not cfg.blocklist_enabled:
            return accounts, []
        blocked = await self.db.blocklist_emails()
        kept: list[AccountRecord] = []
        skipped: list[dict[str, str]] = []
        for a in accounts:
            if a.email.lower() in blocked:
                skipped.append({"email": a.email, "reason": "email_blocked"})
            else:
                kept.append(a)
        return kept, skipped

    async def on_job_failed(self, job: JobRecord) -> None:
        cfg = self.config.get()
        if not cfg.blocklist_enabled:
            return
        streak = await self.db.consecutive_fail_count(job.email)
        if streak >= cfg.blocklist_auto_threshold:
            existing = await self.db.get_blocklist_entry(job.email)
            if existing:
                return
            await self.db.add_blocklist(
                job.email,
                reason="auto_fail_threshold",
                fail_count_at_add=streak,
            )

    async def add_manual(self, email: str, notes: str | None = None) -> BlocklistEntry:
        existing = await self.db.get_blocklist_entry(email)
        if existing:
            raise LookupError("already_blocked")
        return await self.db.add_blocklist(email, reason="manual", notes=notes)

    async def remove(self, email: str) -> bool:
        return await self.db.remove_blocklist(email)

    async def list_all(self) -> list[BlocklistEntry]:
        return await self.db.list_blocklist()
