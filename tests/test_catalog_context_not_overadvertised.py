"""Advertised context must be a window some route will actually honour.

Upstream endpoint metadata is not trustworthy: resellers publish context
windows larger than the publisher's own endpoint serves. Taking the max
across endpoints therefore advertised a window no route accepts -- callers
sized a request to it and got a provider-side rejection.
"""

from __future__ import annotations

import json
from pathlib import Path

from trusted_router.catalog_ingest import _INGEST_PATH, _ingested_models_and_endpoints


def _snapshot() -> list[dict]:
    raw = json.loads(Path(_INGEST_PATH).read_text())
    items = raw if isinstance(raw, list) else raw.get("data", raw.get("models", []))
    return [m for m in items if isinstance(m, dict)]


def test_advertised_context_never_exceeds_canonical_top_provider() -> None:
    models, _ = _ingested_models_and_endpoints()
    by_id = {m["id"]: m for m in _snapshot() if m.get("id")}

    over = []
    for model_id, model in models.items():
        raw = by_id.get(model_id)
        if not raw:
            continue
        top = raw.get("top_provider")
        canonical = int((top or {}).get("context_length") or 0)
        if canonical and model.context_length > canonical:
            over.append((model_id, model.context_length, canonical))

    assert not over, (
        "advertised context exceeds upstream's canonical top_provider window "
        f"for {len(over)} model(s): {over[:5]}"
    )


def test_glm_5_3_flash_advertises_the_publisher_window() -> None:
    """Regression: six reseller endpoints report 1310720; Z.AI's own says 1048576."""
    models, _ = _ingested_models_and_endpoints()
    model = models.get("z-ai/glm-5.3-flash")
    assert model is not None, "z-ai/glm-5.3-flash missing from the ingested catalog"
    assert model.context_length == 1_048_576, (
        f"expected the publisher's 1,048,576 window, got {model.context_length:,}"
    )


def test_every_ingested_model_keeps_a_usable_context() -> None:
    """Narrowing must never zero a model out."""
    models, _ = _ingested_models_and_endpoints()
    assert models, "no models ingested"
    zeroed = [mid for mid, m in models.items() if not m.context_length]
    assert not zeroed, f"models left with no context window: {zeroed[:5]}"
