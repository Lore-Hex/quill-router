"""Frozen async-video tariffs; content-free and independent of Stage D admission."""

from collections.abc import Iterable, Mapping
from typing import Any

from trusted_router.stage_d import (
    canonical_pricing_snapshot,
    endpoint_cost_microdollars_from_candidate,
    endpoint_pricing_document,
    parse_pricing_snapshot,
    pricing_candidate_for_endpoint,
)


def video_pricing_snapshot(endpoints: Iterable[Any], output_token_limit: int) -> str:
    document = endpoint_pricing_document(endpoints)
    document["output_token_limit"] = output_token_limit
    return canonical_pricing_snapshot(document)


def video_pricing_candidate(snapshot: str, endpoint_id: str) -> Mapping[str, Any]:
    return pricing_candidate_for_endpoint(parse_pricing_snapshot(snapshot), endpoint_id)


def video_token_billed(snapshot: str | None, endpoint: Any) -> bool:
    if snapshot is None:
        # Legacy fixed-price authorizations remain readable during a rollout.
        if endpoint.completion_price_microdollars_per_million_tokens > 0:
            raise ValueError("Token-billed video is missing its authorized tariff")
        return False
    candidate = video_pricing_candidate(snapshot, endpoint.id)
    return int(candidate["rates"]["output_micro_per_million"]) > 0


def video_cost_microdollars(
    snapshot: str, endpoint_id: str, *, output_tokens: int, quoted_microdollars: int,
) -> int:
    document = parse_pricing_snapshot(snapshot)
    candidate = pricing_candidate_for_endpoint(document, endpoint_id)
    token_billed = int(candidate["rates"]["output_micro_per_million"]) > 0
    if token_billed:
        if quoted_microdollars or not 0 < output_tokens <= document["output_token_limit"]:
            raise ValueError("Token-billed video requires bounded output usage without a fixed surcharge")
    elif output_tokens:
        raise ValueError("Fixed-price video must not include output token usage")
    return endpoint_cost_microdollars_from_candidate(candidate, 0, output_tokens)
