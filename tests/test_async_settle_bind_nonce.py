"""Fresh async authorize must carry every identity the enclave binds."""
from __future__ import annotations

import pytest

from tests.test_async_settle_admission import request
from tests.test_async_settle_probe_eligibility import authorize_body
from tests.test_async_settle_ticket import runtime, settings
from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from trusted_router.routes.internal import gateway
from trusted_router.synthetic.shadow_probe_contract import SHAPES, Shape


@pytest.mark.catalog_as_built
@pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
@pytest.mark.parametrize("negotiate", [False, True])
def test_fresh_async_response_echoes_bound_nonce(shape: Shape, negotiate: bool) -> None:
    _, _, key = _seed_typed_gateway_store()
    req = request(runtime())
    if not negotiate:
        req.scope["headers"] = []
    body = authorize_body(shape, key.hash, shape.body())
    data = gateway._authorize_gateway_sync(req, body, settings())["data"]
    assert not data["idempotent_replay"]
    if negotiate:
        assert data["async_eligible"] is True
        assert data.get("invocation_nonce") == body.invocation_nonce
    else:
        assert "invocation_nonce" not in data
