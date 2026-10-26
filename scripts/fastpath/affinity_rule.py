"""routing_state.rank's re-rank of a cold candidate order (G13, §10.1).

The authorize golden generator holds every warm answer Python gives to
this rule, and tests/test_fastpath_affinity_rule.py feeds it forbidden
answers. It has no side effects, so both import it; nothing imports the
generator.

Python's rank (src/trusted_router/routing_state.py) for a request naming
a cache_affinity_key: the session is one entry (the last remembered wins)
read within SESSION_TTL_SECONDS; a provider.order naming a provider keeps
the cold order; otherwise the candidates are sorted, stably, by their
model's first-seen rank (unless provider.sort.partition is none) and then
the remembered endpoint first. With fallbacks off the resolver has already
kept the primary model's endpoints alone, so the one candidate kept is the
remembered endpoint when it is of that model. A remembered endpoint the
request's other filters exclude is not a case the goldens hold.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

SESSION_TTL_SECONDS = 600

# The 200's members written from the first candidate
# (fastpath/internal/gwauth/response.go, responseData); wafer_zdr_required
# only under a Wafer first candidate.
FIRST_CANDIDATE_MEMBERS = frozenset({
    "model", "upstream_model", "endpoint_id", "provider", "provider_name", "inference_location",
    "wafer_zdr_required", "route_candidates",
})
ESTIMATE_MEMBERS = frozenset({"estimated_cost", "estimated_cost_microdollars"})
_MISSING = object()


def provider_orders(body: dict[str, Any]) -> bool:
    """Whether provider.order names a provider (routing._string_list: a
    list, or a comma-separated string; empty names none)."""
    order = (body.get("provider") or {}).get("order")
    if isinstance(order, str):
        return any(item.strip() for item in order.split(","))
    return bool(order)


def partition_by_model(body: dict[str, Any]) -> bool:
    """routing._sort_preferences: the partition is "model" unless
    provider.sort is an object whose partition is "none"."""
    sort = (body.get("provider") or {}).get("sort")
    return not (isinstance(sort, dict) and str(sort.get("partition") or "").strip().lower() == "none")


def fallbacks_off(body: dict[str, Any]) -> bool:
    provider = body.get("provider") or {}
    return body.get("allow_fallbacks") is False or provider.get("allow_fallbacks") is False


def rerank(cold: Sequence[str], sticky: str, by_model: bool, model_of: Callable[[str], str]) -> list[str]:
    """rank's order: a stable sort by the model's first-seen rank (unless
    not by_model) and then the remembered endpoint first."""
    ranks: dict[str, int] = {}
    for endpoint_id in cold:
        ranks.setdefault(model_of(endpoint_id), len(ranks))
    return sorted(cold, key=lambda e: (ranks[model_of(e)] if by_model else 0, e != sticky))


def expected_candidates(body: dict[str, Any], cold: Sequence[str], sessions: Sequence[tuple[str, float]],
                        model_of: Callable[[str], str]) -> list[str]:
    """Python's candidates with the sessions remembered under the request's
    session: the cold order, or rank's re-rank of it."""
    if not sessions or provider_orders(body):
        return list(cold)
    sticky, age = sessions[-1]
    if age >= SESSION_TTL_SECONDS:
        return list(cold)
    if fallbacks_off(body):
        return [sticky] if model_of(sticky) == model_of(cold[0]) else list(cold)
    if sticky not in cold:
        return list(cold)
    return rerank(cold, sticky, partition_by_model(body), model_of)


def affinity_class(name: str, body: dict[str, Any], cold_response: str, warm_response: str,
                   sessions: Sequence[tuple[str, float]], model_of: Callable[[str], str]) -> str:
    """The class Python's warm answer compares as against its cold one, or
    an AssertionError for an answer that is not rank's re-rank of the cold
    order with nothing else changed."""
    cold, warm = json.loads(cold_response)["data"], json.loads(warm_response)["data"]
    cold_ids = [c["endpoint_id"] for c in cold["route_candidates"]]
    warm_ids = [c["endpoint_id"] for c in warm["route_candidates"]]
    expected = expected_candidates(body, cold_ids, sessions, model_of)
    if warm_ids != expected:
        raise AssertionError(f"{name}: Python's candidates {warm_ids} are not rank's order {expected} over {cold_ids}")
    if warm_ids == cold_ids:
        if warm_response != cold_response:
            raise AssertionError(f"{name}: the same order, other bytes")
        return "affinity_cold"
    allowed = FIRST_CANDIDATE_MEMBERS | (ESTIMATE_MEMBERS if len(cold_ids) == 1 else frozenset())
    changed = {k for k in set(cold) | set(warm) if cold.get(k, _MISSING) != warm.get(k, _MISSING)}
    if not changed <= allowed:
        raise AssertionError(f"{name}: the session changed {sorted(changed - allowed)}")
    return "affinity_warm"
