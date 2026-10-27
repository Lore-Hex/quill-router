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

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from trusted_router.billing_snapshot import Eligibility, exclusion
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle import ORDINARY_PARAMETERS

SESSION_TTL_SECONDS = 600

# The 200's members written from the first candidate
# (fastpath/internal/gwauth/response.go, responseData); wafer_zdr_required
# only under a Wafer first candidate.
FIRST_CANDIDATE_MEMBERS = frozenset({
    "model", "upstream_model", "endpoint_id", "provider", "provider_name", "inference_location",
    "wafer_zdr_required", "route_candidates",
})
ESTIMATE_MEMBERS = frozenset({"estimated_cost", "estimated_cost_microdollars"})
# An async-v1 answer's members (docs/design/fast-admission-e2-g15-async.md
# §2.2): built from the candidates kept, sorted by endpoint id, so a re-rank
# that keeps the same set leaves them as they were, and one that changes the
# set (fallbacks off, a remembered endpoint) can change them in kind (§4.4).
# Admission is off in the goldens, so an answer carries shape A or B only.
ASYNC_MEMBERS = frozenset({
    "async_eligible", "billing_snapshot", "billing_snapshot_hash", "settlement_ticket", "settlement_status_url",
})
SHAPE_A, SHAPE_B = ("async_eligible",), ("async_eligible", "billing_snapshot", "billing_snapshot_hash")
_MISSING = object()

# The billing snapshot build_snapshot freezes from these candidates (its
# model_dump, mode json), or None where it refuses one of them.
SnapshotOf = Callable[[Sequence[str]], "dict[str, Any] | None"]
# The facts async_settle.authorize_additions reads besides the body and the
# candidates, as the case's world and its authorization row hold them:
# replay, typed, federated, ticket_authority, admission_enabled, and the
# authorization's usage_type, settlement, app_markup_basis_points,
# custom_model_markup_basis_points, receipt_fee_basis_points, custom_model,
# user_model, tool_cost and native_batch.
AsyncFacts = dict[str, Any]


def canonical(snapshot: Any) -> bytes:
    """billing_snapshot.canonical_bytes over a snapshot's JSON form: sorted
    keys, compact, ASCII, so true, 1.0 and 1 are three different values."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def request_exclusion(body: dict[str, Any], facts: AsyncFacts) -> str | None:
    """Why authorize_additions answers shape A whatever the candidates, or
    None: its checks before any snapshot (replay, an undeclared member, the
    route type, input_modalities present, a requested parameter outside the
    ordinary set), billing_snapshot.exclusion over the Eligibility it builds,
    and the runtime's ticket authority. Admission must be off (shape C is
    not compared)."""
    if facts["admission_enabled"]:
        raise AssertionError("admission on: the async members carry a ticket")
    request = GatewayAuthorizeRequest.model_validate(body)
    if facts["replay"]:
        return "replay"
    if request.model_extra:
        return "undeclared_member"
    if request.route_type not in {"chat.completions", "responses"}:
        return "unsupported_route"
    if "input_modalities" in request.model_fields_set:
        return "input_modalities"
    if set(request.requested_parameters or ()) - ORDINARY_PARAMETERS:
        return "requested_parameters"
    reason = exclusion(Eligibility(
        typed=facts["typed"], usage_type=facts["usage_type"],
        authority="federated" if facts["federated"] else facts["settlement"],
        route_type=request.route_type, streamed=request.stream is True, service_tier=request.service_tier,
        app_markup=facts["app_markup_basis_points"], custom_markup=facts["custom_model_markup_basis_points"],
        receipt_fee=max(int(request.inference_receipt), facts["receipt_fee_basis_points"]),
        custom_model=facts["custom_model"], user_model=facts["user_model"], tool_cost=facts["tool_cost"],
        native_batch=facts["native_batch"], partner=not request.model.startswith(("openai/", "anthropic/")),
    ))
    if reason is not None:
        return reason
    return None if facts["ticket_authority"] else "ticket_authority"


def _async_shape(data: dict[str, Any]) -> tuple[str, ...]:
    return tuple(k for k in data if k in ASYNC_MEMBERS)


def check_async(name: str, label: str, data: dict[str, Any], ids: Sequence[str], eligible: bool,
                snapshot_of: SnapshotOf) -> None:
    """One answer's async members against its own candidates: shape B with
    exactly the snapshot they freeze (compared as canonical bytes) and the
    hash of the snapshot it carries, when the request is eligible and they
    freeze one; shape A otherwise; async_eligible false either way."""
    shape = _async_shape(data)
    if shape not in (SHAPE_A, SHAPE_B) or data["async_eligible"] is not False:
        raise AssertionError(f"{name}: the {label} answer's async members are not shape A or B")
    expected = snapshot_of(ids) if eligible else None
    if expected is None:
        if shape != SHAPE_A:
            raise AssertionError(f"{name}: the {label} answer is shape B, where Python answers shape A")
        return
    if shape != SHAPE_B:
        raise AssertionError(f"{name}: the {label} answer is shape A, where its candidates freeze a snapshot")
    supplied = canonical(data["billing_snapshot"])
    if supplied != canonical(expected):
        raise AssertionError(f"{name}: the {label} billing_snapshot is not its candidates' snapshot")
    if data["billing_snapshot_hash"] != hashlib.sha256(supplied).hexdigest():
        raise AssertionError(f"{name}: the {label} billing_snapshot_hash is not its snapshot's")


def check_warm_async(name: str, body: dict[str, Any], cold: dict[str, Any], warm: dict[str, Any],
                     cold_ids: Sequence[str], warm_ids: Sequence[str], snapshot_of: SnapshotOf | None,
                     facts: AsyncFacts | None) -> None:
    """A warm answer whose candidate set differs from the cold one: each
    answer's async members must be Python's for its own candidates, the
    request's eligibility decided from the request and the case's facts
    (request_exclusion), never from either answer."""
    if not _async_shape(cold) and not _async_shape(warm):
        return
    if snapshot_of is None or facts is None:
        raise AssertionError(f"{name}: async members, and no way to rebuild them")
    eligible = request_exclusion(body, facts) is None
    check_async(name, "cold", cold, cold_ids, eligible, snapshot_of)
    check_async(name, "warm", warm, warm_ids, eligible, snapshot_of)


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
                   sessions: Sequence[tuple[str, float]], model_of: Callable[[str], str],
                   snapshot_of: SnapshotOf | None = None, async_facts: AsyncFacts | None = None) -> str:
    """The class Python's warm answer compares as against its cold one, or
    an AssertionError for an answer that is not rank's re-rank of the cold
    order with nothing else changed. An async-v1 answer's members may change
    only with the candidate set, and only to the warm candidates' own
    (check_warm_async, which needs snapshot_of and async_facts)."""
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
    set_changed = set(warm_ids) != set(cold_ids)
    allowed = (FIRST_CANDIDATE_MEMBERS | (ESTIMATE_MEMBERS if len(cold_ids) == 1 else frozenset())
               | (ASYNC_MEMBERS if set_changed else frozenset()))
    changed = {k for k in set(cold) | set(warm) if cold.get(k, _MISSING) != warm.get(k, _MISSING)}
    if not changed <= allowed:
        raise AssertionError(f"{name}: the session changed {sorted(changed - allowed)}")
    if set_changed:
        check_warm_async(name, body, cold, warm, cold_ids, warm_ids, snapshot_of, async_facts)
    return "affinity_warm"
