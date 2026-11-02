from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.fakes.spanner import make_fake_store
from trusted_router.money import dollars_to_microdollars
from trusted_router.routes.console.api_keys import _parse_limit
from trusted_router.schemas import (
    CreateKeyRequest,
    EarningsTransferRequest,
    PatchKeyRequest,
    UserModelCreateRequest,
    UserModelPatchRequest,
)
from trusted_router.storage import STORE

MAX_INT64 = 2**63 - 1
MAX_DOLLARS = "9223372036854.775807"
BAD_BUDGETS = [
    "NaN", "sNaN", "Infinity", "-Infinity", "1e1000000",
    "9223372036854.775808", "-0.0000001",
]
FIELDS = ["limit", "limit_daily", "limit_weekly", "limit_monthly"]


@pytest.mark.parametrize("schema", [CreateKeyRequest, PatchKeyRequest])
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", BAD_BUDGETS)
def test_key_budget_rejects_nonfinite_and_unstorable_values(schema, field, value):
    with pytest.raises(ValidationError):
        schema.model_validate({field: value})


@pytest.mark.parametrize("schema", [CreateKeyRequest, PatchKeyRequest])
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value,expected", [(None, None), ("0", 0), ("0.000001", 1),
                                           (MAX_DOLLARS, MAX_INT64)])
def test_key_budget_preserves_valid_limits(schema, field, value, expected):
    result = getattr(schema.model_validate({field: value}), field)
    assert (None if result is None else dollars_to_microdollars(result)) == expected
    if result is not None:
        assert result == Decimal(value)


@pytest.mark.parametrize("value", BAD_BUDGETS)
def test_console_budget_uses_the_same_storage_boundary(value):
    with pytest.raises(ValueError):
        _parse_limit(value)


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", BAD_BUDGETS)
def test_invalid_key_budget_returns_400_without_mutation(client, user_headers, method, field, value):
    created = client.post("/v1/keys", headers=user_headers, json={"limit": "1"})
    assert created.status_code == 201
    key_hash = created.json()["data"]["hash"]
    key = STORE.get_key_by_hash(key_hash)
    before = STORE.list_keys(key.workspace_id)
    response = getattr(client, method)(
        "/v1/keys" + (f"/{key_hash}" if method == "patch" else ""),
        headers=user_headers, json={field: value},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_request"
    assert STORE.list_keys(key.workspace_id) == before
    assert STORE.get_key_by_hash(key_hash).limit_microdollars == 1_000_000


@pytest.mark.parametrize("offset", [0, 1, 999999999999999999, MAX_INT64 - 1, MAX_INT64, MAX_INT64 + 1])
@pytest.mark.parametrize("limit", [None, 1001])
def test_spanner_key_page_limit_plus_offset_never_overflows(offset, limit):
    store, db = make_fake_store()
    assert store.list_api_keys_with_usage("ws-owner", offset=offset, limit=limit) == []
    for params in db.snapshot_sql_params:
        assert 0 <= params["offset"] <= MAX_INT64
        assert 0 <= params["limit"] <= MAX_INT64 - params["offset"]


@pytest.mark.parametrize("query", ["offset=1", "offset=999999999999999999", f"offset={MAX_INT64}"])
def test_key_page_large_offsets_are_empty_not_errors(client, user_headers, query):
    response = client.get(f"/v1/keys?{query}", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.parametrize("value", [2**63, "1e1000000", "NaN", "Infinity", "-Infinity"])
def test_integer_money_schemas_reject_unstorable_values(value):
    with pytest.raises(ValidationError):
        EarningsTransferRequest(workspace_id="ws-test", amount_microdollars=value)
    for schema in (UserModelCreateRequest, UserModelPatchRequest):
        data = ({"name": "test", "slug": "test", "kind": "machine", "display_name": "test",
                 "endpoint_url": "https://example.com", "upstream_model_id": "test"}
                if schema is UserModelCreateRequest else {})
        for field in ("prompt_price_microdollars_per_million_tokens",
                      "completion_price_microdollars_per_million_tokens"):
            with pytest.raises(ValidationError):
                schema.model_validate({**data, field: value})
