#!/usr/bin/env python3
"""Unit: 1 email = 1 job on submit (no live server required)."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigStore  # noqa: E402
from app.db import Database  # noqa: E402
from app.job_manager import JobManager  # noqa: E402
from app.models import ConfigModel, FlowMode  # noqa: E402
from app.result_writer import ResultWriter  # noqa: E402
from app.sse import JobStream  # noqa: E402


async def _run() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert ":disabled=\"submitting\"" in html
    assert "if (this.submitting) return;" in html

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg_path = tmp_path / "config.yaml"
        model = ConfigModel(
            rust_bot_token="upi_test_token_xxxx",
            flow_mode=FlowMode.MANUAL,
        )
        cfg_path.write_text(
            "\n".join(
                f"{k}: {v}"
                for k, v in model.model_dump(mode="json").items()
                if not isinstance(v, list)
            )
            + "\nlogin_backoff_seconds: [2, 5, 15]\n",
            encoding="utf-8",
        )
        # ConfigStore validates via YAML — write properly
        import yaml

        cfg_path.write_text(
            yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )
        config = ConfigStore(cfg_path)
        db = Database(tmp_path / "t.sqlite3")
        await db.connect()
        sse = JobStream()
        blocklist = MagicMock()
        blocklist.filter_submit = AsyncMock(side_effect=lambda acc: (acc, []))
        jm = JobManager(
            config=config,
            db=db,
            sse=sse,
            session_cache=MagicMock(),
            rust_bot=MagicMock(),
            telegram=MagicMock(),
            plus=MagicMock(),
            results=ResultWriter(tmp_path / "results"),
            blocklist=blocklist,
        )
        await jm.start()

        line = "only1@example.com|Passw0rd!x|JBSWY3DPEHPK3PXP"
        r1 = await jm.submit(line, dispatch_mode="hold")
        assert len(r1["created"]) == 1, r1
        r2 = await jm.submit(line, dispatch_mode="hold")
        assert len(r2["created"]) == 0, r2
        assert r2["duplicate_count"] == 1, r2
        assert sum(1 for j in jm.jobs.values() if j.email == "only1@example.com") == 1

        # parallel double-submit still 1 job
        line2 = "race@example.com|Passw0rd!x|JBSWY3DPEHPK3PXP"
        a, b = await asyncio.gather(
            jm.submit(line2, dispatch_mode="hold"),
            jm.submit(line2, dispatch_mode="hold"),
        )
        created = len(a["created"]) + len(b["created"])
        assert created == 1, (a, b)
        assert sum(1 for j in jm.jobs.values() if j.email == "race@example.com") == 1

        await jm.stop()
        await db.close()

    print("OK no duplicate email jobs")


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
