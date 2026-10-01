"""An explicit media route whose provider's manifest marks it dark is not served.

The async video routes are registered explicitly (catalog_registry._VIDEO_MODELS)
and skip the chat allowlist, which never lists video models. That exemption
used to come before the dark-row check too, so a video model its own provider
delisted, held or left without a price stayed on sale; the tombstone sweep found
Decart's Lucy routes and fal's MiniMax H3 Max served after their rows went dark.
"""

from __future__ import annotations

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint

MODEL = "fixture/video-model"
CREDITS = ModelEndpoint(id=f"{MODEL}@decart/prepaid", model_id=MODEL, provider="decart", usage_type="Credits")
BYOK = ModelEndpoint(id=f"{MODEL}@decart/byok", model_id=MODEL, provider="decart", usage_type="BYOK")


def _kept(monkeypatch: pytest.MonkeyPatch, dark: dict[str, frozenset[str]]) -> set[str]:
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: dark)
    kept = catalog_ingest._filter_unserved_provider_endpoints(
        {CREDITS.id: CREDITS, BYOK.id: BYOK}, explicit_model_ids=frozenset({MODEL})
    )
    return set(kept)


def test_a_dark_row_takes_an_explicit_media_route_off_sale(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = _kept(monkeypatch, {"decart": frozenset({MODEL})})

    assert CREDITS.id not in kept
    # A dark row closes our account's prepaid route, as for every other route;
    # the customer's own key is theirs to use.
    assert BYOK.id in kept


def test_an_explicit_media_route_still_skips_the_chat_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # Positive control: without a dark row the explicit route is kept, although
    # no chat manifest lists it.
    assert _kept(monkeypatch, {}) == {CREDITS.id, BYOK.id}
