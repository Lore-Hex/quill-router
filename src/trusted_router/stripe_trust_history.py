"""Stripe/x402 history conversion shared by backfill and tail reconciliation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trusted_router.money import MICRODOLLARS_PER_CENT
from trusted_router.routes.internal.webhook import (
    _auto_refill_credit_amount_microdollars,
    _checkout_credit_amount_microdollars,
    _checkout_credit_event_id,
)
from trusted_router.services.x402_billing import x402_event_id
from trusted_router.storage_models import AdverseTrustEvent, CreditProvenance, TrustEvent
from trusted_router.trust_reconciliation import OutstandingAdverse
from trusted_router.trust_tiers import payment_or_grant_event

#: Stripe Event types whose processing credits a workspace. Their ``id`` is the
#: ``stripe_event`` idempotency marker the live credit path writes, so a stored
#: marker under one of them is local proof that the PaymentIntent was credited.
CREDITING_EVENT_TYPES: tuple[str, ...] = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "payment_intent.succeeded",
)


@dataclass(frozen=True, slots=True)
class StripeTrustScan:
    payments: tuple[TrustEvent, ...]
    adverse: tuple[AdverseTrustEvent, ...]
    source_events: tuple[TrustEvent, ...]
    outstanding: tuple[OutstandingAdverse, ...]
    unmatched_ids: tuple[str, ...]
    #: PaymentIntent id -> candidate ``stripe_event`` marker ids that would prove
    #: the payment was credited locally. Empty means no derivable evidence: the
    #: writer must then refuse the payment fact (decision 76, P1-B).
    credit_evidence: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


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


#: Every Stripe Event type the live webhook applies as a refund fact
#: (``routes/internal/webhook.py`` ``_stripe_adverse_events``). ``charge.refunded``
#: carries a Charge whose ``refunds.data[]`` holds the refund objects.
REFUND_EVENT_TYPES: tuple[str, ...] = (
    "charge.refunded",
    "charge.refund.updated",
    "refund.created",
    "refund.updated",
    "refund.failed",
)
#: Every Stripe Event type the live webhook applies as a dispute fact.
DISPUTE_EVENT_TYPES: tuple[str, ...] = (
    "charge.dispute.created",
    "charge.dispute.updated",
    "charge.dispute.closed",
    "charge.dispute.funds_withdrawn",
    "charge.dispute.funds_reinstated",
)


@dataclass(frozen=True, slots=True)
class AdverseSourceEvent:
    """The Stripe Event whose stamps the live handler left on one refund/dispute."""

    created: int
    event_id: str
    event_type: str

    @property
    def watermark(self) -> str:
        return f"{self.created:020d}:{self.event_id}"


def adverse_lifecycle_status(obj: Mapping[str, Any], *, kind: str) -> str:
    """Lifecycle status the live handler records for a Refund/Dispute object."""

    return (
        _refund_status(obj.get("status"))
        if kind == "refund"
        else _dispute_status(obj.get("status"))
    )


def event_adverse_status(
    event: Mapping[str, Any], *, kind: str, adverse_ref: str
) -> str | None:
    """Status the live webhook would record from this Event for ``adverse_ref``.

    ``None`` when the Event is not about that refund/dispute. Mirrors
    ``_stripe_adverse_events`` byte for byte: ``charge.refunded`` matches the
    refund inside ``data.object.refunds.data[]``; every other refund type
    carries the Refund as ``data.object``; ``charge.dispute.funds_reinstated``
    maps to ``won`` whatever the object says.
    """

    event_type = str(event.get("type") or "")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, Mapping) else None
    if not isinstance(obj, Mapping):
        return None
    if kind == "refund":
        if event_type == "charge.refunded":
            refunds = obj.get("refunds")
            rows = refunds.get("data") if isinstance(refunds, Mapping) else None
            for row in rows if isinstance(rows, list) else ():
                if isinstance(row, Mapping) and str(row.get("id") or "") == adverse_ref:
                    return _refund_status(row.get("status"))
            return None
        if event_type in REFUND_EVENT_TYPES and str(obj.get("id") or "") == adverse_ref:
            return _refund_status(obj.get("status"))
        return None
    if event_type in DISPUTE_EVENT_TYPES and str(obj.get("id") or "") == adverse_ref:
        if event_type == "charge.dispute.funds_reinstated":
            return "won"
        return _dispute_status(obj.get("status"))
    return None


def latest_adverse_event(
    stripe_client: Any,
    *,
    kind: str,
    adverse_ref: str,
    occurred_at: datetime,
    lifecycle_status: str | None = None,
) -> AdverseSourceEvent | None:
    """Return the Stripe Event whose stamps the live writer holds for an adverse object.

    The live webhook stamps adverse facts from the Event, not the object:
    ``occurred_at=Event.created``, ``provider_subtype=Event.type`` and
    ``provider_ordering_watermark=f"{Event.created:020d}:{Event.id}"``. The
    converter needs the same Event to reproduce those bytes (P1-C).

    The writer converges those stamps to the greatest watermark among the
    Events that carried the object's *current* lifecycle status
    (``adverse_restamp_wins``), so with ``lifecycle_status`` given this
    considers only Events whose mapped status equals it -- every type the live
    handler applies, including ``charge.refunded`` and ``charge.refund.updated``
    -- and picks the max ``(created, id)`` among them. Without a status it
    falls back to the latest Event of any status.
    """

    event_resource = getattr(stripe_client, "Event", None)
    if event_resource is None:
        return None
    event_types = REFUND_EVENT_TYPES if kind == "refund" else DISPUTE_EVENT_TYPES
    latest: AdverseSourceEvent | None = None
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
            status = event_adverse_status(event, kind=kind, adverse_ref=adverse_ref)
            if status is None or (lifecycle_status is not None and status != lifecycle_status):
                continue
            candidate = AdverseSourceEvent(
                int(event.get("created") or 0),
                str(event.get("id") or ""),
                str(event.get("type") or event_type),
            )
            if latest is None or (candidate.created, candidate.event_id) > (
                latest.created,
                latest.event_id,
            ):
                latest = candidate
    return latest


def latest_adverse_event_watermark(
    stripe_client: Any,
    *,
    kind: str,
    adverse_ref: str,
    occurred_at: datetime,
    lifecycle_status: str | None = None,
) -> str | None:
    """Return the ordering key of the Event ``latest_adverse_event`` picks, if any."""

    latest = latest_adverse_event(
        stripe_client,
        kind=kind,
        adverse_ref=adverse_ref,
        occurred_at=occurred_at,
        lifecycle_status=lifecycle_status,
    )
    return None if latest is None else latest.watermark


def stamp_adverse_source_event(row: dict[str, Any], event: AdverseSourceEvent | None) -> None:
    """Attach the Event-derived fields the converter prefers over the object's."""

    if event is None:
        return
    row["_trust_ordering_watermark"] = event.watermark
    row["_trust_event_created"] = event.created
    row["_trust_event_type"] = event.event_type


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
            stamp_adverse_source_event(
                row,
                latest_adverse_event(
                    stripe_client,
                    kind=kind,
                    adverse_ref=adverse_ref,
                    occurred_at=datetime.fromtimestamp(int(row["created"]), tz=UTC),
                    lifecycle_status=adverse_lifecycle_status(row, kind=kind),
                ),
            )
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


