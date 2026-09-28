"""Compare literal billing vectors with actual gateway/typed ledger commits.

Only catalog inputs and the crash seam are replaced. Normalization, pricing,
settle, finalize, outbox apply and fake Spanner transactions are real code.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.test_billing_snapshot import CASES, endpoint_from_candidate
from tests.test_settle_outbox_drain import (
    _client,
    _make_key,
    _outbox,
    _seed_credit,
    _typed_credit,
    _typed_key,
)
from trusted_router import billing_snapshot as billing
from trusted_router.catalog_data import Model
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.services import settle_outbox_apply
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome

SUPPORTED = [c for c in CASES if c["expected_exclusion"] is None]


@pytest.mark.parametrize("case", SUPPORTED, ids=lambda c: c["name"])
@pytest.mark.parametrize("mode", ["sync", "outbox_inline", "outbox_recovery"])
def test_committed_charge(case: dict[str, Any], mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoints = {c["endpoint_id"]: endpoint_from_candidate(c) for c in case["snapshot"]["candidates"]}
    primary = next(iter(endpoints.values()))
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(
            id=endpoint.model_id, name="Billing contract", provider=endpoint.provider,
            context_length=1_000_000, prepaid_available=True,
        ))
    monkeypatch.setattr(gateway, "endpoint_for_id", endpoints.get)
    monkeypatch.setattr(settle_outbox_apply, "endpoint_for_id", endpoints.get)
    store, db, _bt = make_fake_store()
    configure_store(store)
    try:
        ws = "ws-contract"
        _seed_credit(store, ws, total=10**15)
        key = _make_key(store, ws, limit=10**15)
        outcome, auth = store.authorize_gateway_typed(
            workspace_id=ws, key_hash=key.hash, estimate=case.get("reservation_estimate_micro", 1),
            has_credit_candidate=True, reservation_usage_type="Credits",
            model_id=primary.model_id, provider=primary.provider,
            requested_model_id=primary.model_id,
            candidate_model_ids=list({e.model_id for e in endpoints.values()}),
            region="us-central1", endpoint_id=primary.id, candidate_endpoint_ids=list(endpoints),
            idempotency_key=None, idempotency_fingerprint=None,
            expires_at="2099-01-01T00:00:00Z",
        )
        assert outcome == AuthorizeOutcome.ACCEPTED and auth is not None
        frozen = billing.parse_snapshot(json.dumps(case["snapshot"]))
        raw = billing.RawUsage(**case["raw_usage"])
        expected = case["expected_charge_micro"]
        assert billing.evaluate(frozen, case["selected_endpoint"], raw,
                                billing.Eligibility(**case["context"])).charge_micro == expected
        original = type(store).typed_finalize_gateway_authorization_result
        crashed = False

        def crash_once(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise RuntimeError("simulated loss after durable enqueue")
            return original(self, *args, **kwargs)

        if mode == "outbox_recovery":
            monkeypatch.setattr(type(store), "typed_finalize_gateway_authorization_result", crash_once)
        client = _client(Settings(environment="test", settle_outbox_enabled=mode != "sync"))
        body = dict(
            authorization_id=auth.id, selected_endpoint=case["selected_endpoint"],
            actual_input_tokens=raw.input_tokens, actual_output_tokens=raw.output_tokens,
            cache_read_input_tokens=raw.cache_read_tokens,
            cache_creation_input_tokens=raw.cache_creation_tokens, reasoning_tokens=raw.reasoning_tokens,
            route_type=case["context"].get("route_type", "chat.completions"),
            streamed=case["context"].get("streamed", False), request_id="billing-v1-differential",
        )
        response = client.post("/v1/internal/gateway/settle", json=body)
        assert response.status_code == 200, response.text
        if mode == "outbox_recovery":
            assert response.json()["data"]["disposition"] == "intent_durable"
            assert not db.reservations[auth.credit_reservation_id]["settled"]
            row = _outbox(store).get(auth.id, "settle")
            assert row is not None and row.actual_cost_micro == expected
            if case.get("catalog_action") == "remove":
                endpoints.clear()
            elif case.get("catalog_action") == "replace":
                for eid, endpoint in endpoints.copy().items():
                    endpoints[eid] = replace(endpoint, price_tiers=(),
                                             prompt_price_microdollars_per_million_tokens=900_000_000)
            db.settle_outbox[(auth.id, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
            drained = client.post("/v1/internal/gateway/settle-outbox/drain?limit=10")
            assert drained.status_code == 200, drained.text
            assert drained.json()["outcomes"] == {"settled_now": 1}
        else:
            assert response.json()["data"]["disposition"] == "finalized"
            assert response.json()["data"]["cost_microdollars"] == expected
        assert db.reservations[auth.credit_reservation_id]["actual_micro"] == expected
        assert _typed_credit(db, ws)["total_usage"] == expected
        assert _typed_key(db, key.hash)["usage"] == expected
        settled = store.get_gateway_authorization(auth.id)
        assert settled is not None and settled.settled and settled.finalized_generation_id
        generation = store.get_generation(settled.finalized_generation_id)
        assert generation is not None and generation.total_cost_microdollars == expected
        assert generation.tokens_prompt == case["expected_normalized_usage"]["total_prompt_tokens"]
        assert generation.tokens_completion == raw.output_tokens
        # Retries and outbox replays cannot add another ledger charge.
        replay = client.post("/v1/internal/gateway/settle", json=body)
        assert replay.status_code == 200
        assert _typed_credit(db, ws)["total_usage"] == expected
        assert billing.evaluate(frozen, case["selected_endpoint"], raw,
                                billing.Eligibility()).charge_micro == expected
    finally:
        configure_store(InMemoryStore())
