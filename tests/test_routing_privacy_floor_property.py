"""Property tests for the routing privacy floor.

The floor is the flagship privacy invariant: a customer asking for zdr or e2e
routing must not have their prompt dispatched to a provider below that tier.
The law:

    for every request body B,
        resolved floor >= max(explicit body floor,
                              max over every requested meta-model of its
                              enforced tier)

and, as a corollary that is easier to falsify:

    the resolved floor does not depend on the ORDER of `model` + `models[]`.

Both were false. `_requested_model_ids` accumulates into one flat `overrides`
dict shared by every id, and stored the enforced tier as a plain assignment:

    overrides["min_privacy"] = "e2ee" if enforced_privacy_tier >= 3 else "zdr"

so a later, weaker meta-model silently overwrote a stricter earlier one. The
merge with the body floor at the end *is* a max, which is what made this hard
to see by reading: only the last meta-model's tier ever reached that max.

    {"model": "trustedrouter/e2e", "models": ["trustedrouter/zdr"]}  -> rank 2
    {"model": "trustedrouter/zdr", "models": ["trustedrouter/e2e"]}  -> rank 3

Same request, same two models, floor decided by list order — and rank 2 admits
providers that rank 3 exists to exclude. `models[]` is a documented fallback
mechanism and production gateway authorization resolves candidates through this
path, so this was reachable from an ordinary API call.

These tests quantify over orderings and combinations rather than pinning the
two examples, because the defect is a *composition* bug: every individual
model resolved correctly on its own.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.catalog_data import (
    E2E_MODEL_ID,
    PRIVACY_TIER_ALIASES,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    ZDR_MODEL_ID,
)
from trusted_router.config import Settings
from trusted_router.routing import _routing_for_body

SETTINGS = Settings()

# Meta-models that enforce a floor, plus ordinary models that enforce none —
# the mix matters, since a non-enforcing model must not disturb the floor.
FLOOR_MODELS = sorted(ROUTING_MODEL_MIN_PRIVACY_TIERS)
PLAIN_MODELS = ["openai/gpt-4o-mini", "anthropic/claude-3-5-haiku"]
ALL_MODELS = FLOOR_MODELS + PLAIN_MODELS


def _floor_of(model_id: str) -> int:
    return ROUTING_MODEL_MIN_PRIVACY_TIERS.get(model_id, 0)


def _expected_floor(model_ids: list[str], explicit: str | None) -> int:
    """The floor the request is entitled to: the strictest thing it asked for."""
    enforced = max((_floor_of(m) for m in model_ids), default=0)
    body_floor = PRIVACY_TIER_ALIASES[explicit] if explicit else 0
    return max(enforced, body_floor)


def _resolve(model_ids: list[str], explicit: str | None = None) -> int:
    body: dict[str, Any] = {"model": model_ids[0]}
    if len(model_ids) > 1:
        body["models"] = list(model_ids[1:])
    if explicit:
        body["provider"] = {"min_privacy": explicit}
    _, prefs = _routing_for_body(body, SETTINGS)
    return int(prefs.min_privacy_rank)


# ------------------------------------------------------------- the law ---


@given(
    model_ids=st.lists(st.sampled_from(ALL_MODELS), min_size=1, max_size=5),
    explicit=st.one_of(st.none(), st.sampled_from(["standard", "no_store", "zdr", "e2ee"])),
)
@settings(max_examples=500)
def test_resolved_floor_dominates_everything_requested(
    model_ids: list[str], explicit: str | None
) -> None:
    """The floor is at least the strictest tier the request asked for.

    Stated as domination rather than equality: filters downstream may only
    narrow the candidate set, so a floor *above* what was asked is safe. A
    floor below it is the breach.
    """
    resolved = _resolve(model_ids, explicit)
    expected = _expected_floor(model_ids, explicit)
    assert resolved >= expected, (
        f"floor {resolved} is below the strictest requested tier {expected} "
        f"for models={model_ids!r} explicit={explicit!r}"
    )


@given(model_ids=st.lists(st.sampled_from(ALL_MODELS), min_size=2, max_size=4))
@settings(max_examples=300)
def test_floor_is_independent_of_model_order(model_ids: list[str]) -> None:
    """Permuting the requested models must not move the floor.

    This is the corollary that actually fails loudly on the old code, and the
    one worth keeping: a request's privacy guarantee cannot depend on which
    fallback happens to be listed last.
    """
    floors = {tuple(p): _resolve(list(p)) for p in itertools.permutations(model_ids)}
    assert len(set(floors.values())) == 1, f"floor depends on ordering: {floors!r}"


@given(
    model_ids=st.lists(st.sampled_from(ALL_MODELS), min_size=1, max_size=4),
    extra=st.sampled_from(ALL_MODELS),
)
@settings(max_examples=300)
def test_adding_a_model_never_lowers_the_floor(model_ids: list[str], extra: str) -> None:
    """Monotonicity: a fallback entry can only tighten the floor, never relax it.

    This is the property a caller reasons with when adding a fallback — that
    doing so cannot quietly downgrade the privacy of the whole request.
    """
    before = _resolve(model_ids)
    after = _resolve([*model_ids, extra])
    assert after >= before, (
        f"adding {extra!r} lowered the floor from {before} to {after} "
        f"(models={model_ids!r})"
    )


# ------------------------------------------------ concrete regressions ---


def test_the_two_orderings_that_used_to_disagree() -> None:
    """The exact reproduction, pinned alongside the general property."""
    e2e_first = _resolve([E2E_MODEL_ID, ZDR_MODEL_ID])
    zdr_first = _resolve([ZDR_MODEL_ID, E2E_MODEL_ID])

    assert e2e_first == zdr_first
    assert e2e_first == PRIVACY_TIER_ALIASES["e2ee"]


def test_a_plain_model_does_not_dilute_a_meta_model_floor() -> None:
    """A non-enforcing fallback must leave the floor where the meta-model put it."""
    assert _resolve([E2E_MODEL_ID, "openai/gpt-4o-mini"]) == PRIVACY_TIER_ALIASES["e2ee"]
    assert _resolve(["openai/gpt-4o-mini", E2E_MODEL_ID]) == PRIVACY_TIER_ALIASES["e2ee"]


def test_explicit_body_floor_still_composes_with_meta_models() -> None:
    """The body floor and the enforced floor merge by max, in both directions."""
    assert _resolve([ZDR_MODEL_ID], explicit="e2ee") == PRIVACY_TIER_ALIASES["e2ee"]
    assert _resolve([E2E_MODEL_ID], explicit="standard") == PRIVACY_TIER_ALIASES["e2ee"]


@pytest.mark.parametrize("model_id", FLOOR_MODELS)
def test_each_meta_model_alone_still_resolves_to_its_own_tier(model_id: str) -> None:
    """The fix must not disturb the single-model case it was not aimed at."""
    assert _resolve([model_id]) == ROUTING_MODEL_MIN_PRIVACY_TIERS[model_id]
