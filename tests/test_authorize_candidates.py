"""The lifted authorize candidate pipeline holds no store, request or process state.

tests/test_authorize_candidates_differential.py proves the route answers as
main did; these tests pin the properties that make the pipeline portable: it
reads only what it is handed (plus the catalog), and the process-local health
re-rank is an explicit argument that callers may leave absent.
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trusted_router import authorize_candidates
from trusted_router.authorize_candidates import (
    HOLD_OUTPUT_TOKENS,
    CandidateRequest,
    endpoint_cost_microdollars,
    finish_candidates,
    hold_output_tokens,
    plan_route,
    resolve_candidates,
)
from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.custom_model_billing import custom_model_cost_microdollars

ROOT = Path(__file__).resolve().parents[1]
AT = dt.datetime(2026, 10, 21, 9, 30, tzinfo=dt.UTC)


def _request(model: str, route_type: str | None = "chat.completions", **body: object) -> CandidateRequest:
    routing_body = {"model": model, "route_type": route_type, **body}
    return CandidateRequest(
        route_type=route_type, route_model_id=model,
        routing_body={k: v for k, v in routing_body.items() if v is not None},
        body_region=None,
    )


def _outcome(request: CandidateRequest, rerank=None):  # type: ignore[no-untyped-def]
    settings = Settings(environment="test")
    plan = plan_route(request, settings, region="us-central1")
    resolved = resolve_candidates(plan, request, region="us-central1", at=AT)
    return plan, resolved, finish_candidates(
        plan, resolved, route_type=request.route_type, byok_providers=frozenset(),
        estimated_input_tokens=1_000, output_tokens=512, service_tier=None,
        additional_cost_reservation=0, video_resolution=None, at=AT, rerank=rerank,
    )


def _multi_route_chat_model() -> str:
    from trusted_router.catalog import endpoints_for_model

    return next(
        model.id for model in sorted(MODELS.values(), key=lambda m: m.id)
        if model.supports_chat and not model.id.startswith("trustedrouter/")
        and len([e for e in endpoints_for_model(model.id) if e.usage_type == "Credits"]) >= 3
    )


def test_the_rerank_is_applied_only_where_live_routing_is() -> None:
    model = _multi_route_chat_model()
    seen = []

    def reverse(candidates, preferences):  # type: ignore[no-untyped-def]
        seen.append(len(candidates))
        return list(reversed(candidates))

    _plan, resolved, absent = _outcome(_request(model))
    _plan, _resolved, reversed_ = _outcome(_request(model), rerank=reverse)
    credits = [e.id for _m, e in resolved.candidates if e.usage_type == "Credits"]
    assert [e.id for _m, e in absent.candidates] == credits
    assert [e.id for _m, e in reversed_.candidates] == credits[::-1] and seen == [len(credits)]
    embedding = next(m.id for m in sorted(MODELS.values(), key=lambda m: m.id)
                     if m.supports_embeddings and not m.supports_chat)
    seen.clear()
    _outcome(_request(embedding, "embeddings"), rerank=reverse)
    assert seen == []


def test_allow_fallbacks_false_keeps_the_first_candidate_after_the_rerank() -> None:
    model = _multi_route_chat_model()
    request = _request(model, provider={"allow_fallbacks": False})
    _plan, resolved, outcome = _outcome(request)
    _plan, _resolved, reranked = _outcome(request, rerank=lambda c, _p: list(reversed(c)))
    credits = [e.id for _m, e in resolved.candidates if e.usage_type == "Credits"]
    assert [e.id for _m, e in outcome.candidates] == credits[:1]
    assert [e.id for _m, e in reranked.candidates] == credits[-1:]


def test_byok_candidates_need_the_providers_credentials() -> None:
    model = next(
        model.id for model in sorted(MODELS.values(), key=lambda m: m.id)
        if model.supports_chat and not model.id.startswith("trustedrouter/")
        and any(e.usage_type == "BYOK" for _m, e in authorize_candidates.chat_route_endpoint_candidates(
            {"model": model.id}, Settings(environment="test")))
    )
    request = _request(model)
    settings = Settings(environment="test")
    plan = plan_route(request, settings, region="us-central1")
    resolved = resolve_candidates(plan, request, region="us-central1", at=AT)
    byok = sorted({e.provider for _m, e in resolved.candidates if e.usage_type == "BYOK"})
    arguments = {
        "route_type": "chat.completions", "estimated_input_tokens": 10, "output_tokens": 10,
        "service_tier": None, "additional_cost_reservation": 0, "video_resolution": None, "at": AT,
    }
    without = finish_candidates(plan, resolved, byok_providers=frozenset(), **arguments)
    with_keys = finish_candidates(plan, resolved, byok_providers=frozenset(byok), **arguments)
    assert all(e.usage_type == "Credits" for _m, e in without.candidates)
    assert {e.provider for _m, e in with_keys.candidates if e.usage_type == "BYOK"} == set(byok)


def test_the_pipeline_reads_no_store_request_clock_or_process_state() -> None:
    source = inspect.getsource(authorize_candidates)
    for forbidden in ("ROUTING_STATE", "STORE", "datetime.now", "uuid", "starlette", "logger"):
        assert forbidden not in source, forbidden
    script = (
        "import sys; import trusted_router.authorize_candidates; "
        "loaded = sorted(m for m in sys.modules if m == 'trusted_router.storage' or "
        "m.startswith(('trusted_router.routes', 'trusted_router.services', 'trusted_router.storage_gcp'))); "
        "assert not loaded, loaded"
    )
    result = subprocess.run(  # noqa: S603 - fixed Python script, no shell or caller input
        [sys.executable, "-c", script],
        env={**os.environ, "TR_ENVIRONMENT": "test", "PYTHONPATH": str(ROOT / "src")},
        capture_output=True, text=True, timeout=300, cwd=ROOT, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_a_refused_named_chain_carries_its_log_event() -> None:
    from trusted_router.catalog_data import NAMED_DECISION_MODEL_PROVIDERS

    model = next(iter(NAMED_DECISION_MODEL_PROVIDERS))
    request = _request(model, "decide")
    settings = Settings(environment="test")
    plan = plan_route(request, settings, region="us-central1")
    resolved = authorize_candidates.ResolvedCandidates([], plan.preferences)
    with pytest.raises(authorize_candidates.NamedChainRefusal) as refusal:
        finish_candidates(
            plan, resolved, route_type="decide", byok_providers=frozenset(),
            estimated_input_tokens=10, output_tokens=1, service_tier=None,
            additional_cost_reservation=0, video_resolution=None, at=AT,
        )
    assert refusal.value.event == "unavailable" and refusal.value.error.status_code == 503


def _candidates(model: str, route_type: str = "chat.completions"):  # type: ignore[no-untyped-def]
    request = _request(model, route_type=route_type)
    plan = plan_route(request, Settings(environment="test"), region="us-central1")
    return request, plan, resolve_candidates(plan, request, region="us-central1", at=AT)


def _figures(request, plan, resolved, output_tokens: int,  # type: ignore[no-untyped-def]
             user_model_prices: tuple[int, int] | None = None) -> tuple[int, int]:
    """The outcome's (estimate, reservation) for a 1,000-token prompt."""
    outcome = finish_candidates(
        plan, resolved, route_type=request.route_type, byok_providers=frozenset(),
        estimated_input_tokens=1_000, output_tokens=output_tokens, service_tier=None,
        additional_cost_reservation=0, video_resolution=None, at=AT,
        user_model_prices=user_model_prices,
    )
    return outcome.model_estimate, outcome.model_reservation


