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
* ``new_workspace_payer_not_verified``: PayPal did not attest VERIFIED
  wallet status on the approved order for a non-established workspace.
* ``invalid_paypal_payer_email``: an invalid or known typo address, or a
  missing address on an order for a non-established workspace.
* ``new_workspace_daily_captures`` / ``new_owner_daily_captures``: more
  PayPal captures in a rolling day than the per-workspace / per-owner limit.

Logged-only signals (never refuse; promote one in code once it proves
quiet): ``phone_unverified``, ``payer_email_mismatch`` and
``seller_protection_not_eligible``. PayPal Seller Protection does not cover
intangible goods, so most of our captures are expected to be NOT_ELIGIBLE;
the status is logged to measure that rather than to gate on it.

Established workspaces skip the new-workspace limits and verification check:
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
from collections.abc import Iterable, Sequence
from typing import Any

from email_validator import EmailNotValidError, validate_email

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.abuse_tripwires import github_account_was_new, parse_time
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
RULE_PAYER_VERIFICATION = "new_workspace_payer_not_verified"
RULE_PAYER_EMAIL = "invalid_paypal_payer_email"
RULE_WORKSPACE_VELOCITY = "new_workspace_daily_captures"
RULE_OWNER_VELOCITY = "new_owner_daily_captures"
SIGNAL_PHONE_UNVERIFIED = "phone_unverified"
SIGNAL_PAYER_MISMATCH = "payer_email_mismatch"
SIGNAL_SELLER_PROTECTION = "seller_protection_not_eligible"

ENFORCED_RULES = frozenset({
    RULE_WORKSPACE_PAUSED,
    RULE_ACCOUNT_RESTRICTED,
    RULE_NEW_GITHUB_ACCOUNT,
    RULE_DISPOSABLE_EMAIL,
    RULE_AMOUNT_CAP,
    RULE_PAYER_VERIFICATION,
    RULE_PAYER_EMAIL,
    RULE_WORKSPACE_VELOCITY,
    RULE_OWNER_VELOCITY,
})

# Pause causes that mean "do not take another PayPal payment from here".
# Migration/resharding/federation pauses are operational, not about the payer.
_PAYPAL_BLOCKING_PAUSE_CAUSES = frozenset({"abuse", "principal_recovery"})
_VELOCITY_WINDOW_SECONDS = 86_400
_WORKSPACE_VELOCITY_NAMESPACE = "paypal_capture_workspace"
_OWNER_VELOCITY_NAMESPACE = "paypal_capture_owner"

PAYPAL_UNAVAILABLE_MESSAGE = (
    "PayPal is not available for this account right now. Add credits with a card instead."
)
PAYPAL_DAILY_LIMIT_MESSAGE = (
    "This account has reached today's PayPal top-up limit. "
    "Try again tomorrow or add credits with a card."
)

# Machine-readable refusal codes, so the console can show the right line.
GUARD_CODE_AMOUNT_CAP = "paypal_amount_cap"
GUARD_CODE_DAILY_LIMIT = "paypal_daily_limit"
GUARD_CODE_UNAVAILABLE = "paypal_unavailable"
GUARD_CODES = frozenset({GUARD_CODE_AMOUNT_CAP, GUARD_CODE_DAILY_LIMIT, GUARD_CODE_UNAVAILABLE})

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
    return GuardSubject(
        workspace_id=workspace_id,
        pause_causes=frozenset(getattr(workspace, "billing_pause_causes", None) or ()),
        owner=owner,
        payer=payer,
        established=owner_is_established(store, owner, settings, now),
    )


def account_findings(subject: GuardSubject) -> list[str]:
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
    if owner is not None and not owner.phone_verified:
        findings.append(SIGNAL_PHONE_UNVERIFIED)
    return findings


def _enforced(findings: Iterable[str], settings: Settings) -> list[str]:
    if settings.paypal_topup_guard_mode != "enforce":
        return []
    demoted = settings.paypal_topup_guard_log_only_rule_set
    return [rule for rule in findings if rule in ENFORCED_RULES and rule not in demoted]


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
    findings = account_findings(subject)
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
    now: dt.datetime | None = None,
) -> GuardSubject | None:
    """Run right before TrustedRouter captures an approved PayPal order.

    Re-checks account state (a latch may have landed since the order was
    created) and counts the capture against the daily limits. Raises when a
    rule refuses; the order then stays uncaptured and the buyer is not
    charged.
    """
    if settings.paypal_topup_guard_mode == "off":
        return None
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    subject = load_subject(store, workspace_id, payer_user_id=None, settings=settings, now=current)
    findings = account_findings(subject)
    if not _enforced(findings, settings) and not subject.established:
        findings.extend(_velocity_findings(store, subject, settings))
    enforced = _enforced(findings, settings)
    _report("capture", subject, findings, enforced)
    if enforced:
        raise _refusal(enforced, settings)
    return subject


def check_approved_paypal_payer(
    subject: GuardSubject, *, account_status: object, payer_email: object,
    credit_cents: int, settings: Settings,
) -> None:
    """Use only the merchant's authenticated Orders response, never browser fields."""
    findings: list[str] = []
    if not subject.established:
        if account_status != "VERIFIED":
            findings.append(RULE_PAYER_VERIFICATION)
        if credit_cents > settings.paypal_new_workspace_max_topup_cents:
            findings.append(RULE_AMOUNT_CAP)
    if payer_email is not None or not subject.established:
        try:
            domain = validate_email(
                payer_email.strip() if isinstance(payer_email, str) else "",
                check_deliverability=False,
            ).domain.lower()
        except EmailNotValidError:
            domain = ""
        # Exact known mistakes only. Never fuzzy-match custom or international domains.
        if not domain or domain.endswith(".con") or domain in {"gmial.com", "gamil.com", "gmai.com"}:
            findings.append(RULE_PAYER_EMAIL)
    enforced = _enforced(findings, settings)
    _report("approved_order", subject, findings, enforced)
    if enforced:
        raise _refusal(enforced, settings)


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
