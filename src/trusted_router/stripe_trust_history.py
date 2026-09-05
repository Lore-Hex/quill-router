"""Stripe/x402 history conversion shared by backfill and tail reconciliation."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trusted_router.money import MICRODOLLARS_PER_CENT
from trusted_router.routes.internal.webhook import (
    _auto_refill_credit_amount_microdollars,
    _checkout_credit_amount_microdollars,
)
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.trust_reconciliation import OutstandingAdverse
from trusted_router.trust_tiers import payment_or_grant_event


@dataclass(frozen=True, slots=True)
class StripeTrustScan:
    payments: tuple[TrustEvent, ...]
    adverse: tuple[AdverseTrustEvent, ...]
    source_events: tuple[TrustEvent, ...]
    outstanding: tuple[OutstandingAdverse, ...]
    unmatched_ids: tuple[str, ...]


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
    raise TypeError(f"unsupported Stripe object {type(value).__name__}")


def _rows(page: Any) -> list[Any]:
    auto_paging = getattr(page, "auto_paging_iter", None)
    if callable(auto_paging):
        return list(auto_paging())
    data = getattr(page, "data", page)
    return list(data)


def latest_adverse_event_watermark(
    stripe_client: Any,
    *,
    kind: str,
    adverse_ref: str,
    occurred_at: datetime,
) -> str | None:
    """Return the latest relevant Stripe Event ordering key, if available."""

    event_resource = getattr(stripe_client, "Event", None)
    if event_resource is None:
        return None
    event_types = (
        ("refund.created", "refund.updated", "refund.failed")
        if kind == "refund"
        else (
            "charge.dispute.created",
            "charge.dispute.updated",
            "charge.dispute.closed",
            "charge.dispute.funds_withdrawn",
            "charge.dispute.funds_reinstated",
        )
    )
    latest: tuple[int, str] | None = None
    for event_type in event_types:
        events = _rows(
            event_resource.list(
                type=event_type,
                created={"gte": int(occurred_at.timestamp())},
                limit=100,
            )
        )
        for raw in events:
            event = _mapping(raw)
            data = event.get("data")
            obj = data.get("object") if isinstance(data, dict) else None
            if isinstance(obj, dict) and str(obj.get("id") or "") == adverse_ref:
                candidate = (int(event.get("created") or 0), str(event.get("id") or ""))
                latest = max(latest, candidate) if latest is not None else candidate
    return None if latest is None else f"{latest[0]:020d}:{latest[1]}"


def _enrich_adverse_watermarks(
    stripe_client: Any,
    rows: list[Any],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for raw in rows:
        row = _mapping(raw)
        adverse_ref = str(row.get("id") or "")
        if adverse_ref and row.get("created"):
            watermark = latest_adverse_event_watermark(
                stripe_client,
                kind=kind,
                adverse_ref=adverse_ref,
                occurred_at=datetime.fromtimestamp(int(row["created"]), tz=UTC),
            )
            if watermark is not None:
                row["_trust_ordering_watermark"] = watermark
        enriched.append(row)
    return enriched


def _timestamp(raw: Any) -> datetime:
    if isinstance(raw, bool):
        raise ValueError("invalid provider timestamp")
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("provider timestamp must be timezone-aware")
        return parsed.astimezone(UTC)
    raise ValueError("provider timestamp is missing")


def _object_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(_mapping(value).get("id") or "")


def _provider_for_payment(payment_intent: Mapping[str, Any]) -> str:
    metadata = payment_intent.get("metadata")
    return (
        "x402"
        if isinstance(metadata, Mapping) and metadata.get("payment_method") == "x402"
        else "stripe"
    )


def _watermark(obj: Mapping[str, Any]) -> str:
    explicit = obj.get("_trust_ordering_watermark")
    if isinstance(explicit, str) and explicit:
        return explicit
    created = int(obj.get("created") or 0)
    return f"{created:020d}:{obj.get('id') or ''}"


def _payment_principal(payment_intent: dict[str, Any], provider: str) -> tuple[int, str]:
    metadata_value = payment_intent.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    amount_cents = payment_intent.get("amount")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ValueError(f"PaymentIntent {payment_intent.get('id')} has no positive amount")
    if provider == "x402":
        requested = int(metadata.get("amount_microdollars") or 0)
        settled = int(payment_intent.get("amount_received") or amount_cents) * MICRODOLLARS_PER_CENT
        principal = min(requested, settled)
        subtype = "x402"
    elif metadata.get("auto_refill") == "true":
        principal = _auto_refill_credit_amount_microdollars(
            metadata=metadata,
            payment_intent_amount_cents=amount_cents,
        )
        subtype = "auto_refill"
    else:
        principal = _checkout_credit_amount_microdollars(
            metadata=metadata,
            amount_total_cents=amount_cents,
        )
        subtype = "checkout"
    if principal <= 0:
        raise ValueError(f"PaymentIntent {payment_intent.get('id')} has no credited principal")
    return principal, subtype


def _payment_event(payment_intent: dict[str, Any], *, recorded_at: datetime) -> TrustEvent:
    payment_intent_id = str(payment_intent.get("id") or "")
    metadata_value = payment_intent.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    workspace_id = str(metadata.get("workspace_id") or "")
    if not payment_intent_id.startswith("pi_") or not workspace_id:
        raise ValueError("qualifying PaymentIntent requires id and stored workspace metadata")
    provider = _provider_for_payment(payment_intent)
    credited, subtype = _payment_principal(payment_intent, provider)
    payment_amount_cents = (
        int(payment_intent.get("amount_received") or payment_intent["amount"])
        if provider == "x402"
        else int(payment_intent["amount"])
    )
    amount_micro = payment_amount_cents * MICRODOLLARS_PER_CENT
    event = payment_or_grant_event(
        workspace_id,
        f"trust-backfill:{provider}:payment:{payment_intent_id}",
        credited,
        CreditProvenance(
            source=subtype,
            provider=provider,
            external_ref=payment_intent_id,
            occurred_at=_timestamp(payment_intent.get("created")),
        ),
        recorded_at=recorded_at,
        payment_amount_microdollars=amount_micro,
        currency=str(payment_intent.get("currency") or "usd"),
    )
    return dataclasses.replace(event, provider_ordering_watermark=_watermark(payment_intent))


def _refund_status(raw: Any) -> str:
    return {
        "pending": "pending",
        "requires_action": "pending",
        "succeeded": "succeeded",
        "failed": "failed",
        "canceled": "failed",
        "cancelled": "failed",
        "reversed": "reversed",
    }.get(str(raw or "succeeded").lower(), "pending")


def _dispute_status(raw: Any) -> str:
    status = str(raw or "").lower()
    if status == "won":
        return "won"
    if status == "lost":
        return "lost"
    if status in {"closed", "warning_closed"}:
        return "closed"
    if status in {"warning_needs_response", "warning_under_review"}:
        return "pending"
    return "succeeded"


def _evidence_deadline(dispute: Mapping[str, Any]) -> datetime | None:
    details = dispute.get("evidence_details")
    if not isinstance(details, Mapping) or not details.get("due_by"):
        return None
    return _timestamp(details["due_by"])


def scan_stripe_responses(
    *,
    payment_intents: Iterable[Any],
    refunds: Iterable[Any],
    disputes: Iterable[Any],
    recorded_at: datetime,
    known_payment_intents: Iterable[Any] = (),
) -> StripeTrustScan:
    """Convert recorded list/retrieve responses without performing I/O."""

    payment_objects = [_mapping(row) for row in payment_intents]
    all_payment_objects = [*payment_objects, *(_mapping(row) for row in known_payment_intents)]
    payment_by_id = {
        str(row.get("id") or ""): row
        for row in all_payment_objects
        if str(row.get("id") or "")
    }
    payments: list[TrustEvent] = []
    for row in payment_objects:
        metadata = row.get("metadata")
        if (
            row.get("status") == "succeeded"
            and isinstance(metadata, Mapping)
            and metadata.get("workspace_id")
        ):
            payments.append(_payment_event(row, recorded_at=recorded_at))

    # Adverse facts may refer to a payment created before the scan range. Add
    # those stored PaymentIntents to the source model, but not to the created
    # range's payment-id set unless the caller explicitly listed them there.
    needed_payment_ids: set[str] = set()
    refund_objects = [_mapping(row) for row in refunds]
    dispute_objects = [_mapping(row) for row in disputes]
    for row in [*refund_objects, *dispute_objects]:
        payment_ref = _object_id(row.get("payment_intent"))
        if payment_ref:
            needed_payment_ids.add(payment_ref)
    for payment_ref in sorted(needed_payment_ids):
        stored_payment = payment_by_id.get(payment_ref)
        stored_metadata = (
            stored_payment.get("metadata") if stored_payment is not None else None
        )
        if (
            stored_payment is not None
            and stored_payment.get("status") == "succeeded"
            and isinstance(stored_metadata, Mapping)
            and stored_metadata.get("workspace_id")
            and not any(
                event.original_payment_ref == payment_ref for event in payments
            )
        ):
            payments.append(_payment_event(stored_payment, recorded_at=recorded_at))

    payment_events = {str(row.original_payment_ref): row for row in payments}
    adverse: list[AdverseTrustEvent] = []
    source_adverse: list[TrustEvent] = []
    outstanding: list[OutstandingAdverse] = []
    unmatched: list[str] = []

    def append_adverse(obj: dict[str, Any], *, kind: str) -> None:
        adverse_ref = str(obj.get("id") or "")
        payment_ref = _object_id(obj.get("payment_intent"))
        payment = payment_events.get(payment_ref)
        if not adverse_ref or payment is None:
            unmatched.append(adverse_ref or f"{kind}:missing_id")
            return
        status = (
            _refund_status(obj.get("status"))
            if kind == "refund"
            else _dispute_status(obj.get("status"))
        )
        occurred_at = _timestamp(obj.get("created"))
        event = AdverseTrustEvent(
            event_id=f"trust-backfill:{payment.provider}:{kind}:{adverse_ref}",
            provider=payment.provider,
            kind=kind,
            adverse_ref=adverse_ref,
            original_payment_ref=payment_ref,
            amount_micro=int(obj.get("amount") or 0) * MICRODOLLARS_PER_CENT,
            provider_subtype=str(obj.get("object") or kind),
            lifecycle_status=status,
            occurred_at=occurred_at,
            provider_ordering_watermark=_watermark(obj),
            payload="",
        )
        adverse.append(event)
        source_adverse.append(
            TrustEvent(
                workspace_id=payment.workspace_id,
                event_id=event.event_id,
                kind=kind,
                provider=payment.provider,
                amount_micro=event.amount_micro,
                original_payment_ref=payment_ref,
                adverse_ref=adverse_ref,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                payment_amount_micro=payment.payment_amount_micro,
                currency=payment.currency,
                credited_micro=payment.credited_micro,
                recovered_micro=None,
                provider_subtype=event.provider_subtype,
                lifecycle_status=status,
                cumulative_refunded=None,
                recovery_target=None,
                debit_status=None,
                unrecovered_micro=None,
                provider_ordering_watermark=event.provider_ordering_watermark,
            )
        )
        terminal = (
            status in {"succeeded", "failed", "reversed"}
            if kind == "refund"
            else status in {"won", "lost", "closed"}
        )
        if not terminal:
            outstanding.append(
                OutstandingAdverse(
                    provider=payment.provider,
                    kind=kind,
                    adverse_ref=adverse_ref,
                    original_payment_ref=payment_ref,
                    lifecycle_status=status,
                    occurred_at=occurred_at,
                    evidence_deadline=(
                        _evidence_deadline(obj) if kind == "dispute" else None
                    ),
                )
            )

    for refund in refund_objects:
        append_adverse(refund, kind="refund")
    for dispute in dispute_objects:
        append_adverse(dispute, kind="dispute")
    return StripeTrustScan(
        payments=tuple(payments),
        adverse=tuple(adverse),
        source_events=tuple([*payments, *source_adverse]),
        outstanding=tuple(outstanding),
        unmatched_ids=tuple(sorted(unmatched)),
    )


def scan_created_range(
    stripe_client: Any,
    *,
    start: datetime,
    end: datetime,
    recorded_at: datetime,
    include_event_watermarks: bool = False,
) -> StripeTrustScan:
    """List all trust object kinds by created range, resolving stored payments."""

    created = {"gte": int(start.timestamp()), "lt": int(end.timestamp())}
    payment_intents = _rows(stripe_client.PaymentIntent.list(created=created, limit=100))
    refunds = _rows(stripe_client.Refund.list(created=created, limit=100))
    disputes = _rows(stripe_client.Dispute.list(created=created, limit=100))
    if include_event_watermarks:
        refunds = _enrich_adverse_watermarks(stripe_client, refunds, kind="refund")
        disputes = _enrich_adverse_watermarks(stripe_client, disputes, kind="dispute")
    payment_ids = {
        _object_id(_mapping(row).get("payment_intent"))
        for row in [*refunds, *disputes]
    }
    listed_ids = {str(_mapping(row).get("id") or "") for row in payment_intents}
    known = [
        stripe_client.PaymentIntent.retrieve(payment_id)
        for payment_id in sorted(payment_ids - listed_ids)
        if payment_id
    ]
    return scan_stripe_responses(
        payment_intents=payment_intents,
        refunds=refunds,
        disputes=disputes,
        known_payment_intents=known,
        recorded_at=recorded_at,
    )
