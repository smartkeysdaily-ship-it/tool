from __future__ import annotations

import asyncio
from pathlib import Path

from app.models import JobRecord

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


class ResultWriter:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or RESULTS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._written_emails: set[str] = set()

    def _path(self, kind: str) -> Path:
        if kind == "plus":
            name = "plus_accounts.txt"
        elif kind == "done":
            name = "done_accounts.txt"
        else:
            name = "free_accounts.txt"  # legacy
        return self.root / name

    async def write_result(self, job: JobRecord, kind: str) -> bool:
        async with self._lock:
            email = job.email.lower()
            if email in self._written_emails:
                return False
            path = self._path(kind)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = f"{job.email}|{job.password}|{job.totp_secret}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            self._written_emails.add(email)
            return True

    async def read_result(self, kind: str) -> tuple[str, int]:
        path = self._path(kind)
        if not path.exists():
            return "", 0
        content = path.read_text(encoding="utf-8")
        lines = [ln for ln in content.splitlines() if ln.strip()]
        return content, len(lines)

    async def clear_outputs(self, kinds: tuple[str, ...] = ("done", "plus", "free")) -> dict[str, int]:
        """Truncate result files and reset in-memory dedupe set."""
        cleared: dict[str, int] = {}
        async with self._lock:
            for kind in kinds:
                path = self._path(kind)
                n = 0
                if path.exists():
                    try:
                        n = sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
                    except OSError:
                        n = 0
                    path.write_text("", encoding="utf-8")
                cleared[kind] = n
            self._written_emails.clear()
        return cleared
