"""The JSONL history both delivery verifiers append to.

Kept apart from either verifier so each one imports only its own plane's
driver: the Postgres verifier runs on the AWS ClickHouse node, whose slim
virtualenv has no Google client libraries, and importing this function from
verify_spanner_delivery pulled those in transitively and failed there.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

RETENTION = dt.timedelta(days=16)


def write_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = dt.datetime.now(dt.UTC) - RETENTION
    history: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                checked = dt.datetime.fromisoformat(
                    str(row["checked_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if checked >= cutoff:
                history.append(row)
    history.append(result)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in history))
    os.replace(temporary, path)
