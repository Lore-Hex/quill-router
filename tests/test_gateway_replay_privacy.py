"""An idempotent authorize replay never restores a route the privacy floor excludes.

The replay advertises the routes it stored at first authorization, so billing
matches what was authorized. A route that has since lost the guarantee the
request asks for leaves the replay. When none is left, the replay fails closed:
settlement and refund accept only routes the authorization holds, so it cannot
hand out freshly filtered ones.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_privacy import endpoint_zero_data_retention
from trusted_router.routes.internal import gateway


def _routes() -> tuple[object, object]:
    """One ZDR route and one route without ZDR, both priced and catalogued."""
    credits = [
        endpoint
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.usage_type == "Credits" and endpoint.model_id in MODELS
    ]
    zdr = next(endpoint for endpoint in credits if endpoint_zero_data_retention(endpoint) is True)
    plain = next(endpoint for endpoint in credits if endpoint_zero_data_retention(endpoint) is not True)
    return zdr, plain


def _authorization(*endpoint_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_provided_model_id=None,
        candidate_endpoint_ids=list(endpoint_ids),
        endpoint_id=endpoint_ids[0],
    )


def test_replay_drops_a_stored_route_the_privacy_floor_now_excludes() -> None:
    zdr, plain = _routes()
    fresh = [(MODELS[zdr.model_id], zdr)]
    authorization = _authorization(plain.id, zdr.id)
    floor = frozenset({PRIVACY_TIER_ZERO_RETENTION})

    # Positive control: without a floor the stored routes replay in order.
    replayed = gateway._authorization_endpoint_candidates(authorization, fresh)
    assert [endpoint.id for _model, endpoint in replayed] == [plain.id, zdr.id]

    replayed = gateway._authorization_endpoint_candidates(
        authorization, fresh, privacy_requirements=floor
    )
    assert [endpoint.id for _model, endpoint in replayed] == [zdr.id]


def test_replay_fails_closed_when_no_stored_route_qualifies() -> None:
    zdr, plain = _routes()
    fresh = [(MODELS[zdr.model_id], zdr)]
    with pytest.raises(HTTPException) as exc:
        gateway._authorization_endpoint_candidates(
            _authorization(plain.id),
            fresh,
            privacy_requirements=frozenset({PRIVACY_TIER_ZERO_RETENTION}),
        )
    assert exc.value.status_code == 409
    # Positive control: a stored route that left the catalog still falls back.
    assert gateway._authorization_endpoint_candidates(
        _authorization("gone/model@nowhere/prepaid"),
        fresh,
        privacy_requirements=frozenset({PRIVACY_TIER_ZERO_RETENTION}),
    ) == fresh