def _payment_event(
    payment_intent: dict[str, Any],
    *,
    recorded_at: datetime,
    checkout_session: Mapping[str, Any] | None = None,
) -> TrustEvent:
    """Emit exactly the payment fact the live credit path writes.

    Parity is load-bearing (P1-C): ``CanonicalTrustRecord.digest()`` hashes
    ``occurred_at`` and ``provider_ordering_watermark``, so any shape drift
    between this converter and ``credit_workspace_typed_direct`` keeps
    ``semantic_mismatch_count`` above zero and the marker never completes.

    * The live path stamps payment facts with ``provider_ordering_watermark=NULL``
      (``payment_or_grant_event``); so does this converter.
    * A Checkout payment's ``occurred_at`` is ``Session.created`` on the live
      side (webhook.py); the converter therefore needs the Checkout Session and
      falls back to ``PaymentIntent.created`` only when none is resolvable --
      which the reconciliation then reports as a semantic mismatch rather than
      papering over.
    * Auto-refill and x402 credits stamp ``PaymentIntent.created`` on both sides.
    """

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
    occurred_raw: Any = payment_intent.get("created")
    if subtype == "checkout" and checkout_session is not None and checkout_session.get("created"):
        occurred_raw = checkout_session["created"]
    return payment_or_grant_event(
        workspace_id,
        f"trust-backfill:{provider}:payment:{payment_intent_id}",
        credited,
        CreditProvenance(
            source=subtype,
            provider=provider,
            external_ref=payment_intent_id,
            occurred_at=_timestamp(occurred_raw),
        ),
        recorded_at=recorded_at,
        payment_amount_microdollars=amount_micro,
        currency=str(payment_intent.get("currency") or "usd"),
    )


