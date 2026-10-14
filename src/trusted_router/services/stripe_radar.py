"""What TrustedRouter tells Stripe Radar about its accounts.

Two signals. Every checkout carries account facts as metadata, which the
payment's dashboard page shows and Radar rules can read. And when an operator
confirms that a workspace paid with stolen cards, Stripe learns it too:
reporting a charge as fraudulent does two things in one call, Radar trains on
the report, and Stripe adds the charge's card fingerprint and email to the
account's default block lists, so the same card is declined when it returns on
a new account. No money moves; refunds remain a separate decision.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import stripe

from trusted_router.config import Settings
from trusted_router.storage import STORE
from trusted_router.storage_models import User, Workspace

log = logging.getLogger(__name__)


def account_signals(
    *,
    workspace: Workspace,
    user: User | None,
    prior_topup_microdollars: int | None,
    now: datetime,
) -> dict[str, str]:
    """Account facts sent with a checkout as Stripe metadata.

    Radar's own model does not read metadata. Radar for Fraud Teams rules can,
    for example ``Review if ::tr_user_age_minutes:: < 60``, and the dashboard
    shows the facts on the payment page to whoever reviews it. Stripe metadata
    values are strings; ages are whole minutes. A fact that could not be read
    is left out, which a Radar rule treats as missing rather than as zero.

    Auto-refill does not send them: its retries reuse one idempotency key,
    and Stripe refuses a retry whose parameters changed, as ages do.
    """
    signals: dict[str, str] = {}
    _put_age(signals, "tr_workspace_age_minutes", workspace.created_at, now)
    if user is not None:
        _put_age(signals, "tr_user_age_minutes", user.created_at, now)
        signals["tr_email_verified"] = _flag(user.email_verified)
        signals["tr_phone_verified"] = _flag(user.phone_verified)
        signals["tr_identity_verified"] = _flag(user.identity_verified)
        signals["tr_owned_workspaces"] = str(user.owner_workspace_count)
    if prior_topup_microdollars is not None:
        signals["tr_prior_topup_cents"] = str(prior_topup_microdollars // 10_000)
    return signals


def payment_account_signals(workspace: Workspace, user_id: str | None) -> dict[str, str]:
    """Read the payer's account facts. A failed read never stops a payment."""
    user: User | None = None
    prior_topup: int | None = None
    if user_id:
        try:
            user = STORE.get_user(user_id)
            # Strong read: a top-up seconds earlier must count as prior funding.
            prior_topup = STORE.get_lifetime_topup_microdollars(user_id)
        except Exception:  # noqa: BLE001 - signals are advisory; the payment goes on.
            log.warning(
                "stripe_radar.account_signals_partial workspace=%s",
                workspace.id,
                exc_info=True,
            )
    return account_signals(
        workspace=workspace,
        user=user,
        prior_topup_microdollars=prior_topup,
        now=datetime.now(UTC),
    )


def _put_age(signals: dict[str, str], key: str, created_at: str, now: datetime) -> None:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    signals[key] = str(max(0, int((now - created).total_seconds() // 60)))


def _flag(value: bool) -> str:
    return "true" if value else "false"


def report_workspace_card_fraud(*, workspace_id: str, settings: Settings) -> dict[str, Any]:
    """Report every succeeded card charge of ``workspace_id`` as fraudulent.

    Payments are found the way ``list_workspace_payments`` finds them: the
    PaymentIntent search on ``metadata['workspace_id']``, which checkout and
    auto-refill both stamp. Search can trail a new payment by about a minute.
    Refunded charges are still ``succeeded`` and are reported too. Calling
    again is safe: charges Stripe already marks fraudulent are skipped, so a
    retry completes only what failed before.
    """
    if not settings.stripe_secret_key:
        return {"status": "unavailable", "reason": "this service has no Stripe key"}
    stripe.api_key = settings.stripe_secret_key
    reported: list[str] = []
    already_reported: list[str] = []
    failed: list[str] = []
    try:
        page = stripe.PaymentIntent.search(
            query=f"metadata['workspace_id']:'{workspace_id}'",
            limit=100,
            expand=["data.latest_charge"],
        )
        intents = [_as_dict(intent) for intent in page.auto_paging_iter()]
    except Exception as exc:  # noqa: BLE001 - the operator sees the failure and retries.
        log.warning(
            "stripe_radar.search_failed workspace=%s error=%s",
            workspace_id,
            type(exc).__name__,
        )
        return {
            "status": "incomplete",
            "error": f"payment search failed: {type(exc).__name__}",
            "reported": reported,
            "already_reported": already_reported,
            "failed": failed,
        }
    for intent in intents:
        charge = intent.get("latest_charge")
        if not isinstance(charge, Mapping) or charge.get("status") != "succeeded":
            continue
        details = charge.get("payment_method_details")
        if not isinstance(details, Mapping) or details.get("type") != "card":
            continue
        charge_id = str(charge.get("id") or "")
        fraud_details = charge.get("fraud_details")
        if isinstance(fraud_details, Mapping) and fraud_details.get("user_report") == "fraudulent":
            already_reported.append(charge_id)
            continue
        try:
            stripe.Charge.modify(charge_id, fraud_details={"user_report": "fraudulent"})
        except Exception as exc:  # noqa: BLE001 - one bad charge must not hide the rest.
            log.warning(
                "stripe_radar.report_failed workspace=%s charge=%s error=%s",
                workspace_id,
                charge_id,
                type(exc).__name__,
            )
            failed.append(charge_id)
            continue
        reported.append(charge_id)
    log.info(
        "stripe_radar.reported workspace=%s reported=%d already=%d failed=%d",
        workspace_id,
        len(reported),
        len(already_reported),
        len(failed),
    )
    return {
        "status": "incomplete" if failed else "complete",
        "reported": reported,
        "already_reported": already_reported,
        "failed": failed,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    recursive = getattr(value, "_to_dict_recursive", None)
    if callable(recursive):
        converted = recursive()
        if isinstance(converted, dict):
            return converted
    raise TypeError(f"unsupported Stripe object {type(value).__name__}")
