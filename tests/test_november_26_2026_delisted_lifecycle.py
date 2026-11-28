from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.test_decide_models import (
    _assert_no_financial_side_effects,
    _authorize,
    _named_candidates,
    _serve_on_fixture_routes,
)
from trusted_router import provider_lifecycle
from trusted_router.catalog_data import (
    LEV_1_0_MODEL_ID,
    NAMED_DECISION_MODEL_PROVIDERS,
    TREV_1_0_MODEL_ID,
)

# Delisted without a notice. SambaNova's model API stopped listing
# Meta-Llama-3.3-70B-Instruct on 2026-11-17 and Fireworks' stopped listing
# gpt-oss-120b on 2026-11-21; both routes retire on 2026-11-26 05:00 UTC,
# between two refreshes that still found them missing. Every instant below is
# explicit, so these rules hold whatever the hosts list on the day they run.
_CUTOFF = datetime(2026, 11, 26, 5, 0, tzinfo=UTC)
_LLAMA = ("meta-llama/llama-3.3-70b-instruct", "Meta-Llama-3.3-70B-Instruct")
_GPT_OSS = ("openai/gpt-oss-120b", "accounts/fireworks/models/gpt-oss-120b")
_RETIRED = (("sambanova", *_LLAMA), ("fireworks", *_GPT_OSS))
# Routes neither delisting touches: the same models on the other hosts of the
# two chains, and each provider's other routes.
_RETAINED = (
    ("parasail", _LLAMA[0], "meta-llama/Llama-3.3-70B-Instruct"),
    ("cerebras", _GPT_OSS[0], "gpt-oss-120b"),
    ("sambanova", _GPT_OSS[0], "gpt-oss-120b"),
    ("sambanova", "google/gemma-4-31b-it", "gemma-4-31B-it"),
    ("fireworks", "moonshotai/kimi-k3", "accounts/fireworks/models/kimi-k3"),
    ("fireworks", "z-ai/glm-5.3-fast", "accounts/fireworks/routers/glm-5p3-fast"),
)


def _entries() -> list[provider_lifecycle._Retirement]:
    return [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.effective_at == _CUTOFF
    ]


def _at(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: instant)


def test_cutoff_is_five_utc_on_november_26() -> None:
    assert provider_lifecycle.NOVEMBER_26_DELISTED_RETIREMENT_AT == _CUTOFF


def test_entries_retire_exactly_the_two_delisted_routes_without_a_replacement() -> None:
    # Neither provider named a successor, so customers are quoted none.
    assert {
        (entry.provider, entry.model_ids, entry.upstream_ids, entry.replacement_model_ids)
        for entry in _entries()
    } == {
        (provider, frozenset({model_id}), frozenset({native_id}), ())
        for provider, model_id, native_id in _RETIRED
    }


@pytest.mark.parametrize(("provider", "model_id", "native_id"), _RETIRED)
def test_delisted_route_serves_before_the_cutoff_and_retires_at_it(
    provider: str, model_id: str, native_id: str,
) -> None:
    before = _CUTOFF - timedelta(microseconds=1)
    retired = provider_lifecycle.provider_model_retired
    assert not retired(provider, model_id, native_id, at=before)
    assert retired(provider, model_id, native_id, at=_CUTOFF)
    # Either spelling alone is enough: a relisted feed row cannot restore it.
    assert retired(provider, model_id, at=_CUTOFF)
    assert retired(provider, "fixture/other-spelling", native_id, at=_CUTOFF)


@pytest.mark.parametrize(("provider", "model_id", "native_id"), _RETAINED)
def test_other_hosts_and_other_routes_are_not_retired(
    provider: str, model_id: str, native_id: str,
) -> None:
    # Just past the cutoff, before any later notice (Together's gpt-oss-120b
    # on 2026-12-09) changes the picture.
    assert not provider_lifecycle.provider_model_retired(
        provider, model_id, native_id, at=_CUTOFF + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_lev_no_longer_rides_on_sambanova_at_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SambaNova headed lev-1.0's chain when its route retired, which left
    Parasail to serve the name alone. On 2026-11-28 lev moved to W&B, Parasail
    and IO Intelligence, so SambaNova is outside the chain on either side of
    the cutoff and the retirement changes nothing for the name."""
    chain = NAMED_DECISION_MODEL_PROVIDERS[LEV_1_0_MODEL_ID]
    assert chain == ("wandb", "parasail", "io-net")
    backing = _serve_on_fixture_routes(monkeypatch, LEV_1_0_MODEL_ID, ("sambanova", *chain))
    assert backing == _LLAMA[0]

    for instant in (
        _CUTOFF - timedelta(microseconds=1), _CUTOFF, _CUTOFF + timedelta(days=30),
    ):
        _at(monkeypatch, instant)
        assert await _named_candidates(LEV_1_0_MODEL_ID, None) == list(chain)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "served_before"), [(LEV_1_0_MODEL_ID, False), (_LLAMA[0], True)],
)
async def test_a_request_pinned_to_sambanova_is_refused_from_the_cutoff(
    model_id: str, served_before: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat model was served on SambaNova until the cutoff. The name is
    refused there on both sides of it: SambaNova left lev's chain."""
    _serve_on_fixture_routes(monkeypatch, LEV_1_0_MODEL_ID, ("sambanova", "parasail"))
    request: dict[str, Any] = {
        "model": model_id, "route_type": "decide",
        "estimated_input_tokens": 480, "max_output_tokens": 700,
        "provider": {"only": ["sambanova"], "allow_fallbacks": False},
    }
    _at(monkeypatch, _CUTOFF - timedelta(microseconds=1))
    response = await _authorize(request)
    if served_before:
        assert response.status_code == 200, response.text
        assert response.json().get("data", response.json())["provider"] == "sambanova"
    else:
        assert response.status_code >= 400, response.text
        _assert_no_financial_side_effects()

    _at(monkeypatch, _CUTOFF)
    response = await _authorize(request)
    assert response.status_code >= 400, response.text
    _assert_no_financial_side_effects()


@pytest.mark.asyncio
async def test_trev_skips_fireworks_and_keeps_its_order_at_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fireworks is third on trev-1.0's chain. The hosts around it keep their
    order, and the chain itself is unchanged."""
    chain = NAMED_DECISION_MODEL_PROVIDERS[TREV_1_0_MODEL_ID]
    assert chain == ("cerebras", "sambanova", "fireworks", "together")
    backing = _serve_on_fixture_routes(monkeypatch, TREV_1_0_MODEL_ID)
    assert backing == _GPT_OSS[0]

    _at(monkeypatch, _CUTOFF - timedelta(microseconds=1))
    assert await _named_candidates(TREV_1_0_MODEL_ID, None) == list(chain)
    _at(monkeypatch, _CUTOFF)
    assert await _named_candidates(TREV_1_0_MODEL_ID, None) == ["cerebras", "sambanova", "together"]
