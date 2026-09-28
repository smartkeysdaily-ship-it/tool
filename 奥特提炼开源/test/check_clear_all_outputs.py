#!/usr/bin/env python3
"""Clear all must wipe done/plus result files."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import JobRecord, JobState  # noqa: E402
from app.result_writer import ResultWriter  # noqa: E402


async def _run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rw = ResultWriter(root=Path(tmp))
        done_job = JobRecord(
            job_id="j1",
            email="done@ex.com",
            password="p",
            totp_secret="JBSWY3DPEHPK3PXP",
            state=JobState.DONE,
        )
        plus_job = JobRecord(
            job_id="j2",
            email="plus@ex.com",
            password="p",
            totp_secret="JBSWY3DPEHPK3PXP",
            state=JobState.DONE,
        )
        assert await rw.write_result(done_job, "done")
        assert await rw.write_result(plus_job, "plus")
        assert (Path(tmp) / "done_accounts.txt").read_text().strip()
        assert (Path(tmp) / "plus_accounts.txt").read_text().strip()

        cleared = await rw.clear_outputs(("done", "plus", "free"))
        assert cleared["done"] == 1
        assert cleared["plus"] == 1
        assert (Path(tmp) / "done_accounts.txt").read_text() == ""
        assert (Path(tmp) / "plus_accounts.txt").read_text() == ""

        # Dedupe reset — có thể ghi lại cùng email
        assert await rw.write_result(done_job, "done") is True

    src = (ROOT / "app" / "job_manager.py").read_text(encoding="utf-8")
    assert 'clear_outputs(("done", "plus", "free"))' in src
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "Done + Plus output" in html
    print("OK clear_all wipes done/plus outputs")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
