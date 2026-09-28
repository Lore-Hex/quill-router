"""Provider manifest rows pinned in tests, whatever the hosts list today.

The committed manifests are rebuilt hourly from provider feeds and move on: a
host delists a model and the refresh tombstones its row. A rule that needs a
host's route (a lifecycle cutover, a host's price schedule) runs on rows pinned
here instead: served in this process by serve_manifest_rows, or, for a catalog
built in a fresh process at a chosen instant, in a copy of today's manifests
from pinned_manifests.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from trusted_router import catalog_ingest, catalog_registry
from trusted_router.routes import catalog as catalog_routes


def _deepseek_row(model_id: str, display_name: str, **extra: Any) -> dict[str, Any]:
    upstream_id = model_id.removeprefix("deepseek/")
    return {
        "display_name": display_name,
        "title": upstream_id,
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": model_id,
        "upstream_id": upstream_id,
        "input_token_price_per_m": 150000,
        "output_token_price_per_m": 600000,
        "cached_input_token_price_per_m": 3000,
        "context_length": 1048576,
        "supported_features": ["function-calling", "json-mode", "reasoning-effort"],
        **extra,
    }


# DeepSeek's own V4 routes as its manifest lists them. Its time-of-day price
# schedule (provider_lifecycle) applies to DeepSeek's direct routes for these.
DEEPSEEK_V4_FLASH = _deepseek_row("deepseek/deepseek-v4-flash", "deepseek-v4-flash")
DEEPSEEK_V4_PRO = _deepseek_row("deepseek/deepseek-v4-pro", "DeepSeek-V4-Pro")
DEEPSEEK_FLASH = _deepseek_row(
    "deepseek/deepseek-flash", "DeepSeek V4.1 Flash (rolling)", input_modalities=["text", "image"],
)
DEEPSEEK_DIRECT_ROWS = (DEEPSEEK_V4_FLASH, DEEPSEEK_V4_PRO, DEEPSEEK_FLASH)

# The routes the DeepSeek V4 Pro 0813 release leaf is built from
# (catalog_registry._install_deepseek_v4_pro_release_routes), as their
# manifests listed them before Fireworks retired its route on 2026-09-25.
DEEPSEEK_V4_PRO_0813_ROUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("deepseek", DEEPSEEK_V4_PRO),
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


def serve_manifest_rows(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    provider: str,
    rows: Iterable[dict[str, Any]],
) -> None:
    """Serve `provider`'s routes for these manifest rows in this process's
    registry, built by the catalog's own manifest ingestion, in place of any
    route with the same id. A model the registry lacks is added with them."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"provider": provider, "models": [dict(row) for row in rows]}
    (directory / f"{provider}.json").write_text(json.dumps(manifest), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", directory)
        models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert endpoints, f"the catalog built no {provider} route from {manifest}"
    for model_id, model in models.items():
        if model_id not in catalog_registry.MODELS:
            monkeypatch.setitem(catalog_registry.MODELS, model_id, model)
    for endpoint_id, endpoint in endpoints.items():
        monkeypatch.setitem(catalog_registry.MODEL_ENDPOINTS, endpoint_id, endpoint)
    # The public catalog projection is cached per price period: this test
    # builds its own from these routes, and the process's cache is left as it was.
    projection = catalog_routes._public_catalog_payload
    monkeypatch.setattr(
        catalog_routes, "_public_catalog_payload", lru_cache(maxsize=1)(projection.__wrapped__)
    )
