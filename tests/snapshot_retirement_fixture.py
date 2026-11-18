"""Supply historical retirement rows only to isolated snapshot test processes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_DIRECTORIES: list[TemporaryDirectory] = []


def _chat_row(model_id: str, native: str, prices: tuple[int, int], retirement_at: str,
              replacement: str | None = None) -> dict[str, Any]:
    return {
        "id": model_id, "upstream_id": native,
        "model_type": "chat", "input_modalities": ["text"], "output_modalities": ["text"],
        "endpoints": ["chat/completions"], "status": 1, "routable": True,
        "context_length": 131_072, "max_output_tokens": 131_072,
        "input_token_price_per_m": prices[0], "output_token_price_per_m": prices[1],
        "retirement_at": retirement_at,
        **({"replacement_model_id": replacement} if replacement else {}),
    }


# One routable row per retirement the as-of tests cross, on the provider whose
# scheduled retirement in provider_lifecycle ends it at that instant. The
# scenario must not depend on the provider still listing the route: the hourly
# refresh tombstones what a provider delists (Together's Llama 3.3 Turbo on
# 2026-10-25, OpenAI's October 23 snapshots in the 2026-11-18 refresh).
RETIRING_ROWS: dict[str, tuple[str, dict[str, Any]]] = {
    "deepinfra-2026-10-20": ("deepinfra", _chat_row(
        "openai/gpt-oss-120b-turbo", "openai/gpt-oss-120b-Turbo", (150_000, 600_000),
        "2026-10-20T00:00:00Z", "openai/gpt-oss-120b",
    )),
    "together-2026-10-22": ("together", _chat_row(
        "meta-llama/llama-3.3-70b-instruct", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        (1_040_000, 1_040_000), "2026-10-22T00:00:00Z", "meta-models/muse-glimmer-30b",
    )),
    "openai-2026-10-23": ("openai", _chat_row(
        "openai/gpt-4.1-nano", "gpt-4.1-nano", (100_000, 400_000), "2026-10-23T00:00:00Z",
    )),
}


def install() -> None:
    assert "trusted_router.catalog_registry" not in sys.modules
    from trusted_router import catalog_ingest

    directory = TemporaryDirectory(prefix="tr-snapshot-retirement-")
    _DIRECTORIES.append(directory)
    target = Path(directory.name)
    supplied: dict[str, list[dict[str, Any]]] = {}
    for provider, row in RETIRING_ROWS.values():
        supplied.setdefault(provider, []).append(row)
    for path in catalog_ingest._PROVIDER_MODELS_DIR.glob("*.json"):
        rows = supplied.get(path.stem)
        if rows is None:
            (target / path.name).symlink_to(path)
            continue
        raw = json.loads(path.read_text())
        replaced = {key for row in rows for key in (row["id"], row["upstream_id"])}
        raw["models"] = [
            row for row in raw["models"]
            if row.get("id") not in replaced and row.get("upstream_id") not in replaced
        ]
        raw["models"].extend(rows)
        raw["model_count"] = len(raw["models"])
        (target / path.name).write_text(json.dumps(raw))
    catalog_ingest._PROVIDER_MODELS_DIR = target


if __name__ == "__main__":
    install()
    from trusted_router.routing_snapshot_worker import main

    raise SystemExit(main())
