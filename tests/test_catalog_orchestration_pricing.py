"""Billed orchestrations must never publish a $0 price on /v1/models.

Panels and advisor orchestrations bill every inner call at the inner model's
own price. trustedrouter/synth, synth-code, fusion, fusion-code and
openpatcher-a1 once published ``{"prompt": "0", "completion": "0"}``, which
OpenRouter-compatible clients read as free. These tests pin the rule:

* a meta model with known billed members publishes the cheapest and dearest
  member rate (``prompt``/``completion`` and ``*_max``), the router convention;
* a primitive whose members the caller picks publishes OpenRouter's ``"-1"``;
* only the free pool publishes 0.
"""

from __future__ import annotations

from decimal import Decimal

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    FREE_MODEL_ID,
    FUSION_BILLED_MODEL_ORDER,
    FUSION_CODE_BILLED_MODEL_ORDER,
    FUSION_CODE_MODEL_ID,
    FUSION_MODEL_ID,
    META_MODEL_IDS,
    MODELS,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    SYNTH_CODE_MODEL_ID,
    SYNTH_MODEL_ID,
    VARIABLE_PRICE_META_MODEL_IDS,
    VARIABLE_PRICE_MICRODOLLARS,
    Model,
    billed_member_models,
    meta_candidate_models,
    model_to_openrouter_shape,
)
from trusted_router.dashboard import _model_view
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal

PROMPT = "prompt_price_microdollars_per_million_tokens"
COMPLETION = "completion_price_microdollars_per_million_tokens"
PUB_PROMPT = "published_prompt_price_microdollars_per_million_tokens"
PUB_COMPLETION = "published_completion_price_microdollars_per_million_tokens"
MICRO_FIELDS = (
    PROMPT,
    COMPLETION,
    PUB_PROMPT,
    PUB_COMPLETION,
    "prompt_price_max_microdollars_per_million_tokens",
    "completion_price_max_microdollars_per_million_tokens",
    "published_prompt_price_max_microdollars_per_million_tokens",
    "published_completion_price_max_microdollars_per_million_tokens",
)

REPORTED_ZERO_PRICE_MODELS = (
    SYNTH_MODEL_ID,
    SYNTH_CODE_MODEL_ID,
    FUSION_MODEL_ID,
    FUSION_CODE_MODEL_ID,
    OPEN_PATCHER_A1_MODEL_ID,
)


def _bounds(models: list[Model], attr: str) -> tuple[int, int]:
    values = [getattr(model, attr) for model in models if getattr(model, attr) > 0]
    assert values, f"no priced members for {attr}"
    return min(values), max(values)


def _assert_range_matches(model_id: str, members: list[Model]) -> None:
    shape = model_to_openrouter_shape(MODELS[model_id])
    pricing = shape["pricing"]
    tr = shape["trustedrouter"]
    prompt_lo, prompt_hi = _bounds(members, PROMPT)
    completion_lo, completion_hi = _bounds(members, COMPLETION)

    assert pricing["prompt"] == microdollars_per_million_tokens_to_token_decimal(prompt_lo)
    assert pricing["completion"] == microdollars_per_million_tokens_to_token_decimal(completion_lo)
    assert pricing.get("prompt_max", pricing["prompt"]) == (
        microdollars_per_million_tokens_to_token_decimal(prompt_hi)
    )
    assert pricing.get("completion_max", pricing["completion"]) == (
        microdollars_per_million_tokens_to_token_decimal(completion_hi)
    )
    # The microdollar block states the same numbers as the decimal strings.
    assert tr[PROMPT] == prompt_lo
    assert tr[COMPLETION] == completion_lo
    assert tr["prompt_price_max_microdollars_per_million_tokens"] == prompt_hi
    assert tr["completion_price_max_microdollars_per_million_tokens"] == completion_hi
    assert tr[PUB_PROMPT] == _bounds(members, PUB_PROMPT)[0]
    assert tr[PUB_COMPLETION] == _bounds(members, PUB_COMPLETION)[0]


def test_reported_orchestrations_no_longer_publish_zero() -> None:
    for model_id in REPORTED_ZERO_PRICE_MODELS:
        shape = model_to_openrouter_shape(MODELS[model_id])
        pricing = shape["pricing"]
        tr = shape["trustedrouter"]
        assert tr["route_kind"] in {"fusion_panel", "advisor_orchestration"}
        assert tr["prepaid_available"] is True
        for key in ("prompt", "completion"):
            assert pricing[key] == "-1" or Decimal(pricing[key]) > 0, (model_id, pricing)
        for field in MICRO_FIELDS:
            assert tr[field] != 0, (model_id, field)


