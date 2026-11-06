"""Supply a historical retirement row only to isolated snapshot test processes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

_DIRECTORIES: list[TemporaryDirectory] = []


def install() -> None:
    assert "trusted_router.catalog_registry" not in sys.modules
    from trusted_router import catalog_ingest

    directory = TemporaryDirectory(prefix="tr-snapshot-retirement-")
    _DIRECTORIES.append(directory)
    target = Path(directory.name)
    for path in catalog_ingest._PROVIDER_MODELS_DIR.glob("*.json"):
        if path.name != "deepinfra.json":
            (target / path.name).symlink_to(path)
            continue
        raw = json.loads(path.read_text())
        native = "openai/gpt-oss-120b-Turbo"
        raw["models"] = [row for row in raw["models"] if row.get("upstream_id") != native]
        raw["models"].append({
            "id": "openai/gpt-oss-120b-turbo", "upstream_id": native,
            "model_type": "chat", "input_modalities": ["text"], "output_modalities": ["text"],
            "endpoints": ["chat/completions"], "status": 1, "routable": True,
            "context_length": 131_072, "max_output_tokens": 131_072,
            "input_token_price_per_m": 150_000, "output_token_price_per_m": 600_000,
            "retirement_at": "2026-10-20T00:00:00Z",
            "replacement_model_id": "openai/gpt-oss-120b",
        })
        raw["model_count"] = len(raw["models"])
        (target / path.name).write_text(json.dumps(raw))
    catalog_ingest._PROVIDER_MODELS_DIR = target


if __name__ == "__main__":
    install()
    from trusted_router.routing_snapshot_worker import main

    raise SystemExit(main())
