"""A positive balance can be smaller than a request's upfront token budget."""
from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException

from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _request,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_positive_balance_rejection_explains_reservation_and_smaller_budget_works(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The fixture catalog prices 1 per million prompt and 5 per million
    # output tokens. The reservation prices at most HOLD_OUTPUT_TOKENS
    # (16,384) of the budget (authorize_candidates.hold_output_tokens):
    # 42,000 + 16,384 * 5 = 123,920, where the estimate, the whole
    # 1,000,000-token budget, is 5,042,000. The balance sits between the
    # 8,192-token reservation (82,960) and the capped one.
    _store, database, key = _seed_typed_gateway_store()
    balance = database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)]
    balance["total_credits"] = 100_000
    body = _lookup_body(key)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 1_000_000
    request = _request()
    request.state.request_id = "request-reservation-test"
    settings = Settings(environment="test")

    with caplog.at_level(logging.INFO), pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(request, body, settings)

    assert raised.value.status_code == 402
    error = raised.value.detail["error"]
    assert error["type"] == "insufficient_credits"
    assert "upfront reservation of $0.12392" in error["message"]
    assert "output-token limit" in error["message"]
    assert "not the final charge" in error["message"]
    assert balance["total_credits"] == 100_000
    assert balance["total_usage"] == balance["reserved"] == 0
    assert not database.typed.get("tr_reservation", {})
    assert not database.typed.get("tr_gateway_authorization", {})
    assert "billing.insufficient_credits" in caplog.text
    assert "reserved_microdollars=123920" in caplog.text
    assert "estimated_microdollars=5042000" in caplog.text
    assert "output_token_budget=1000000" in caplog.text
    assert f"workspace_id={key.workspace_id}" in caplog.text
    assert f"key_id={key.hash}" in caplog.text
    assert "request_id=request-reservation-test" in caplog.text

    body.max_output_tokens = 8192
    result = gateway._authorize_gateway_sync(request, body, settings)["data"]
    assert result["estimated_cost_microdollars"] == 82_960
    assert result["credit_reservation_id"]
    current = database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)]
    assert current["total_credits"] == 100_000
    assert current["total_usage"] == 0
    assert current["reserved"] == 82_960
