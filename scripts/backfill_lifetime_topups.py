#!/usr/bin/env python3
"""Backfill lifetime paid-top-up totals from payment-provider history.

The default is a read-only dry run. Applying requires an exact expected total
from a reviewed dry run. This script changes only ``tr_user_lifetime_topup``
through ``Store.add_lifetime_topup``; it never grants workspace credits.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import (
    MICRODOLLARS_PER_CENT,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
)
from trusted_router.routes.internal.webhook import (
    _auto_refill_credit_amount_microdollars,
    _checkout_credit_amount_microdollars,
)
from trusted_router.services.adyen_billing import _parse_checkout_reference
from trusted_router.services.paypal_billing import (
    _paypal_capture_payload,
    fetch_paypal_capture,
)
from trusted_router.services.x402_billing import (
    _metadata_requested_microdollars,
    _settled_amount_microdollars,
)
from trusted_router.storage import create_store
from trusted_router.storage_models import Workspace

DEFAULT_CUTOVER = "2026-08-16T05:20:00Z"
_HEURISTIC_WINDOW_SECONDS = 600


@dataclass
class Candidate:
    source: str
    provider_ref: str
    workspace_id: str | None
    user_id: str | None
    user_email: str | None
    amount_microdollars: int
    gross_cents: int | None
    provider_created_at: str | None
    payment_method: str | None
    decision: str = "include"
    reason: str | None = None
    backfill_event_id: str | None = None
    status: str = "skipped"
    partial_refund: bool = False
    # Set when the two cut-over witnesses (creation time vs. the Phase-2
    # initiating_user_id stamp) disagree; the stamp decided, the row is
    # flagged for the operator.
    cutover_witness_disagreement: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provider_ref": self.provider_ref,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "amount_microdollars": self.amount_microdollars,
            "gross_cents": self.gross_cents,
            "provider_created_at": self.provider_created_at,
            "payment_method": self.payment_method,
            "decision": self.decision,
            "reason": self.reason,
            "backfill_event_id": self.backfill_event_id,
            "status": self.status,
            "partial_refund": self.partial_refund,
            "cutover_witness_disagreement": self.cutover_witness_disagreement,
        }


@dataclass(frozen=True)
class EntityClaim:
    entity_id: str
    body: dict[str, Any]

    @property
    def created_at(self) -> str | None:
        raw = self.body.get("created_at")
        return raw if isinstance(raw, str) and raw else None


@dataclass(frozen=True)
class RefundState:
    amount_cents: int
    amount_refunded_cents: int
    disputed: bool


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    recursive = getattr(value, "_to_dict_recursive", None)
    if callable(recursive):
        converted = recursive()
        if isinstance(converted, dict):
            return converted
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"provider returned unsupported object {type(value).__name__}")


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return _mapping(value)


def _parse_timestamp(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {raw}")
    return parsed.astimezone(UTC)


def _provider_timestamp(raw: Any) -> tuple[datetime, str]:
    if isinstance(raw, bool):
        raise ValueError("provider created timestamp is invalid")
    if isinstance(raw, int | float):
        parsed = datetime.fromtimestamp(raw, tz=UTC)
    elif isinstance(raw, str) and raw:
        parsed = _parse_timestamp(raw)
    else:
        raise ValueError("provider created timestamp is missing")
    return parsed, parsed.isoformat().replace("+00:00", "Z")


def _scan_entity_claims(store: Any, prefix: str) -> list[EntityClaim]:
    sql = (
        "SELECT id, body FROM tr_entities WHERE kind=@kind "
        "AND STARTS_WITH(id, @prefix) ORDER BY id"
    )
    with store._database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                sql,
                params={"kind": "stripe_event", "prefix": prefix},
                param_types={
                    "kind": store._param_types.STRING,
                    "prefix": store._param_types.STRING,
                },
            )
        )
    claims: list[EntityClaim] = []
    for row in rows:
        raw_body = row[1]
        body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
        claims.append(EntityClaim(entity_id=str(row[0]), body=body))
    return claims


def _raw_workspace(store: Any, workspace_id: str) -> Workspace | None:
    read_entity = getattr(store, "_read_entity", None)
    if callable(read_entity):
        workspace = read_entity("workspace", workspace_id, Workspace)
        return workspace if isinstance(workspace, Workspace) else None
    workspaces = getattr(store, "workspaces", None)
    if isinstance(workspaces, dict):
        workspace = workspaces.get(workspace_id)
        return workspace if isinstance(workspace, Workspace) else None
    raise TypeError("store does not expose a raw workspace reader")


def _exclude(candidate: Candidate, reason: str) -> Candidate:
    candidate.decision = "exclude"
    candidate.reason = reason
    candidate.status = "skipped"
    return candidate


def _attribute(
    candidate: Candidate,
    *,
    store: Any,
    initiating_user_id: str | None,
) -> Candidate:
    workspace_id = candidate.workspace_id
    if not workspace_id:
        return _exclude(candidate, "no_workspace_metadata")
    workspace = _raw_workspace(store, workspace_id)
    if workspace is None:
        return _exclude(candidate, "unknown_workspace")
    if workspace.federated_home:
        return _exclude(candidate, "no_owner")
    user_id = initiating_user_id or workspace.owner_user_id
    if not user_id:
        return _exclude(candidate, "no_owner")
    user = store.get_user(user_id)
    if user is None:
        return _exclude(candidate, "no_owner")
    candidate.user_id = user_id
    candidate.user_email = user.email
    return candidate


def _is_post_cutover(
    *,
    created_at: datetime,
    metadata: dict[str, Any],
    cutover: datetime,
    provider_ref: str = "",
) -> tuple[bool, bool]:
    """Decide whether Phase 2 already accrued this Stripe object.

    Two witnesses: the object's creation time versus the Phase-2 deploy
    instant, and the presence of ``initiating_user_id``, which only the
    post-deploy control plane stamps. The stamp is the direct fingerprint of
    the code path that accrued (or did not), so it DECIDES; time is the
    cross-check. A disagreement means the cut-over constant is off by the
    rollout window (old instances still credited for minutes after the run
    started, or the reverse); the row is flagged so the operator sees every
    such object in the dry-run report rather than the script guessing
    silently or refusing to run.

    Returns (post_cutover, witnesses_disagree).
    """
    raw_initiator = metadata.get("initiating_user_id")
    has_initiator = isinstance(raw_initiator, str) and bool(raw_initiator)
    by_time = created_at >= cutover
    disagree = by_time != has_initiator
    if disagree:
        print(
            "WARNING: cut-over witnesses disagree for "
            f"{provider_ref or 'stripe object'}: created_at={created_at.isoformat()} "
            f"(>= cutover: {by_time}) but initiating_user_id "
            f"{'present' if has_initiator else 'absent'}; the stamp decides "
            f"({'excluded as post-cutover' if has_initiator else 'included as pre-cutover'})"
        )
    return has_initiator, disagree


def _payment_intent_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(_mapping(value).get("id") or "")


def _stripe_refunds(stripe_client: Any) -> dict[str, RefundState]:
    totals: dict[str, list[int | bool]] = {}
    page = stripe_client.Charge.list(limit=100)
    for raw_charge in page.auto_paging_iter():
        charge = _mapping(raw_charge)
        payment_intent_id = _payment_intent_id(charge.get("payment_intent"))
        if not payment_intent_id:
            continue
        amount = charge.get("amount")
        refunded = charge.get("amount_refunded")
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or not isinstance(refunded, int)
            or isinstance(refunded, bool)
            or amount < 0
            or refunded < 0
            or refunded > amount
        ):
            raise ValueError(f"invalid Stripe Charge amounts for {payment_intent_id}")
        aggregate = totals.setdefault(payment_intent_id, [0, 0, False])
        aggregate[0] = int(aggregate[0]) + amount
        aggregate[1] = int(aggregate[1]) + refunded
        aggregate[2] = bool(aggregate[2]) or bool(charge.get("disputed"))
    return {
        payment_intent_id: RefundState(
            amount_cents=int(values[0]),
            amount_refunded_cents=int(values[1]),
            disputed=bool(values[2]),
        )
        for payment_intent_id, values in totals.items()
    }


def _apply_refund_state(
    candidate: Candidate,
    refund: RefundState | None,
) -> Candidate:
    if refund is None:
        return candidate
    if refund.disputed:
        candidate.amount_microdollars = 0
        return _exclude(candidate, "disputed")
    if refund.amount_cents and refund.amount_refunded_cents == refund.amount_cents:
        candidate.amount_microdollars = 0
        return _exclude(candidate, "refunded")
    if refund.amount_refunded_cents:
        candidate.partial_refund = True
        candidate.amount_microdollars = max(
            0,
            candidate.amount_microdollars
            - refund.amount_refunded_cents * MICRODOLLARS_PER_CENT,
        )
        if candidate.amount_microdollars <= 0:
            return _exclude(candidate, "refunded")
    return candidate


def _stripe_candidates(
    *,
    store: Any,
    stripe_client: Any,
    cutover: datetime,
) -> list[Candidate]:
    refunds = _stripe_refunds(stripe_client)
    candidates: list[Candidate] = []
    session_payment_intents: set[str] = set()
    page = stripe_client.checkout.Session.list(
        limit=100,
        expand=["data.payment_intent"],
    )
    for raw_session in page.auto_paging_iter():
        session = _mapping(raw_session)
        metadata = _metadata(session.get("metadata"))
        payment_intent_id = _payment_intent_id(session.get("payment_intent"))
        if payment_intent_id:
            session_payment_intents.add(payment_intent_id)
        workspace_raw = metadata.get("workspace_id")
        workspace_id = workspace_raw if isinstance(workspace_raw, str) and workspace_raw else None
        created_at, created_iso = _provider_timestamp(session.get("created"))
        amount_total = session.get("amount_total")
        gross_cents = amount_total if isinstance(amount_total, int) and not isinstance(amount_total, bool) else None
        candidate = Candidate(
            source="stripe_checkout",
            provider_ref=payment_intent_id or str(session.get("id") or ""),
            workspace_id=workspace_id,
            user_id=None,
            user_email=None,
            amount_microdollars=0,
            gross_cents=gross_cents,
            provider_created_at=created_iso,
            payment_method=str(metadata.get("payment_method") or "stripe"),
            backfill_event_id=(
                f"lifetime_backfill:stripe:{payment_intent_id}"
                if payment_intent_id
                else None
            ),
        )
        if session.get("mode") != "payment" or session.get("payment_status") != "paid":
            candidates.append(_exclude(candidate, "non_payment_mode"))
            continue
        if workspace_id is None:
            candidates.append(_exclude(candidate, "no_workspace_metadata"))
            continue
        if not payment_intent_id:
            raise ValueError(f"paid Stripe Checkout Session {session.get('id')} has no PaymentIntent")
        if gross_cents is None:
            raise ValueError(f"paid Stripe Checkout Session {session.get('id')} has no amount_total")
        candidate.amount_microdollars = _checkout_credit_amount_microdollars(
            metadata=metadata,
            amount_total_cents=gross_cents,
        )
        candidate = _apply_refund_state(candidate, refunds.get(payment_intent_id))
        if candidate.decision == "exclude":
            candidates.append(candidate)
            continue
        post_cutover, disagree = _is_post_cutover(
            created_at=created_at,
            metadata=metadata,
            cutover=cutover,
            provider_ref=payment_intent_id,
        )
        candidate.cutover_witness_disagreement = disagree
        initiating_user_id = metadata.get("initiating_user_id")
        candidate = _attribute(
            candidate,
            store=store,
            initiating_user_id=(
                initiating_user_id
                if isinstance(initiating_user_id, str) and initiating_user_id
                else None
            ),
        )
        if post_cutover and candidate.decision == "include":
            candidate = _exclude(candidate, "post_cutover")
        candidates.append(candidate)

    page = stripe_client.PaymentIntent.list(limit=100)
    for raw_payment_intent in page.auto_paging_iter():
        payment_intent = _mapping(raw_payment_intent)
        payment_intent_id = str(payment_intent.get("id") or "")
        if not payment_intent_id or payment_intent_id in session_payment_intents:
            continue
        metadata = _metadata(payment_intent.get("metadata"))
        is_auto_refill = metadata.get("auto_refill") == "true"
        is_x402 = metadata.get("payment_method") == "x402"
        if payment_intent.get("status") != "succeeded" or not (is_auto_refill or is_x402):
            continue
        if is_auto_refill and is_x402:
            raise ValueError(f"Stripe PaymentIntent {payment_intent_id} has conflicting metadata")
        workspace_raw = metadata.get("workspace_id")
        workspace_id = workspace_raw if isinstance(workspace_raw, str) and workspace_raw else None
        created_at, created_iso = _provider_timestamp(payment_intent.get("created"))
        gross = payment_intent.get("amount")
        gross_cents = gross if isinstance(gross, int) and not isinstance(gross, bool) else None
        source = "stripe_auto_refill" if is_auto_refill else "stripe_x402"
        candidate = Candidate(
            source=source,
            provider_ref=payment_intent_id,
            workspace_id=workspace_id,
            user_id=None,
            user_email=None,
            amount_microdollars=0,
            gross_cents=gross_cents,
            provider_created_at=created_iso,
            payment_method="stripe_auto_refill" if is_auto_refill else "stablecoin",
            backfill_event_id=f"lifetime_backfill:stripe:{payment_intent_id}",
        )
        if workspace_id is None:
            candidates.append(_exclude(candidate, "no_workspace_metadata"))
            continue
        if is_auto_refill:
            candidate.amount_microdollars = _auto_refill_credit_amount_microdollars(
                metadata=metadata,
                payment_intent_amount_cents=payment_intent.get("amount"),
            )
        else:
            candidate.amount_microdollars = min(
                _settled_amount_microdollars(payment_intent),
                _metadata_requested_microdollars(metadata),
            )
            if candidate.amount_microdollars <= 0:
                raise ValueError(f"Stripe x402 PaymentIntent {payment_intent_id} has no principal")
        candidate = _apply_refund_state(candidate, refunds.get(payment_intent_id))
        if candidate.decision == "exclude":
            candidates.append(candidate)
            continue
        post_cutover, disagree = _is_post_cutover(
            created_at=created_at,
            metadata=metadata,
            cutover=cutover,
            provider_ref=payment_intent_id,
        )
        candidate.cutover_witness_disagreement = disagree
        initiating_user_id = metadata.get("initiating_user_id")
        candidate = _attribute(
            candidate,
            store=store,
            initiating_user_id=(
                initiating_user_id
                if isinstance(initiating_user_id, str) and initiating_user_id
                else None
            ),
        )
        if post_cutover and candidate.decision == "include":
            candidate = _exclude(candidate, "post_cutover")
        candidates.append(candidate)
    for candidate in candidates:
        if candidate.source.startswith("stripe_") and candidate.decision == "include":
            assert candidate.reason is None
            assert candidate.user_id
    return candidates


def _read_csv(path: Path, expected_fields: set[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or ()) != expected_fields:
            raise ValueError(
                f"{path} must have exactly these columns: {','.join(sorted(expected_fields))}"
            )
        return [dict(row) for row in reader]


def _paypal_csv_candidates(
    *,
    store: Any,
    path: Path,
    cutover: datetime,
) -> list[Candidate]:
    rows = _read_csv(
        path,
        {"capture_id", "workspace_id", "credit_amount_microdollars", "created_at"},
    )
    candidates: list[Candidate] = []
    for row in rows:
        capture_id = row["capture_id"].strip()
        workspace_id = row["workspace_id"].strip()
        amount = int(row["credit_amount_microdollars"])
        if not capture_id or not workspace_id or amount <= 0:
            raise ValueError(f"invalid PayPal CSV row: {row}")
        created_at, created_iso = _provider_timestamp(row["created_at"].strip())
        candidate = Candidate(
            source="paypal",
            provider_ref=capture_id,
            workspace_id=workspace_id,
            user_id=None,
            user_email=None,
            amount_microdollars=amount,
            gross_cents=(amount // MICRODOLLARS_PER_CENT if amount % MICRODOLLARS_PER_CENT == 0 else None),
            provider_created_at=created_iso,
            payment_method="paypal",
            backfill_event_id=f"lifetime_backfill:paypal:{capture_id}",
        )
        candidate = _attribute(candidate, store=store, initiating_user_id=None)
        if created_at >= cutover and candidate.decision == "include":
            candidate = _exclude(candidate, "post_cutover")
        candidates.append(candidate)
    return candidates


def _paypal_claim_candidates(
    *,
    store: Any,
    claims: Iterable[EntityClaim],
    paypal_fetch: Callable[[str], dict[str, Any]],
    cutover: datetime,
    skip_capture_ids: set[str],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for claim in claims:
        capture_id = claim.entity_id.removeprefix("paypal_capture:")
        if capture_id in skip_capture_ids:
            continue
        capture = paypal_fetch(capture_id)
        payload = _paypal_capture_payload(capture, order_id="")
        created_raw = capture.get("create_time") or claim.created_at
        created_at, created_iso = _provider_timestamp(created_raw)
        amount = int(payload["amount_microdollars"])
        gross_microdollars = int(payload["charge_amount_microdollars"])
        candidate = Candidate(
            source="paypal",
            provider_ref=capture_id,
            workspace_id=str(payload["workspace_id"]),
            user_id=None,
            user_email=None,
            amount_microdollars=amount,
            gross_cents=gross_microdollars // MICRODOLLARS_PER_CENT,
            provider_created_at=created_iso,
            payment_method="paypal",
            backfill_event_id=f"lifetime_backfill:paypal:{capture_id}",
        )
        if str(payload["status"]) != "COMPLETED":
            candidates.append(_exclude(candidate, "refunded"))
            continue
        initiating_user_id = payload.get("initiating_user_id")
        candidate = _attribute(
            candidate,
            store=store,
            initiating_user_id=(
                initiating_user_id
                if isinstance(initiating_user_id, str) and initiating_user_id
                else None
            ),
        )
        if created_at >= cutover and candidate.decision == "include":
            candidate = _exclude(candidate, "post_cutover")
        candidates.append(candidate)
    return candidates


def _adyen_candidates(
    *,
    store: Any,
    claims: Iterable[EntityClaim],
    settings: Settings,
    cutover: datetime,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for claim in claims:
        merchant_reference = claim.entity_id.removeprefix("adyen_checkout:")
        reference = _parse_checkout_reference(
            merchant_reference,
            reference_key=str(settings.adyen_reference_key or ""),
        )
        created_at, created_iso = _provider_timestamp(claim.created_at)
        candidate = Candidate(
            source="adyen",
            provider_ref=merchant_reference,
            workspace_id=reference.workspace_id,
            user_id=None,
            user_email=None,
            amount_microdollars=reference.credit_amount_cents * MICRODOLLARS_PER_CENT,
            gross_cents=reference.charge_amount_cents,
            provider_created_at=created_iso,
            payment_method="adyen",
            backfill_event_id=f"lifetime_backfill:adyen:{merchant_reference}",
        )
        candidate = _attribute(candidate, store=store, initiating_user_id=None)
        if created_at >= cutover and candidate.decision == "include":
            candidate = _exclude(candidate, "post_cutover")
        candidates.append(candidate)
    return candidates


def _manual_candidates(store: Any, path: Path) -> list[Candidate]:
    rows = _read_csv(path, {"event_id", "user_id", "amount_microdollars"})
    candidates: list[Candidate] = []
    for row in rows:
        original_event_id = row["event_id"].strip()
        user_id = row["user_id"].strip()
        amount = int(row["amount_microdollars"])
        if not original_event_id or not user_id or amount <= 0:
            raise ValueError(f"invalid manual grant CSV row: {row}")
        user = store.get_user(user_id)
        candidate = Candidate(
            source="manual",
            provider_ref=original_event_id,
            workspace_id=None,
            user_id=user_id if user is not None else None,
            user_email=(user.email if user is not None else None),
            amount_microdollars=amount,
            gross_cents=(amount // MICRODOLLARS_PER_CENT if amount % MICRODOLLARS_PER_CENT == 0 else None),
            provider_created_at=None,
            payment_method="manual",
            backfill_event_id=f"lifetime_backfill:manual:{original_event_id}",
        )
        candidates.append(candidate if user is not None else _exclude(candidate, "no_owner"))
    return candidates


def _claim_datetime(claim: EntityClaim) -> datetime | None:
    return _parse_timestamp(claim.created_at) if claim.created_at else None


def _stripe_claim_cross_check(
    candidates: list[Candidate],
    claims: list[EntityClaim],
    cutover: datetime,
) -> dict[str, Any]:
    included = [
        candidate
        for candidate in candidates
        if candidate.source.startswith("stripe_") and candidate.decision == "include"
    ]
    pre_cutover_claims = [
        claim
        for claim in claims
        if (created := _claim_datetime(claim)) is not None and created < cutover
    ]
    unmatched_candidate_refs = {candidate.provider_ref for candidate in included}
    unmatched_claim_ids = {claim.entity_id for claim in pre_cutover_claims}

    for claim in pre_cutover_claims:
        if not claim.entity_id.startswith("stripe_checkout:"):
            continue
        provider_ref = claim.entity_id.removeprefix("stripe_checkout:")
        if provider_ref in unmatched_candidate_refs:
            unmatched_candidate_refs.remove(provider_ref)
            unmatched_claim_ids.remove(claim.entity_id)

    candidates_by_ref = {candidate.provider_ref: candidate for candidate in included}
    claims_by_id = {claim.entity_id: claim for claim in pre_cutover_claims}
    for provider_ref in sorted(list(unmatched_candidate_refs)):
        candidate = candidates_by_ref[provider_ref]
        if candidate.provider_created_at is None:
            continue
        provider_created = _parse_timestamp(candidate.provider_created_at)
        possible: list[tuple[float, str]] = []
        for claim_id in unmatched_claim_ids:
            if not claim_id.startswith("evt_"):
                continue
            claim_created = _claim_datetime(claims_by_id[claim_id])
            if claim_created is None:
                continue
            distance = abs((claim_created - provider_created).total_seconds())
            if distance <= _HEURISTIC_WINDOW_SECONDS:
                possible.append((distance, claim_id))
        if possible:
            _, claim_id = min(possible)
            unmatched_candidate_refs.remove(provider_ref)
            unmatched_claim_ids.remove(claim_id)

    return {
        "label": f"heuristic +/-{_HEURISTIC_WINDOW_SECONDS}s time correlation",
        "included_stripe_rows": len(included),
        "pre_cutover_claims": len(pre_cutover_claims),
        "unmatched_provider_refs": sorted(unmatched_candidate_refs),
        "unmatched_claim_ids": sorted(unmatched_claim_ids),
    }


def _assign_preflight_statuses(
    candidates: list[Candidate],
    *,
    existing_event_ids: set[str],
    only_user: str | None,
) -> None:
    seen_event_ids: set[str] = set()
    for candidate in candidates:
        if candidate.decision != "include":
            candidate.status = "skipped"
            continue
        if not candidate.backfill_event_id:
            raise ValueError(f"included row {candidate.provider_ref} has no backfill event id")
        if candidate.backfill_event_id in seen_event_ids:
            raise ValueError(f"duplicate backfill event id {candidate.backfill_event_id}")
        seen_event_ids.add(candidate.backfill_event_id)
        if only_user is not None and candidate.user_id != only_user:
            candidate.status = "skipped"
        elif candidate.backfill_event_id in existing_event_ids:
            candidate.status = "already_applied"
        else:
            candidate.status = "would_apply"


def _would_apply_total(candidates: Iterable[Candidate]) -> int:
    return sum(
        candidate.amount_microdollars
        for candidate in candidates
        if candidate.status == "would_apply"
    )


def _user_summary(
    candidates: list[Candidate],
    *,
    before: dict[str, int],
    after: dict[str, int],
    delta_statuses: set[str],
) -> list[dict[str, Any]]:
    deltas: dict[str, int] = defaultdict(int)
    emails: dict[str, str | None] = {}
    for candidate in candidates:
        if candidate.user_id is None:
            continue
        emails[candidate.user_id] = candidate.user_email
        if candidate.status in delta_statuses:
            deltas[candidate.user_id] += candidate.amount_microdollars
    return [
        {
            "user_id": user_id,
            "email": emails[user_id],
            "before": before[user_id],
            "delta": deltas[user_id],
            "after": after[user_id],
        }
        for user_id in sorted(emails)
    ]


def _source_totals(candidates: Iterable[Candidate]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        source = totals.setdefault(
            candidate.source,
            {
                "rows": 0,
                "included_rows": 0,
                "included_microdollars": 0,
                "would_apply_microdollars": 0,
                "applied_microdollars": 0,
            },
        )
        source["rows"] += 1
        if candidate.decision == "include":
            source["included_rows"] += 1
            source["included_microdollars"] += candidate.amount_microdollars
        if candidate.status == "would_apply":
            source["would_apply_microdollars"] += candidate.amount_microdollars
        if candidate.status == "applied":
            source["applied_microdollars"] += candidate.amount_microdollars
    return dict(sorted(totals.items()))


def _make_summary(
    candidates: list[Candidate],
    *,
    mode: str,
    before: dict[str, int],
    after: dict[str, int],
    cross_check: dict[str, Any],
    verification: dict[str, Any] | None = None,
    refused_reason: str | None = None,
) -> dict[str, Any]:
    delta_statuses = {"applied"} if mode == "apply" else {"would_apply"}
    users = _user_summary(
        candidates,
        before=before,
        after=after,
        delta_statuses=delta_statuses,
    )
    return {
        "record_type": "summary",
        "mode": mode,
        "would_apply_microdollars": _would_apply_total(candidates),
        "applied_microdollars": sum(
            candidate.amount_microdollars
            for candidate in candidates
            if candidate.status == "applied"
        ),
        "totals_by_source": _source_totals(candidates),
        "per_user": users,
        "users_crossing_25_dollars": [
            row["user_id"]
            for row in users
            if row["before"] < VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
            <= row["after"]
        ],
        "stripe_claim_cross_check": cross_check,
        "cutover_witness_disagreements": [
            candidate.provider_ref
            for candidate in candidates
            if candidate.cutover_witness_disagreement
        ],
        "excluded": [
            {
                "source": candidate.source,
                "provider_ref": candidate.provider_ref,
                "reason": candidate.reason,
            }
            for candidate in candidates
            if candidate.decision == "exclude"
        ],
        "verification": verification,
        "refused_reason": refused_reason,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(
        f"TOTAL: would_apply_microdollars={summary['would_apply_microdollars']} "
        f"applied_microdollars={summary['applied_microdollars']}"
    )
    for source, totals in summary["totals_by_source"].items():
        print(f"SOURCE {source}: {json.dumps(totals, sort_keys=True)}")
    for row in summary["per_user"]:
        print(
            "USER "
            f"user_id={row['user_id']} email={row['email']} before={row['before']} "
            f"delta={row['delta']} after={row['after']}"
        )
    cross_check = summary["stripe_claim_cross_check"]
    print(
        "STRIPE CLAIM CROSS-CHECK (HEURISTIC +/-600s): "
        f"included={cross_check['included_stripe_rows']} "
        f"pre_cutover_claims={cross_check['pre_cutover_claims']}"
    )
    print(
        "STRIPE unmatched provider rows (heuristic): "
        f"{json.dumps(cross_check['unmatched_provider_refs'])}"
    )
    print(
        "STRIPE unmatched claims (heuristic): "
        f"{json.dumps(cross_check['unmatched_claim_ids'])}"
    )
    print(f"EXCLUDED: {json.dumps(summary['excluded'], sort_keys=True)}")


def _write_report(path: Path, candidates: list[Candidate], summary: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate.as_dict(), sort_keys=True) + "\n")
        handle.write(json.dumps(summary, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    secret_group = parser.add_mutually_exclusive_group()
    secret_group.add_argument("--stripe-secret-key-env", metavar="NAME")
    secret_group.add_argument("--stripe-secret-key-file", type=Path, metavar="PATH")
    parser.add_argument(
        "--paypal",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Fetch claimed PayPal captures (default: on when credentials are configured)",
    )
    parser.add_argument("--paypal-csv", type=Path)
    parser.add_argument("--manual-grants-csv", type=Path)
    parser.add_argument("--cutover", default=DEFAULT_CUTOVER)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-total-microdollars", type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--only-user")
    return parser


def _stripe_secret(args: argparse.Namespace, settings: Settings) -> str | None:
    if args.stripe_secret_key_env:
        return os.environ.get(str(args.stripe_secret_key_env))
    if args.stripe_secret_key_file:
        return args.stripe_secret_key_file.read_text(encoding="utf-8").strip()
    return settings.stripe_secret_key


def main(
    argv: list[str] | None = None,
    *,
    store: Any | None = None,
    stripe_client: Any | None = None,
    paypal_fetch: Callable[[str], dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.apply and args.expected_total_microdollars is None:
        print("REFUSED: --expected-total-microdollars is required with --apply")
        return 2
    if args.expected_total_microdollars is not None and args.expected_total_microdollars < 0:
        print("REFUSED: --expected-total-microdollars cannot be negative")
        return 2
    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("REFUSED: --apply requires TR_STORAGE_BACKEND=spanner-bigtable")
        return 2
    try:
        cutover = _parse_timestamp(args.cutover)
        settings = Settings()
        active_store = create_store(settings) if store is None else store
        if stripe_client is None:
            secret = _stripe_secret(args, settings)
            if not secret:
                print("REFUSED: Stripe secret key is not configured")
                return 2
            import stripe

            stripe.api_key = secret
            active_stripe_client: Any = stripe
        else:
            active_stripe_client = stripe_client

        existing_backfill_claims = _scan_entity_claims(active_store, "lifetime_backfill:")
        existing_event_ids = {claim.entity_id for claim in existing_backfill_claims}
        stripe_claims = [
            *_scan_entity_claims(active_store, "evt_"),
            *_scan_entity_claims(active_store, "stripe_checkout:"),
        ]
        paypal_claims = _scan_entity_claims(active_store, "paypal_capture:")
        adyen_claims = _scan_entity_claims(active_store, "adyen_checkout:")

        candidates = _stripe_candidates(
            store=active_store,
            stripe_client=active_stripe_client,
            cutover=cutover,
        )
        csv_paypal_candidates: list[Candidate] = []
        if args.paypal_csv:
            csv_paypal_candidates = _paypal_csv_candidates(
                store=active_store,
                path=args.paypal_csv,
                cutover=cutover,
            )
            candidates.extend(csv_paypal_candidates)

        paypal_enabled = (
            bool(args.paypal)
            if args.paypal is not None
            else bool(paypal_fetch is not None or settings.paypal_enabled)
        )
        if paypal_enabled:
            if paypal_fetch is None:
                if not settings.paypal_enabled:
                    print("PAYPAL: skipped loudly; credentials are not configured")
                else:
                    def configured_paypal_fetch(capture_id: str) -> dict[str, Any]:
                        return fetch_paypal_capture(settings, capture_id)

                    paypal_fetch = configured_paypal_fetch
            if paypal_fetch is not None:
                candidates.extend(
                    _paypal_claim_candidates(
                        store=active_store,
                        claims=paypal_claims,
                        paypal_fetch=paypal_fetch,
                        cutover=cutover,
                        skip_capture_ids={row.provider_ref for row in csv_paypal_candidates},
                    )
                )
        else:
            print("PAYPAL: skipped loudly; credentials are not configured or --no-paypal was set")

        candidates.extend(
            _adyen_candidates(
                store=active_store,
                claims=adyen_claims,
                settings=settings,
                cutover=cutover,
            )
        )
        if args.manual_grants_csv:
            candidates.extend(_manual_candidates(active_store, args.manual_grants_csv))

        _assign_preflight_statuses(
            candidates,
            existing_event_ids=existing_event_ids,
            only_user=args.only_user,
        )
        would_apply = _would_apply_total(candidates)
        user_ids = sorted(
            {
                candidate.user_id
                for candidate in candidates
                if candidate.user_id is not None
            }
        )
        before = {
            user_id: active_store.get_lifetime_topup_microdollars(user_id)
            for user_id in user_ids
        }
        cross_check = _stripe_claim_cross_check(candidates, stripe_claims, cutover)

        if args.apply and would_apply != args.expected_total_microdollars:
            after = dict(before)
            summary = _make_summary(
                candidates,
                mode="refused",
                before=before,
                after=after,
                cross_check=cross_check,
                refused_reason="expected_total_mismatch",
            )
            _print_summary(summary)
            print(
                "REFUSED: expected-total gate did not match "
                f"expected={args.expected_total_microdollars} actual={would_apply}"
            )
            if args.report:
                _write_report(args.report, candidates, summary)
            return 2

        if not args.apply:
            projected_after = dict(before)
            for candidate in candidates:
                if candidate.status == "would_apply" and candidate.user_id is not None:
                    projected_after[candidate.user_id] += candidate.amount_microdollars
            summary = _make_summary(
                candidates,
                mode="dry_run",
                before=before,
                after=projected_after,
                cross_check=cross_check,
            )
            _print_summary(summary)
            if args.report:
                _write_report(args.report, candidates, summary)
            return 0

        applied_delta_by_user: dict[str, int] = defaultdict(int)
        applied_count = 0
        for candidate in candidates:
            if candidate.status != "would_apply":
                continue
            assert candidate.user_id is not None
            assert candidate.backfill_event_id is not None
            applied = active_store.add_lifetime_topup(
                candidate.user_id,
                candidate.amount_microdollars,
                candidate.backfill_event_id,
            )
            candidate.status = "applied" if applied else "already_applied"
            if applied:
                applied_count += 1
                applied_delta_by_user[candidate.user_id] += candidate.amount_microdollars

        after = {
            user_id: active_store.get_lifetime_topup_microdollars(user_id)
            for user_id in user_ids
        }
        touched_user_ids = sorted(
            {
                candidate.user_id
                for candidate in candidates
                if candidate.decision == "include"
                and candidate.user_id is not None
                and (args.only_user is None or candidate.user_id == args.only_user)
            }
        )
        balances_ok = all(
            before[user_id] + applied_delta_by_user[user_id] == after[user_id]
            for user_id in touched_user_ids
        )
        if not balances_ok:
            raise RuntimeError("post-apply lifetime top-up balance verification failed")

        after_backfill_claims = _scan_entity_claims(active_store, "lifetime_backfill:")
        after_event_ids = {claim.entity_id for claim in after_backfill_claims}
        event_count_delta = len(after_event_ids) - len(existing_event_ids)
        if event_count_delta != applied_count:
            raise RuntimeError(
                "post-apply lifetime_backfill claim-count verification failed: "
                f"delta={event_count_delta} applied={applied_count}"
            )
        second_dry_run_total = sum(
            candidate.amount_microdollars
            for candidate in candidates
            if candidate.decision == "include"
            and candidate.backfill_event_id not in after_event_ids
            and (args.only_user is None or candidate.user_id == args.only_user)
        )
        if second_dry_run_total != 0:
            raise RuntimeError(
                f"post-apply second dry-run verification failed: {second_dry_run_total}"
            )
        verification = {
            "balances": {"ok": True, "users": len(touched_user_ids)},
            "lifetime_backfill_event_count": {
                "ok": True,
                "before": len(existing_event_ids),
                "after": len(after_event_ids),
                "delta": event_count_delta,
                "applied": applied_count,
            },
            "second_dry_run": {"ok": True, "would_apply_microdollars": 0},
            "verified_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        }
        summary = _make_summary(
            candidates,
            mode="apply",
            before=before,
            after=after,
            cross_check=cross_check,
            verification=verification,
        )
        _print_summary(summary)
        print(f"VERIFY balances: PASS users={len(touched_user_ids)}")
        print(
            "VERIFY lifetime_backfill event count: PASS "
            f"before={len(existing_event_ids)} after={len(after_event_ids)} "
            f"delta={event_count_delta} applied={applied_count}"
        )
        print("VERIFY second dry-run: PASS would_apply_microdollars=0")
        if args.report:
            _write_report(args.report, candidates, summary)
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
