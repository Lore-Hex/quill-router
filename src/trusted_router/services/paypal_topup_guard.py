"""Stricter checks on PayPal top-ups, against spend-then-reverse fraud.

A PayPal buyer can reverse a payment (unauthorised-transaction claim) or open
a dispute weeks after the capture, long after the credit was spent on
inference. Recovery then finds an empty balance and the principal is lost.
Stripe has Radar in front of card checkouts; PayPal has nothing on our side,
so this module is that layer.

Credit only becomes spendable when TrustedRouter captures an approved order
and PayPal reports the capture COMPLETED (``paypal_billing``: a PENDING
capture, eCheck included, is never credited until the COMPLETED webhook).
The capture is ours to make, so refusing before it is clean: the buyer is
never charged and the approved order simply expires. Every check here runs
either when the order is created or right before we capture it, never after
money moved.

Enforced rules (``ENFORCED_RULES``), refused in ``enforce`` mode:

* ``workspace_paused``: the workspace carries the operator abuse latch or a
  principal-recovery debt (a reversal it could not cover). Applies to every
  workspace, established or not.
* ``account_restricted``: the owner or paying user is suspended or disabled.
* ``new_github_account``: a non-established workspace whose owner signed up
  with a GitHub account under a day old (the tripwire signal).
* ``disposable_email``: a non-established workspace whose owner or paying
  user is on a disposable email domain (the signup gate's list; accounts
  created before that gate still exist).
* ``new_workspace_amount_cap``: a non-established workspace asks for more
  than ``paypal_new_workspace_max_topup_cents`` in one PayPal top-up.
* ``new_workspace_daily_captures`` / ``new_owner_daily_captures``: more
  PayPal captures in a rolling day than the per-workspace / per-owner limit.
* ``paypal_first_topup``: a non-established workspace with no completed
  top-up of any kind yet (lifetime top-ups of the owner and the paying user
  are both zero). The first top-up has to go through card checkout, behind
  Stripe Radar. The console shows a line pointing to card checkout.
* ``phone_unverified``: the owner has not verified a phone number.
* ``impossible_email``: the owner, paying user or PayPal payer email has a
  typo TLD or a misspelled big-provider domain (``services/email_typos``).
* ``paypal_payer_unverified``: PayPal reports the payer account UNVERIFIED
  (``payment_source.paypal.account_status`` on the approved order).
* ``payer_email_mismatch``: the PayPal payer email is neither the owner's
  nor the paying user's account email.

The three payer rules need the approved order, which only exists once the
buyer has come back from PayPal; they run at capture, on a GET of the order
made before TrustedRouter captures it. Every other rule runs at checkout
too, so most refusals happen before an order exists. Each of the five rules
above has its own on/off setting (``paypal_require_*`` /
``paypal_reject_impossible_email``); off means not evaluated (an unverified
phone is then still logged, as before). A rule named in
``paypal_topup_guard_log_only_rules`` is evaluated and logged, not enforced.

Logged-only signals: ``seller_protection_not_eligible`` and
``paypal_payer_status_unknown`` (the order carried no account status, so the
payer rule could not judge it; never refused on). PayPal Seller Protection
does not cover intangible goods, so most of our captures are expected to be
NOT_ELIGIBLE; the status is logged to measure that rather than to gate on it.

Established workspaces skip every rule except the two account-state rules:
the owner is identity-verified, or the owner account is at least
``paypal_established_min_owner_age_days`` old and has topped up at least
``paypal_established_min_lifetime_topup_cents`` in total.

Separately, ``latch_after_paypal_adverse`` latches a non-established
workspace when PayPal reports a dispute or reversal on one of its captures,
through the same transaction the operator abuse endpoint uses, so the
operator clears it the same way (``action: clear``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from fastapi import HTTPException

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.abuse_tripwires import github_account_was_new, parse_time
from trusted_router.services.email_typos import is_impossible_email
from trusted_router.services.paused_topup import CHECKOUT_GUARD_BILLING_PAUSED
from trusted_router.signup_gate import is_disposable_email
from trusted_router.storage_models import AdverseTrustEvent, AdverseTrustResult, User
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

RULE_WORKSPACE_PAUSED = "workspace_paused"
RULE_ACCOUNT_RESTRICTED = "account_restricted"
RULE_NEW_GITHUB_ACCOUNT = "new_github_account"
RULE_DISPOSABLE_EMAIL = "disposable_email"
RULE_AMOUNT_CAP = "new_workspace_amount_cap"
RULE_WORKSPACE_VELOCITY = "new_workspace_daily_captures"
RULE_OWNER_VELOCITY = "new_owner_daily_captures"
RULE_FIRST_TOPUP = "paypal_first_topup"
RULE_PHONE_UNVERIFIED = "phone_unverified"
RULE_IMPOSSIBLE_EMAIL = "impossible_email"
RULE_PAYER_UNVERIFIED = "paypal_payer_unverified"
RULE_PAYER_MISMATCH = "payer_email_mismatch"
# Earlier names, from when these two were logged-only signals.
SIGNAL_PHONE_UNVERIFIED = RULE_PHONE_UNVERIFIED
SIGNAL_PAYER_MISMATCH = RULE_PAYER_MISMATCH
SIGNAL_SELLER_PROTECTION = "seller_protection_not_eligible"
SIGNAL_PAYER_STATUS_UNKNOWN = "paypal_payer_status_unknown"

ENFORCED_RULES = frozenset({
    RULE_WORKSPACE_PAUSED,
    RULE_ACCOUNT_RESTRICTED,
    RULE_NEW_GITHUB_ACCOUNT,
    RULE_DISPOSABLE_EMAIL,
    RULE_AMOUNT_CAP,
    RULE_WORKSPACE_VELOCITY,
    RULE_OWNER_VELOCITY,
    RULE_FIRST_TOPUP,
    RULE_PHONE_UNVERIFIED,
    RULE_IMPOSSIBLE_EMAIL,
    RULE_PAYER_UNVERIFIED,
    RULE_PAYER_MISMATCH,
})
# The account is shut for PayPal whatever rail paid first: with one of these
# present the "use a card first" line would mislead, so it is not shown.
_ACCOUNT_STATE_RULES = frozenset({RULE_WORKSPACE_PAUSED, RULE_ACCOUNT_RESTRICTED})

# Pause causes that mean "do not take another PayPal payment from here".
# Migration/resharding/federation pauses are operational, not about the payer.
_PAYPAL_BLOCKING_PAUSE_CAUSES = frozenset({"abuse", "principal_recovery"})
_VELOCITY_WINDOW_SECONDS = 86_400
_WORKSPACE_VELOCITY_NAMESPACE = "paypal_capture_workspace"
_OWNER_VELOCITY_NAMESPACE = "paypal_capture_owner"

PAYPAL_UNAVAILABLE_MESSAGE = (
    "PayPal is not available for this account right now. Add credits with a card instead."
)
PAYPAL_FIRST_TOPUP_MESSAGE = (
    "Your first top-up needs to be made with a card. "
    "PayPal is available once a card top-up has completed."
)
PAYPAL_DAILY_LIMIT_MESSAGE = (
    "This account has reached today's PayPal top-up limit. "
    "Try again tomorrow or add credits with a card."
)

# Machine-readable refusal codes, so the console can show the right line.
GUARD_CODE_AMOUNT_CAP = "paypal_amount_cap"
GUARD_CODE_DAILY_LIMIT = "paypal_daily_limit"
GUARD_CODE_UNAVAILABLE = "paypal_unavailable"
GUARD_CODE_FIRST_TOPUP = "paypal_first_topup"
GUARD_CODES = frozenset({
    GUARD_CODE_AMOUNT_CAP, GUARD_CODE_DAILY_LIMIT, GUARD_CODE_UNAVAILABLE, GUARD_CODE_FIRST_TOPUP,
})

ADVERSE_LATCH_OPERATOR = "system:paypal-adverse"
ADVERSE_ALERT_FINGERPRINT = ["paypal-topup-guard", "adverse-latch"]
ADVERSE_LATCH_LIFECYCLES = frozenset({"succeeded", "lost"})


def paypal_amount_cap_message(cap_cents: int) -> str:
    return (
        f"New accounts can add up to ${cap_cents // 100:,} per PayPal payment. "
        "Choose a smaller amount or add credits with a card."
    )


@dataclasses.dataclass(frozen=True)
class GuardSubject:
    workspace_id: str
    pause_causes: frozenset[str]
    owner: User | None
    payer: User | None
    established: bool
    # Whether the owner or the paying user has any completed top-up. Only
    # read for a non-established workspace with the first-top-up rule on;
    # True otherwise, so the rule cannot fire.
    has_prior_topup: bool = True

    def users(self) -> tuple[User, ...]:
        seen: dict[str, User] = {}
        for user in (self.owner, self.payer):
            if user is not None:
                seen.setdefault(user.id, user)
        return tuple(seen.values())


def owner_is_established(store: Any, owner: User | None, settings: Settings, now: dt.datetime) -> bool:
    """Identity-verified, or old enough with enough paid top-ups.

    Unknown or unreadable means not established: the cost of a wrong "no" is
    a cap a real customer can step around with a card; a wrong "yes" is an
    uncapped reversible payment.
    """
    if owner is None:
        return False
    if owner.identity_status == "approved":
        return True
    created = parse_time(owner.created_at)
    min_age = dt.timedelta(days=settings.paypal_established_min_owner_age_days)
    if created is None or now - created < min_age:
        return False
    try:
        lifetime = int(store.get_lifetime_topup_microdollars(owner.id, allow_stale=True))
    except Exception:  # noqa: BLE001 - fail to "not established", never fail open
        log.warning("paypal_topup_guard.lifetime_topup_read_failed", exc_info=True)
        return False
    return lifetime >= settings.paypal_established_min_lifetime_topup_cents * 10_000


def has_prior_topup(store: Any, users: Iterable[User]) -> bool:
    """Whether any of these users has a completed top-up of any kind.

    Every rail that credits a payment (Stripe card/ACH, PayPal, Adyen, x402)
    adds it to the paying user's lifetime top-up total in the same
    transaction, so a non-zero total means a completed, credited top-up. A
    signup trial credit is not a top-up and is not counted. Unreadable
    counts as none: the buyer is sent to card checkout, never let through.
    """
    try:
        return any(
            int(store.get_lifetime_topup_microdollars(user.id, allow_stale=True)) > 0
            for user in users
        )
    except Exception:  # noqa: BLE001 - fail to "no prior top-up", never fail open
        log.warning("paypal_topup_guard.prior_topup_read_failed", exc_info=True)
        return False


def load_subject(
    store: Any,
    workspace_id: str,
    *,
    payer_user_id: str | None,
    settings: Settings,
    now: dt.datetime,
) -> GuardSubject:
    """Primary-key reads only: the workspace, its owner, the paying user."""
    workspace = store.get_workspace(workspace_id)
    owner = (
        store.get_user(workspace.owner_user_id)
        if workspace is not None and workspace.owner_user_id
        else None
    )
    payer = owner
    if payer_user_id and (owner is None or payer_user_id != owner.id):
        payer = store.get_user(payer_user_id)
    established = owner_is_established(store, owner, settings, now)
    prior = True
    if not established and settings.paypal_require_prior_topup:
        prior = has_prior_topup(store, (user for user in (owner, payer) if user is not None))
    return GuardSubject(
        workspace_id=workspace_id,
        pause_causes=frozenset(getattr(workspace, "billing_pause_causes", None) or ()),
        owner=owner,
        payer=payer,
        established=established,
        has_prior_topup=prior,
    )


def account_findings(subject: GuardSubject, settings: Settings) -> list[str]:
    """Rules that read only stored account state (no counters)."""
    findings: list[str] = []
    if subject.pause_causes & _PAYPAL_BLOCKING_PAUSE_CAUSES:
        findings.append(RULE_WORKSPACE_PAUSED)
    if any(user.suspended or user.disabled for user in subject.users()):
        findings.append(RULE_ACCOUNT_RESTRICTED)
    if subject.established:
        return findings
    owner = subject.owner
    if owner is not None and github_account_was_new(owner.github_account_created_at, owner.created_at):
        findings.append(RULE_NEW_GITHUB_ACCOUNT)
    if any(is_disposable_email(user.email) for user in subject.users()):
        findings.append(RULE_DISPOSABLE_EMAIL)
    if settings.paypal_reject_impossible_email and any(
        user.email and is_impossible_email(user.email) for user in subject.users()
    ):
        findings.append(RULE_IMPOSSIBLE_EMAIL)
    # With the switch off this is still logged, as it was before it enforced.
    if owner is not None and not owner.phone_verified:
        findings.append(RULE_PHONE_UNVERIFIED)
    if settings.paypal_require_prior_topup and not subject.has_prior_topup:
        findings.append(RULE_FIRST_TOPUP)
    return findings


@dataclasses.dataclass(frozen=True)
class PayPalPayerFacts:
    """What the approved PayPal order says about the buyer, read before capture.

    ``email`` is compared in memory and never logged or stored.
    """
    email: str | None
    # payment_source.paypal.account_status: "VERIFIED", "UNVERIFIED", or None
    # when PayPal did not report it.
    account_status: str | None
    # The TrustedRouter user who started the checkout (the order's custom_id).
    initiating_user_id: str | None = None


def _payer_rules_on(settings: Settings) -> bool:
    return (
        settings.paypal_require_verified_payer
        or settings.paypal_require_payer_email_match
        or settings.paypal_reject_impossible_email
    )


def _payer_rules_enforced(settings: Settings) -> bool:
    if settings.paypal_topup_guard_mode != "enforce":
        return False
    demoted = settings.paypal_topup_guard_log_only_rule_set
    active = {
        RULE_PAYER_UNVERIFIED: settings.paypal_require_verified_payer,
        RULE_PAYER_MISMATCH: settings.paypal_require_payer_email_match,
        RULE_IMPOSSIBLE_EMAIL: settings.paypal_reject_impossible_email,
    }
    return any(on and rule not in demoted for rule, on in active.items())


def payer_findings(
    store: Any,
    subject: GuardSubject,
    facts: PayPalPayerFacts,
    settings: Settings,
) -> list[str]:
    """Rules on the PayPal buyer of an approved, not yet captured order."""
    findings: list[str] = []
    status = (facts.account_status or "").strip().upper()
    if settings.paypal_require_verified_payer:
        if status == "UNVERIFIED":
            findings.append(RULE_PAYER_UNVERIFIED)
        elif status != "VERIFIED":
            findings.append(SIGNAL_PAYER_STATUS_UNKNOWN)
    payer_email = facts.email.strip().lower() if facts.email else None
    if settings.paypal_reject_impossible_email and payer_email and is_impossible_email(payer_email):
        findings.append(RULE_IMPOSSIBLE_EMAIL)
    if settings.paypal_require_payer_email_match:
        users = list(subject.users())
        if facts.initiating_user_id and all(u.id != facts.initiating_user_id for u in users):
            initiator = store.get_user(facts.initiating_user_id)
            if initiator is not None:
                users.append(initiator)
        account_emails = {u.email.strip().lower() for u in users if u.email}
        # No payer email on the order cannot be shown to match: refuse.
        if payer_email is None or payer_email not in account_emails:
            findings.append(RULE_PAYER_MISMATCH)
    return findings


def _switched_off(settings: Settings) -> frozenset[str]:
    """Rules whose own setting is off. Only the phone rule still produces a
    finding when off (logged, as before it enforced); this keeps it from
    being enforced."""
    return frozenset() if settings.paypal_require_phone_verified else frozenset({RULE_PHONE_UNVERIFIED})


def _enforced(findings: Iterable[str], settings: Settings) -> list[str]:
    if settings.paypal_topup_guard_mode != "enforce":
        return []
    demoted = settings.paypal_topup_guard_log_only_rule_set | _switched_off(settings)
    return list(dict.fromkeys(
        rule for rule in findings if rule in ENFORCED_RULES and rule not in demoted
    ))


def _report(stage: str, subject: GuardSubject, findings: Sequence[str], enforced: Sequence[str]) -> None:
    if not findings:
        return
    log.log(
        logging.WARNING if enforced else logging.INFO,
        "paypal_topup_guard.%s",
        "refused" if enforced else "observed",
        extra={
            "stage": stage,
            "workspace_id": subject.workspace_id,
            "established": subject.established,
            "rules": ",".join(findings),
            "enforced_rules": ",".join(enforced),
        },
    )


def _refusal(enforced: Sequence[str], settings: Settings) -> Exception:
    if RULE_FIRST_TOPUP in enforced and not set(enforced) & _ACCOUNT_STATE_RULES:
        # The one rule the buyer is told about by design: it names the fix.
        return api_error(
            403,
            PAYPAL_FIRST_TOPUP_MESSAGE,
            ErrorType.FORBIDDEN,
            extra={"checkout_guard": GUARD_CODE_FIRST_TOPUP},
        )
    if enforced == [RULE_AMOUNT_CAP]:
        return api_error(
            403,
            paypal_amount_cap_message(settings.paypal_new_workspace_max_topup_cents),
            ErrorType.FORBIDDEN,
            extra={"checkout_guard": GUARD_CODE_AMOUNT_CAP},
        )
    if set(enforced) <= {RULE_WORKSPACE_VELOCITY, RULE_OWNER_VELOCITY}:
        return api_error(
            429,
            PAYPAL_DAILY_LIMIT_MESSAGE,
            ErrorType.RATE_LIMITED,
            extra={"checkout_guard": GUARD_CODE_DAILY_LIMIT},
        )
    # Never name the rule: a precise refusal tells a ring what to change.
    return api_error(
        403,
        PAYPAL_UNAVAILABLE_MESSAGE,
        ErrorType.FORBIDDEN,
        extra={"checkout_guard": GUARD_CODE_UNAVAILABLE},
    )


def checkout_guard_code(exc: Exception) -> str | None:
    """The refusal code on an HTTPException this guard or the abuse-pause
    refusal raised, else None."""
    detail = getattr(exc, "detail", None)
    error = detail.get("error") if isinstance(detail, dict) else None
    code = error.get("checkout_guard") if isinstance(error, dict) else None
    return code if code in GUARD_CODES | {CHECKOUT_GUARD_BILLING_PAUSED} else None


def check_paypal_checkout(
    store: Any,
    *,
    workspace_id: str,
    initiating_user_id: str | None,
    credit_amount_cents: int,
    settings: Settings,
    now: dt.datetime | None = None,
) -> GuardSubject | None:
    """Run before a PayPal order is created. Raises when a rule refuses."""
    if settings.paypal_topup_guard_mode == "off":
        return None
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    subject = load_subject(
        store, workspace_id, payer_user_id=initiating_user_id, settings=settings, now=current,
    )
    findings = account_findings(subject, settings)
    if not subject.established and credit_amount_cents > settings.paypal_new_workspace_max_topup_cents:
        findings.append(RULE_AMOUNT_CAP)
    enforced = _enforced(findings, settings)
    _report("checkout", subject, findings, enforced)
    if enforced:
        raise _refusal(enforced, settings)
    return subject


def check_paypal_capture(
    store: Any,
    *,
    workspace_id: str,
    settings: Settings,
    load_payer: Callable[[], PayPalPayerFacts] | None = None,
    now: dt.datetime | None = None,
) -> GuardSubject | None:
    """Run right before TrustedRouter captures an approved PayPal order.

    Re-checks account state (a latch may have landed since the order was
    created), reads the approved order's payer through ``load_payer`` for a
    non-established workspace, and counts the capture against the daily
    limits. Raises when a rule refuses; the order then stays uncaptured and
    the buyer is not charged. Established workspaces never read the order.
    """
    if settings.paypal_topup_guard_mode == "off":
        return None
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    subject = load_subject(store, workspace_id, payer_user_id=None, settings=settings, now=current)
    findings = account_findings(subject, settings)
    if (
        not subject.established
        and load_payer is not None
        and _payer_rules_on(settings)
        and not _enforced(findings, settings)
    ):
        facts = _read_payer(load_payer, workspace_id, settings)
        if facts is not None:
            findings.extend(payer_findings(store, subject, facts, settings))
    if not _enforced(findings, settings) and not subject.established:
        findings.extend(_velocity_findings(store, subject, settings))
    enforced = _enforced(findings, settings)
    _report("capture", subject, findings, enforced)
    if enforced:
        raise _refusal(enforced, settings)
    return subject


def _read_payer(
    load_payer: Callable[[], PayPalPayerFacts],
    workspace_id: str,
    settings: Settings,
) -> PayPalPayerFacts | None:
    try:
        return load_payer()
    except HTTPException as exc:
        if exc.status_code == 400:
            raise  # unknown or malformed order: the caller's to fix, nothing moved
        failure: Exception = exc
    except Exception as exc:  # noqa: BLE001 - classified below
        failure = exc
    log.warning(
        "paypal_topup_guard.payer_read_failed",
        extra={"workspace_id": workspace_id},
        exc_info=failure,
    )
    if _payer_rules_enforced(settings):
        # A reversible payment from a buyer we could not check fails closed.
        raise api_error(
            503, "PayPal checkout is temporarily unavailable", ErrorType.SERVICE_UNAVAILABLE,
        ) from failure
    return None


def _velocity_findings(store: Any, subject: GuardSubject, settings: Settings) -> list[str]:
    counters = [(
        RULE_WORKSPACE_VELOCITY,
        _WORKSPACE_VELOCITY_NAMESPACE,
        subject.workspace_id,
        settings.paypal_new_workspace_daily_captures,
    )]
    if subject.owner is not None:
        counters.append((
            RULE_OWNER_VELOCITY,
            _OWNER_VELOCITY_NAMESPACE,
            subject.owner.id,
            settings.paypal_new_owner_daily_captures,
        ))
    findings: list[str] = []
    for rule, namespace, key, limit in counters:
        try:
            hit = store.hit_rate_limit(
                namespace=namespace,
                subject=key,
                limit=limit,
                window_seconds=_VELOCITY_WINDOW_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - a reversible payment fails closed
            log.exception("paypal_topup_guard.rate_limit_unavailable", extra={"namespace": namespace})
            raise api_error(
                503, "PayPal checkout is temporarily unavailable", ErrorType.SERVICE_UNAVAILABLE,
            ) from exc
        if not hit.allowed:
            findings.append(rule)
    return findings


def observe_paypal_capture(
    store: Any,
    *,
    workspace_id: str,
    seller_protection: str | None,
    payer_email_matches: bool | None,
    settings: Settings,
) -> list[str]:
    """Log-only signals on a capture that already credited. Never raises."""
    try:
        signals: list[str] = []
        if seller_protection and seller_protection != "ELIGIBLE":
            signals.append(SIGNAL_SELLER_PROTECTION if seller_protection == "NOT_ELIGIBLE"
                           else f"seller_protection_{seller_protection.lower()}")
        if payer_email_matches is False:
            signals.append(SIGNAL_PAYER_MISMATCH)
        log.info(
            "paypal_topup_guard.capture_signals",
            extra={
                "workspace_id": workspace_id,
                "seller_protection": seller_protection or "unknown",
                "signals": ",".join(signals),
            },
        )
        if signals:
            workspace = store.get_workspace(workspace_id)
            owner = store.get_user(workspace.owner_user_id) if workspace is not None else None
            if not owner_is_established(store, owner, settings, dt.datetime.now(dt.UTC)):
                log.warning(
                    "paypal_topup_guard.observed",
                    extra={"stage": "captured", "workspace_id": workspace_id, "rules": ",".join(signals)},
                )
        return signals
    except Exception:  # noqa: BLE001 - observation after a committed credit
        log.warning("paypal_topup_guard.observe_failed", exc_info=True)
        return []


def latch_after_paypal_adverse(
    store: Any,
    results: Iterable[tuple[AdverseTrustEvent, AdverseTrustResult]],
    *,
    settings: Settings,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Latch non-established workspaces PayPal reports a dispute/reversal on.

    Runs after the adverse fact committed and never raises: the money side
    of the webhook is done, and a failed latch is retried by PayPal's next
    delivery of the same dispute (the latch is idempotent on its abuse_ref).
    Refunds are excluded: TrustedRouter issues those itself.
    """
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    actions: list[dict[str, Any]] = []
    for adverse, result in results:
        workspace_id = result.workspace_id
        if (
            workspace_id is None
            or adverse.kind != "dispute"
            or adverse.lifecycle_status not in ADVERSE_LATCH_LIFECYCLES
            or result.outcome not in {"applied", "replay"}
        ):
            continue
        try:
            actions.append(_latch_one(store, workspace_id, adverse, settings=settings, now=current))
        except Exception:  # noqa: BLE001 - never fail the committed webhook
            log.exception("paypal_topup_guard.adverse_latch_failed", extra={"workspace_id": workspace_id})
            ops_alert(
                f"paypal.adverse latch_failed workspace={workspace_id} "
                f"subtype={adverse.provider_subtype}; latch it by hand",
                fingerprint=ADVERSE_ALERT_FINGERPRINT,
                tags={"tr_component": "paypal_topup_guard", "workspace_id": workspace_id},
                claim_detail=f"failed:{adverse.adverse_ref}",
            )
    return actions


