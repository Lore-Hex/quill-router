"""Fraud reports TrustedRouter sends to Stripe Radar.

When an operator confirms that a workspace paid with stolen cards, Stripe
should learn it too. Reporting a charge as fraudulent does two things in one
call: Radar trains on the report, and Stripe adds the charge's card
fingerprint and email to the account's default block lists, so the same card
is declined when it returns on a new account. No money moves; refunds remain a
separate decision.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import stripe

from trusted_router.config import Settings

log = logging.getLogger(__name__)


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