def credit_evidence_ids(
    payment_intent: Mapping[str, Any],
    *,
    checkout_session: Mapping[str, Any] | None = None,
    crediting_event_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Candidate ``stripe_event`` marker ids proving this payment was credited.

    The live credit paths key their idempotency marker as follows, and only
    these keys count as local credit evidence:

    * x402: ``x402_event_id(pi)`` (services/x402_billing.py);
    * ACH Checkout: ``stripe_checkout:<payment_intent or session id>``
      (``_checkout_credit_event_id``);
    * card Checkout and auto-refill: the Stripe Event id of the crediting
      webhook, which is only knowable from Stripe's Events API (30-day
      retention) or an operator-attested list (``--credited-events``).
    """

    payment_intent_id = str(payment_intent.get("id") or "")
    metadata_value = payment_intent.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    candidates: list[str] = []
    if _provider_for_payment(payment_intent) == "x402":
        candidates.append(x402_event_id(payment_intent_id))
    elif metadata.get("auto_refill") != "true":
        payment_method = str(metadata.get("payment_method") or "stripe")
        session: dict[str, Any] = dict(checkout_session or {})
        session.setdefault("payment_intent", payment_intent_id)
        derived = _checkout_credit_event_id(
            event_id="", checkout_session=session, payment_method=payment_method
        )
        if derived:
            candidates.append(derived)
        session_id = str(session.get("id") or "")
        if payment_method == "ach" and session_id:
            candidates.append(f"stripe_checkout:{session_id}")
    for event_id in crediting_event_ids:
        if event_id and event_id not in candidates:
            candidates.append(str(event_id))
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


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
    checkout_sessions: Mapping[str, Any] | None = None,
    crediting_events: Mapping[str, Iterable[str]] | None = None,
) -> StripeTrustScan:
    """Convert recorded list/retrieve responses without performing I/O.

    ``checkout_sessions`` maps PaymentIntent id -> Checkout Session object (for
    ``occurred_at`` parity and ACH evidence); ``crediting_events`` maps
    PaymentIntent id -> Stripe Event ids whose processing credited it.
    """

    sessions = {
        key: _mapping(value) for key, value in (checkout_sessions or {}).items()
    }
    crediting = {key: tuple(value) for key, value in (crediting_events or {}).items()}
    payment_objects = [_mapping(row) for row in payment_intents]
    all_payment_objects = [*payment_objects, *(_mapping(row) for row in known_payment_intents)]
    payment_by_id = {
        str(row.get("id") or ""): row
        for row in all_payment_objects
        if str(row.get("id") or "")
    }
    payments: list[TrustEvent] = []
    credit_evidence: dict[str, tuple[str, ...]] = {}

    def append_payment(row: dict[str, Any]) -> None:
        payment_intent_id = str(row.get("id") or "")
        session = sessions.get(payment_intent_id)
        payments.append(
            _payment_event(row, recorded_at=recorded_at, checkout_session=session)
        )
        credit_evidence[payment_intent_id] = credit_evidence_ids(
            row,
            checkout_session=session,
            crediting_event_ids=crediting.get(payment_intent_id, ()),
        )

    for row in payment_objects:
        metadata = row.get("metadata")
        if (
            row.get("status") == "succeeded"
            and isinstance(metadata, Mapping)
            and metadata.get("workspace_id")
        ):
            append_payment(row)

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
            append_payment(stored_payment)

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
        # Live adverse facts are stamped from the Stripe Event that delivered
        # them (webhook.py `_stripe_adverse_events`): occurred_at=Event.created,
        # provider_subtype=Event.type, watermark from the Event. Prefer the same
        # Event here; the object's own fields are the fallback when no Event is
        # resolvable (older than Stripe's retention), and the reconciliation
        # then reports the difference rather than hiding it.
        occurred_at = _timestamp(obj.get("_trust_event_created") or obj.get("created"))
        event_type = obj.get("_trust_event_type")
        subtype = str(event_type) if isinstance(event_type, str) and event_type else str(
            obj.get("object") or kind
        )
        event = AdverseTrustEvent(
            event_id=f"trust-backfill:{payment.provider}:{kind}:{adverse_ref}",
            provider=payment.provider,
            kind=kind,
            adverse_ref=adverse_ref,
            original_payment_ref=payment_ref,
            amount_micro=int(obj.get("amount") or 0) * MICRODOLLARS_PER_CENT,
            provider_subtype=subtype,
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
        credit_evidence=credit_evidence,
    )


def _is_checkout_payment(payment_intent: Mapping[str, Any]) -> bool:
    metadata_value = payment_intent.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    return (
        str(payment_intent.get("status") or "") == "succeeded"
        and bool(metadata.get("workspace_id"))
        and _provider_for_payment(payment_intent) == "stripe"
        and metadata.get("auto_refill") != "true"
    )


def resolve_checkout_sessions(
    stripe_client: Any, payment_intents: Iterable[Any]
) -> dict[str, dict[str, Any]]:
    """Checkout Session per qualifying PaymentIntent, when the client can list them.

    One ``checkout.Session.list(payment_intent=...)`` per Checkout payment. The
    live handler stamps ``Session.created`` as ``occurred_at``; without the
    session the converter cannot reproduce that byte and the marker stays open.
    A client without a ``checkout`` resource (recorded-response fixtures) yields
    no sessions and the converter falls back to ``PaymentIntent.created``.
    """

    checkout = getattr(stripe_client, "checkout", None)
    session_resource = getattr(checkout, "Session", None) if checkout is not None else None
    if session_resource is None:
        return {}
    sessions: dict[str, dict[str, Any]] = {}
    for raw in payment_intents:
        payment_intent = _mapping(raw)
        payment_intent_id = str(payment_intent.get("id") or "")
        if not payment_intent_id or not _is_checkout_payment(payment_intent):
            continue
        rows = _rows(session_resource.list(payment_intent=payment_intent_id, limit=1))
        if rows:
            sessions[payment_intent_id] = _mapping(rows[0])
    return sessions


def list_crediting_events(
    stripe_client: Any, *, start: datetime
) -> dict[str, tuple[str, ...]]:
    """PaymentIntent id -> ids of the Stripe Events whose processing credits it.

    Stripe retains Events for thirty days, so this resolves evidence only for
    recent payments; older ones need an operator-attested list. No upper bound
    on ``created``: a webhook that Stripe retried for hours (the 2026-09-04
    outage) is credited by an Event created long after the PaymentIntent.
    """

    event_resource = getattr(stripe_client, "Event", None)
    if event_resource is None:
        return {}
    crediting: dict[str, list[str]] = {}
    for event_type in CREDITING_EVENT_TYPES:
        for raw in _rows(
            event_resource.list(
                type=event_type,
                created={"gte": int(start.timestamp())},
                limit=100,
            )
        ):
            event = _mapping(raw)
            event_id = str(event.get("id") or "")
            data = event.get("data")
            obj = data.get("object") if isinstance(data, Mapping) else None
            if not event_id or not isinstance(obj, Mapping):
                continue
            payment_intent_id = (
                str(obj.get("id") or "")
                if event_type == "payment_intent.succeeded"
                else _object_id(obj.get("payment_intent"))
            )
            if payment_intent_id:
                crediting.setdefault(payment_intent_id, []).append(event_id)
    return {key: tuple(dict.fromkeys(value)) for key, value in crediting.items()}


def scan_created_range(
    stripe_client: Any,
    *,
    start: datetime,
    end: datetime,
    recorded_at: datetime,
    include_event_watermarks: bool = True,
    credited_events: Mapping[str, Iterable[str]] | None = None,
) -> StripeTrustScan:
    """List all trust object kinds by created range, resolving stored payments.

    ``include_event_watermarks`` (default on) resolves each refund/dispute's
    latest Stripe Event so adverse facts carry the Event-based occurred_at,
    subtype and watermark the live handler writes. ``credited_events`` is the
    operator-attested PaymentIntent -> Stripe Event id map for payments older
    than Stripe's Events retention; it is merged with whatever the Events API
    still returns.
    """

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
    crediting: dict[str, tuple[str, ...]] = dict(
        list_crediting_events(stripe_client, start=start)
    )
    for payment_intent_id, event_ids in (credited_events or {}).items():
        merged = [*crediting.get(payment_intent_id, ()), *event_ids]
        crediting[payment_intent_id] = tuple(dict.fromkeys(merged))
    return scan_stripe_responses(
        payment_intents=payment_intents,
        refunds=refunds,
        disputes=disputes,
        known_payment_intents=known,
        recorded_at=recorded_at,
        checkout_sessions=resolve_checkout_sessions(stripe_client, [*payment_intents, *known]),
        crediting_events=crediting,
    )