def test_fusion_publishes_the_gateway_quality_panel_judge_and_final_models() -> None:
    for model_id, order in (
        (FUSION_MODEL_ID, FUSION_BILLED_MODEL_ORDER),
        (FUSION_CODE_MODEL_ID, FUSION_CODE_BILLED_MODEL_ORDER),
    ):
        members = [MODELS[member] for member in dict.fromkeys(order) if member in MODELS]
        assert [m.id for m in billed_member_models(model_id)] == [m.id for m in members]
        _assert_range_matches(model_id, members)
    # Fusion-code swaps only the general Kimi for the code-tuned Kimi.
    assert "moonshotai/kimi-k2.6" in FUSION_BILLED_MODEL_ORDER
    assert "moonshotai/kimi-k2.6" not in FUSION_CODE_BILLED_MODEL_ORDER
    # Pricing only: neither alias becomes a control-plane route.
    assert meta_candidate_models(FUSION_MODEL_ID) == []
    assert meta_candidate_models(FUSION_CODE_MODEL_ID) == []


def test_advisor_built_from_panels_prices_the_panels_members() -> None:
    expected = [
        *billed_member_models(OPEN_PATCHER_S1_MODEL_ID),
        *billed_member_models(PROMETHEUS_1_0_MODEL_ID),
    ]
    members = billed_member_models(OPEN_PATCHER_A1_MODEL_ID)
    assert [m.id for m in members] == [m.id for m in expected]
    assert all(m.id not in META_MODEL_IDS for m in members)
    _assert_range_matches(OPEN_PATCHER_A1_MODEL_ID, members)


def test_caller_composed_synth_publishes_variable_price_sentinel() -> None:
    assert VARIABLE_PRICE_META_MODEL_IDS == {SYNTH_MODEL_ID, SYNTH_CODE_MODEL_ID}
    for model_id in VARIABLE_PRICE_META_MODEL_IDS:
        shape = model_to_openrouter_shape(MODELS[model_id])
        assert shape["pricing"] == {"prompt": "-1", "completion": "-1"}
        for field in MICRO_FIELDS:
            assert shape["trustedrouter"][field] == VARIABLE_PRICE_MICRODOLLARS
        # The microdollar sentinel converts exactly to the decimal one.
        assert microdollars_per_million_tokens_to_token_decimal(VARIABLE_PRICE_MICRODOLLARS) == "-1"


def test_free_pool_stays_free() -> None:
    shape = model_to_openrouter_shape(MODELS[FREE_MODEL_ID])
    assert shape["trustedrouter"]["route_kind"] == "free_pool"
    assert shape["pricing"] == {"prompt": "0", "completion": "0"}
    for field in MICRO_FIELDS:
        assert shape["trustedrouter"][field] == 0


def test_no_billed_meta_chat_model_reads_as_free() -> None:
    for model_id in sorted(META_MODEL_IDS):
        model = MODELS.get(model_id)
        if model is None or model_id == FREE_MODEL_ID:
            continue
        shape = model_to_openrouter_shape(model)
        tr = shape["trustedrouter"]
        if tr.get("internal_only") or not tr.get("supports_chat", True):
            continue
        if not billed_member_models(model_id) and model_id not in VARIABLE_PRICE_META_MODEL_IDS:
            # Every member delisted: nothing can be billed or served.
            continue
        pricing = shape["pricing"]
        assert (pricing["prompt"], pricing["completion"]) != ("0", "0"), model_id


def test_routing_pools_keep_the_router_convention() -> None:
    # Router pools list concrete models only, so expanding nested
    # compositions cannot change any router row.
    for model_id in sorted(META_MODEL_IDS):
        if model_id not in MODELS:
            continue
        kind = model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]["route_kind"]
        if not kind.endswith("_pool"):
            continue
        assert [m.id for m in billed_member_models(model_id)] == [
            m.id for m in meta_candidate_models(model_id)
        ], model_id
    candidates = meta_candidate_models(AUTO_MODEL_ID)
    _assert_range_matches(AUTO_MODEL_ID, candidates)


def test_models_page_agrees_with_the_api() -> None:
    for model_id in (FUSION_MODEL_ID, FUSION_CODE_MODEL_ID, OPEN_PATCHER_A1_MODEL_ID):
        view = _model_view(MODELS[model_id], test_mode=True)
        members = billed_member_models(model_id)
        lo, hi = _bounds(members, PROMPT)
        assert str(view["prompt_price"]).startswith("$"), view["prompt_price"]
        assert str(view["completion_price"]).startswith("$"), view["completion_price"]
        if lo != hi:
            assert " to " in str(view["prompt_price"])
    for model_id in VARIABLE_PRICE_META_MODEL_IDS:
        view = _model_view(MODELS[model_id], test_mode=True)
        assert view["prompt_price"] == view["completion_price"] == "varies by panel"
