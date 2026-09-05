from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import AmbiguousGatewayRequestId, GatewayAuthorization
from trusted_router.storage_postgres import PostgresStore
from trusted_router.types import UsageType

TRACE = "rlog_00112233445566778899aabbccddeeff"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_trace_lookup_never_silently_selects_one_of_multiple_calls(
    backend: str, count: int,
) -> None:
    auths = [GatewayAuthorization(
        id=f"gwa-{i}", workspace_id="test", key_hash="test-key", model_id="test/model",
        provider="test", usage_type=UsageType.CREDITS, estimated_microdollars=1,
        credit_reservation_id=None, gateway_request_id=TRACE,
    ) for i in range(count)]
    store: Any
    if backend == "memory":
        store = InMemoryStore()
        store.api_keys.gateway_authorizations.update({auth.id: auth for auth in auths})
    else:
        def execute(sql: str, params: tuple[str, str]) -> Any:
            assert "ORDER BY id LIMIT 2" in sql
            assert params == ("gateway_authorization", TRACE)
            return SimpleNamespace(fetchall=lambda: [(asdict(auth),) for auth in auths])

        store = object.__new__(PostgresStore)
        store._run_transaction = lambda fn: fn(SimpleNamespace(execute=execute))
    if count == 2:
        with pytest.raises(AmbiguousGatewayRequestId):
            store.get_gateway_authorization_by_gateway_request_id(TRACE)
    else:
        result = store.get_gateway_authorization_by_gateway_request_id(TRACE)
        assert (result.id if result else None) == (auths[0].id if auths else None)
