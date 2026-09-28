#!/usr/bin/env python3
"""Settings returns full API key; empty token on bulk keeps previous."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigStore  # noqa: E402
from app.models import ConfigModel  # noqa: E402


def main() -> int:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "includes('***')" not in html
    assert "Full API key is shown" in html

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.yaml"
        p.write_text(
            yaml.safe_dump(
                ConfigModel(rust_bot_token="upi_original_secret_key_abcdef").model_dump(
                    mode="json"
                ),
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        store = ConfigStore(p)
        d = store.masked_dict()
        assert d["rust_bot_token"] == "upi_original_secret_key_abcdef"
        # empty skip
        store.update_bulk({"rust_bot_token": ""})
        assert store.get().rust_bot_token == "upi_original_secret_key_abcdef"
        store.update_bulk({"rust_bot_token": "upi_new_secret_key_zzzzzzzz"})
        assert store.get().rust_bot_token == "upi_new_secret_key_zzzzzzzz"
        assert store.masked_dict()["rust_bot_token"] == "upi_new_secret_key_zzzzzzzz"

    print("OK settings show full API key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