def _latch_one(
    store: Any,
    workspace_id: str,
    adverse: AdverseTrustEvent,
    *,
    settings: Settings,
    now: dt.datetime,
) -> dict[str, Any]:
    workspace = store.get_workspace(workspace_id)
    owner = store.get_user(workspace.owner_user_id) if workspace is not None else None
    established = owner_is_established(store, owner, settings, now)
    abuse_ref = f"paypal-adverse:{adverse.adverse_ref}"
    if established:
        action = "alert_only_established"
    elif not settings.paypal_adverse_auto_latch:
        action = "would_latch"
    else:
        latched = store.record_workspace_abuse_and_demote(
            workspace_id,
            abuse_ref=abuse_ref,
            operator_identity=ADVERSE_LATCH_OPERATOR,
            reason=f"PayPal {adverse.provider_subtype} {adverse.lifecycle_status} on a capture",
        )
        action = "latched" if latched else "already_latched"
    if action != "already_latched":
        ops_alert(
            f"paypal.adverse {action} workspace={workspace_id} "
            f"subtype={adverse.provider_subtype} status={adverse.lifecycle_status}",
            fingerprint=ADVERSE_ALERT_FINGERPRINT,
            tags={
                "tr_component": "paypal_topup_guard",
                "workspace_id": workspace_id,
                "paypal_adverse_action": action,
            },
            claim_detail=abuse_ref,
        )
    return {"workspace_id": workspace_id, "action": action}