def _credits_cost(resolved, output_tokens: int) -> int:  # type: ignore[no-untyped-def]
    return max(
        endpoint_cost_microdollars(endpoint, 1_000, output_tokens, effective_at=AT, reserve_auto=True)
        for _model, endpoint in resolved.candidates if endpoint.usage_type == "Credits"
    )


def test_a_catalog_reservation_prices_at_most_hold_output_tokens_and_the_estimate_the_budget() -> None:
    request, plan, resolved = _candidates(_multi_route_chat_model())
    at_the_bound = _credits_cost(resolved, HOLD_OUTPUT_TOKENS)
    assert _figures(request, plan, resolved, HOLD_OUTPUT_TOKENS) == (at_the_bound, at_the_bound)
    for budget in (HOLD_OUTPUT_TOKENS + 1, 131_072):
        estimate, reservation = _figures(request, plan, resolved, budget)
        assert reservation == at_the_bound
        assert estimate == _credits_cost(resolved, budget) > at_the_bound
    below = _credits_cost(resolved, HOLD_OUTPUT_TOKENS - 1)
    assert _figures(request, plan, resolved, HOLD_OUTPUT_TOKENS - 1) == (below, below)
    assert below < at_the_bound


def test_a_user_provided_model_reserves_its_whole_budget() -> None:
    request, plan, resolved = _candidates(_multi_route_chat_model())
    prices = (1_000_000, 4_000_000)
    whole = custom_model_cost_microdollars(
        input_tokens=1_000, output_tokens=131_072, prompt_price=prices[0], completion_price=prices[1],
    )
    assert _figures(request, plan, resolved, 131_072, user_model_prices=prices) == (whole, whole)
    assert whole > _figures(request, plan, resolved, HOLD_OUTPUT_TOKENS, user_model_prices=prices)[1]


def test_a_token_billed_video_reserves_its_whole_budget() -> None:
    """A video's settle is held to the estimate, so its reservation is the
    estimate: 80,000 video tokens, not 16,384."""
    request, plan, resolved = _candidates("bytedance/seedance-2.5", route_type="videos")
    estimate, reservation = _figures(request, plan, resolved, 80_000)
    assert estimate == reservation == _credits_cost(resolved, 80_000) > _credits_cost(resolved, HOLD_OUTPUT_TOKENS)


def test_hold_output_tokens_is_the_bound_both_paths_share() -> None:
    assert hold_output_tokens(131_072) == HOLD_OUTPUT_TOKENS == 16_384
    assert hold_output_tokens(512) == 512
    evaluator = (ROOT / "fastpath" / "internal" / "routingsnapshot" / "evaluate.go").read_text()
    assert f"const HoldOutputTokens = {HOLD_OUTPUT_TOKENS:_}" in evaluator


def test_a_stream_reserves_its_whole_budget_for_now() -> None:
    """The Stage D heartbeat reader deployed before this change bounds a
    stream's running charge by the credit hold, so a stream's reservation
    stays its estimate until every plane runs the reader that caps at the
    estimate (finish_candidates, ``stream``)."""
    request, plan, resolved = _candidates(_multi_route_chat_model())
    outcome = finish_candidates(
        plan, resolved, route_type=request.route_type, byok_providers=frozenset(),
        estimated_input_tokens=1_000, output_tokens=131_072, service_tier=None,
        additional_cost_reservation=0, video_resolution=None, at=AT, stream=True,
    )
    assert outcome.model_reservation == outcome.model_estimate == _credits_cost(resolved, 131_072)
    assert outcome.model_reservation > _credits_cost(resolved, HOLD_OUTPUT_TOKENS)
