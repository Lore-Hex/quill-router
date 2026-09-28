"""Today's provider manifests with some rows pinned, for a catalog built in a subprocess.

A test that builds the catalog in a fresh process at a chosen instant needs the
routes that instant's catalog had. The committed manifests are rebuilt hourly
from provider feeds and move on: a host delists a model and the refresh
tombstones its row. Such a test copies today's manifests, pins the rows it
needs, and points the subprocess at the copy.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from trusted_router import catalog_ingest

# The routes the DeepSeek V4 Pro 0813 release leaf is built from
# (catalog_registry._install_deepseek_v4_pro_release_routes), as their
# manifests listed them before Fireworks retired its route on 2026-09-25.
DEEPSEEK_V4_PRO_0813_ROUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("deepseek", {
        "display_name": "DeepSeek-V4-Pro",
        "title": "deepseek-v4-pro",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "deepseek/deepseek-v4-pro",
        "upstream_id": "deepseek-v4-pro",
        "input_token_price_per_m": 150000,
        "output_token_price_per_m": 600000,
        "cached_input_token_price_per_m": 3000,
        "context_length": 1048576,
        "supported_features": ["function-calling", "json-mode", "reasoning-effort"],
    }),
    ("baseten", {
        "display_name": "DeepSeek V4 Pro 0813",
        "title": "deepseek-ai/DeepSeek-V4-Pro-0813",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "deepseek/deepseek-v4-pro-0813",
        "upstream_id": "deepseek-ai/DeepSeek-V4-Pro-0813",
        "context_length": 1048576,
        "supported_features": ["tools", "json_mode", "structured_outputs", "reasoning"],
        "supported_sampling_parameters": ["temperature", "stop"],
        "input_token_price_per_m": 1320000,
        "output_token_price_per_m": 3960000,
        "cached_input_token_price_per_m": 132000,
        "max_output_tokens": 262144,
    }),
    ("fireworks", {
        "display_name": "DeepSeek V4 Pro 0813 on Fireworks",
        "title": "accounts/fireworks/models/deepseek-v4-pro-0813",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "deepseek/deepseek-v4-pro-0813",
        "upstream_id": "accounts/fireworks/models/deepseek-v4-pro-0813",
        "retirement_at": "2026-09-25T00:00:00Z",
        "context_length": 1048576,
        "created": 1786637155,
        "input_token_price_per_m": 1320000,
        "output_token_price_per_m": 3960000,
        "cached_input_token_price_per_m": 44000,
    }),
)

# Run in the subprocess before the catalog is built.
USE_PINNED_MANIFESTS = """
from pathlib import Path as _Path
from trusted_router import catalog_ingest as _catalog_ingest
_catalog_ingest._PROVIDER_MODELS_DIR = _Path(__import__("os").environ["TR_TEST_PROVIDER_MODELS_DIR"])
"""


def pinned_manifests(
    directory: Path, rows: Iterable[tuple[str, dict[str, Any]]]
) -> dict[str, str]:
    """Copy today's manifests into `directory`, put each (provider, row) in
    place of that provider's row with the same id, and return the environment
    for a subprocess that runs USE_PINNED_MANIFESTS first."""
    manifests = directory / "provider_models"
    shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, manifests)
    for provider, row in rows:
        path = manifests / f"{provider}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["models"] = [r for r in raw["models"] if r.get("id") != row["id"]] + [dict(row)]
        path.write_text(json.dumps(raw), encoding="utf-8")
    environ = dict(os.environ)
    environ["TR_TEST_PROVIDER_MODELS_DIR"] = str(manifests)
    return environ
