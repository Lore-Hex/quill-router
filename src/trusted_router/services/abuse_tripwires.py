"""Abuse tripwires: detect-and-alert checks for stolen-payment top-up rings.

Alert only. Nothing here pauses, latches or refuses anything: each finding is
an ops alert (log + Sentry issue) that names the workspace ids and the signals
that fired, so an operator can review it and latch with
``POST /internal/admin/workspaces/{id}/abuse`` when it is real.

Primary signals (each one alone raises an alert):

* ``shared_session``: one client ``session_id`` seen in two or more distinct
  workspaces within ``SHARED_SESSION_WINDOW``, at least one of them funded
  (a top-up on record, or over ``FUNDED_MIN_WORKSPACE_SPEND`` of Credits spend
  in the window). An operator hopping between freshly funded accounts carries
  the same conversation along.
* ``starter_credit_farm``: the same shared session where no workspace is
  funded. One client conversation walks through fresh accounts one after
  another, spending each signup credit and moving on. A different abuse with
  a different remedy (the signup gate), so it is its own Sentry issue and
  never dilutes the payment-ring one.
* ``single_model_concentration``: at least ``CONCENTRATION_MIN_SHARE`` of a
  workspace's Credits spend on one model, over ``CONCENTRATION_MIN_SPEND``,
  within ``CONCENTRATION_WINDOW`` of its first top-up. ``models_used_1`` is
  reported alongside when exactly one model was used. Real customers explore
  a router; established single-model customers are excluded by the
  first-top-up window, not by the share.

Context signals (reported with a finding, never alerting on their own):

* ``new_github_account``: the signup's GitHub account was under
  ``NEW_GITHUB_ACCOUNT_AGE`` old when the TrustedRouter account was created.
* ``paypal_payer_mismatch``: a first-week account whose PayPal payer email
  differed from the account email (stored as a boolean only).
* ``already_paused``: the workspace is already billing-paused.

One sweep runs per hour across every replica, piggybacking on the synthetic
monitor tick. Each finding alerts once: per shared session (re-alerting when
another workspace joins it) and per concentrated workspace.

Sentry groups by signal type, not by session or workspace: every finding of
one type is an event in that type's single issue (``ALERT_FINGERPRINTS``). The
event message and tags carry the workspace ids, session digest and signals.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from trusted_router.config import Settings
from trusted_router.synthetic.alerts import ops_alert

log = logging.getLogger(__name__)

SHARED_SESSION_WINDOW = dt.timedelta(days=7)
SHARED_SESSION_MIN_WORKSPACES = 2
# A workspace that spent more than this in the window paid for it: the
# signup credit is 0.30 USD, and starter-credit farms spend at most that per
# account before moving on.
FUNDED_MIN_WORKSPACE_SPEND_MICRODOLLARS = 1_000_000  # 1 USD
CONCENTRATION_WINDOW = dt.timedelta(days=7)
CONCENTRATION_MIN_SHARE_BASIS_POINTS = 9_500  # 95 %
CONCENTRATION_MIN_SPEND_MICRODOLLARS = 5_000_000  # 5 USD
NEW_GITHUB_ACCOUNT_AGE = dt.timedelta(hours=24)
FIRST_WEEK = dt.timedelta(days=7)
QUERY_LIMIT = 500
MAX_ALERTS_PER_SWEEP = 25

SWEEP_CLAIM_SOURCE = "abuse-tripwire-sweep"
ALERT_CLAIM_SOURCE = "abuse-tripwire-alert"

# One Sentry issue per signal type. Findings are distinct events inside it.
ALERT_FINGERPRINTS: dict[str, list[str]] = {
    signal: ["abuse-tripwire", signal]
    for signal in (
        "shared_session",
        "starter_credit_farm",
        "single_model_concentration",
        "new_github_account",
        "deferred",
    )
}


class TripwireReader(Protocol):
    def shared_client_sessions(
        self, *, start_at: str, min_workspaces: int, limit: int,
    ) -> list[dict[str, Any]]: ...

    def concentrated_model_spend(
        self, *, start_at: str, min_total_microdollars: int, min_share_basis_points: int, limit: int,
    ) -> list[dict[str, Any]]: ...


@dataclasses.dataclass(frozen=True)
class WorkspaceContext:
    """What the store says about one workspace, for review context."""

    workspace_id: str
    created_at: dt.datetime | None = None
    first_topup_at: dt.datetime | None = None
    billing_paused: bool = False
    new_github_account: bool = False
    paypal_payer_mismatch: bool | None = None

    @property
    def funded(self) -> bool:
        return self.first_topup_at is not None

    def context_signals(self, now: dt.datetime) -> list[str]:
        signals: list[str] = []
        if self.new_github_account:
            signals.append("new_github_account")
        first_week = self.created_at is not None and now - self.created_at <= FIRST_WEEK
        if self.paypal_payer_mismatch and first_week:
            signals.append("paypal_payer_mismatch")
        if self.billing_paused:
            signals.append("already_paused")
        return signals


@dataclasses.dataclass(frozen=True)
class Finding:
    claim_key: str
    message: str
    fingerprint: list[str]
    tags: dict[str, str]
    workspace_ids: tuple[str, ...]


def parse_time(value: Any) -> dt.datetime | None:
    """A timezone-aware UTC datetime, or None for anything unparseable."""
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def github_account_was_new(github_created_at: str | None, signed_up_at: str | dt.datetime | None) -> bool:
    """Whether the GitHub account was under NEW_GITHUB_ACCOUNT_AGE old at signup.

    Unknown times are not "new": the GitHub profile may simply omit them.
    """
    github_created = parse_time(github_created_at)
    signed_up = parse_time(signed_up_at)
    if github_created is None or signed_up is None:
        return False
    return signed_up - github_created < NEW_GITHUB_ACCOUNT_AGE


def store_workspace_context(store: Any, workspace_id: str) -> WorkspaceContext:
    """Primary-key reads only: the workspace, its owner, its acquisition record."""
    workspace = store.get_workspace(workspace_id)
    if workspace is None:
        return WorkspaceContext(workspace_id=workspace_id)
    owner = store.get_user(workspace.owner_user_id) if workspace.owner_user_id else None
    attribution = None
    try:
        attribution = store.get_acquisition_attribution(workspace_id)
    except Exception:  # noqa: BLE001 - context is best effort
        log.warning("abuse_tripwire.attribution_read_failed", exc_info=True)
    return WorkspaceContext(
        workspace_id=workspace_id,
        created_at=parse_time(workspace.created_at),
        first_topup_at=parse_time(getattr(attribution, "first_purchase_at", None)),
        billing_paused=bool(getattr(workspace, "billing_paused", False)),
        new_github_account=(
            owner is not None
            and github_account_was_new(owner.github_account_created_at, owner.created_at)
        ),
        paypal_payer_mismatch=(
            None if owner is None or owner.paypal_payer_email_matches is None
            else not owner.paypal_payer_email_matches
        ),
    )


def _session_digest(session_id: str) -> str:
    # The raw client session id is the customer's; alerts carry a digest that
    # still joins every workspace sharing it.
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _app(value: Any) -> str:
    text = str(value or "").strip()
    return (text[:64] or "none").replace(" ", "_")


def _describe(context: WorkspaceContext, extra: Iterable[str], now: dt.datetime) -> str:
    signals = [*extra, *context.context_signals(now)]
    return f"{context.workspace_id}[{','.join(signals) or '-'}]"


def evaluate_shared_sessions(
    rows: Iterable[Mapping[str, Any]],
    *,
    context: Callable[[str], WorkspaceContext],
    concentrated: set[str],
    now: dt.datetime,
) -> list[Finding]:
    findings: list[Finding] = []
    for row in rows:
        session_id = str(row.get("session_id") or "")
        workspace_ids = sorted({str(w) for w in row.get("workspace_ids") or [] if w})
        if not session_id or len(workspace_ids) < SHARED_SESSION_MIN_WORKSPACES:
            continue
        contexts = [context(workspace_id) for workspace_id in workspace_ids]
        if all(item.billing_paused for item in contexts):
            continue  # already latched: nothing left for an operator to do
        digest = _session_digest(session_id)
        signal = "shared_session" if _session_funded(row, contexts) else "starter_credit_farm"
        app = _app(row.get("app"))
        described = " ".join(
            _describe(
                item,
                ["single_model_concentration"] if item.workspace_id in concentrated else [],
                now,
            )
            for item in contexts
        )
        findings.append(
            Finding(
                # A workspace joining the session later is new, latchable news.
                claim_key=f"session:{digest}:{len(workspace_ids)}",
                message=(
                    f"abuse.tripwire {signal} "
                    f"session={digest} workspaces={len(workspace_ids)} app={app} "
                    f"signals={signal} ws={described}"
                ),
                fingerprint=ALERT_FINGERPRINTS[signal],
                tags={
                    "tr_component": "abuse_tripwire",
                    "abuse_signal": signal,
                    "abuse_session": digest,
                    "abuse_workspaces": str(len(workspace_ids)),
                    "abuse_app": app,
                },
                workspace_ids=tuple(workspace_ids),
            )
        )
    return findings


def _session_funded(row: Mapping[str, Any], contexts: Iterable[WorkspaceContext]) -> bool:
    """Whether any workspace in the session paid: a top-up on record, or more
    Credits spend in the window than signup credit covers.

    A row without the spend column counts as funded, so a reader that cannot
    say keeps the payment-ring alert rather than hiding it.
    """
    if any(item.funded for item in contexts):
        return True
    spend = row.get("max_workspace_microdollars")
    if spend is None:
        return True
    try:
        return int(spend) > FUNDED_MIN_WORKSPACE_SPEND_MICRODOLLARS
    except (TypeError, ValueError):
        return True


def evaluate_concentration(
    rows: Iterable[Mapping[str, Any]],
    *,
    context: Callable[[str], WorkspaceContext],
    shared: set[str],
    now: dt.datetime,
) -> list[Finding]:
    findings: list[Finding] = []
    for row in rows:
        workspace_id = str(row.get("workspace_id") or "")
        total = int(row.get("total_microdollars") or 0)
        top = int(row.get("top_model_microdollars") or 0)
        models = int(row.get("models") or 0)
        if not workspace_id or total < CONCENTRATION_MIN_SPEND_MICRODOLLARS:
            continue
        if top * 10_000 < total * CONCENTRATION_MIN_SHARE_BASIS_POINTS:
            continue
        item = context(workspace_id)
        if item.billing_paused:
            continue
        # Within the first window after the first top-up. With no purchase on
        # record (privacy opt-out drops the acquisition record), the workspace
        # creation time stands in: it can only be earlier than any top-up.
        anchor = item.first_topup_at or item.created_at
        if anchor is None or now - anchor > CONCENTRATION_WINDOW:
            continue
        primary = ["single_model_concentration"]
        if models == 1:
            primary.append("models_used_1")
        if workspace_id in shared:
            primary.append("shared_session")
        signals = [*primary, *item.context_signals(now)]
        findings.append(
            Finding(
                claim_key=f"concentration:{workspace_id}",
                message=(
                    "abuse.tripwire single_model_concentration "
                    f"workspace={workspace_id} signals={','.join(signals)} "
                    f"share={top / total:.3f} models={models} spend_usd={total / 1_000_000:.2f} "
                    f"top_model={row.get('top_model') or 'unknown'} app={_app(row.get('app'))} "
                    f"days_since_first_topup={(now - anchor).total_seconds() / 86_400:.1f}"
                ),
                fingerprint=ALERT_FINGERPRINTS["single_model_concentration"],
                tags={
                    "tr_component": "abuse_tripwire",
                    "abuse_signal": "single_model_concentration",
                    "workspace_id": workspace_id,
                },
                workspace_ids=(workspace_id,),
            )
        )
    return findings


def run_tripwire_sweep(
    reader: TripwireReader,
    store: Any,
    *,
    now: dt.datetime | None = None,
    emit: Callable[[Finding], bool] | None = None,
) -> list[Finding]:
    """Evaluate both primary tripwires once; emit each finding at most once.

    Returns the findings that were emitted this sweep.
    """
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    session_rows = reader.shared_client_sessions(
        start_at=(current - SHARED_SESSION_WINDOW).isoformat(),
        min_workspaces=SHARED_SESSION_MIN_WORKSPACES,
        limit=QUERY_LIMIT,
    )
    concentration_rows = reader.concentrated_model_spend(
        start_at=(current - CONCENTRATION_WINDOW).isoformat(),
        min_total_microdollars=CONCENTRATION_MIN_SPEND_MICRODOLLARS,
        min_share_basis_points=CONCENTRATION_MIN_SHARE_BASIS_POINTS,
        limit=QUERY_LIMIT,
    )
    cache: dict[str, WorkspaceContext] = {}

    def context(workspace_id: str) -> WorkspaceContext:
        if workspace_id not in cache:
            cache[workspace_id] = store_workspace_context(store, workspace_id)
        return cache[workspace_id]

    shared = {str(w) for row in session_rows for w in row.get("workspace_ids") or [] if w}
    concentration = evaluate_concentration(concentration_rows, context=context, shared=shared, now=current)
    concentrated = {finding.workspace_ids[0] for finding in concentration}
    sessions = evaluate_shared_sessions(session_rows, context=context, concentrated=concentrated, now=current)
    rings = [finding for finding in sessions if finding.tags["abuse_signal"] == "shared_session"]
    farms = [finding for finding in sessions if finding.tags["abuse_signal"] != "shared_session"]
    # Payment-ring findings first, so farms never take the per-sweep cap from them.
    findings = [*rings, *concentration, *farms]
    send = emit or _emit
    emitted: list[Finding] = []
    deferred = 0
    for finding in findings:
        if len(emitted) >= MAX_ALERTS_PER_SWEEP:
            # Unclaimed, so the next hourly sweep picks these up.
            deferred += 1
            continue
        if not store.record_webhook_event_once(ALERT_CLAIM_SOURCE, finding.claim_key):
            continue
        if send(finding):
            emitted.append(finding)
    if deferred:
        ops_alert(
            f"abuse.tripwire deferred={deferred} findings past the per-sweep cap; next sweep continues",
            fingerprint=ALERT_FINGERPRINTS["deferred"],
            tags={"tr_component": "abuse_tripwire"},
        )
    log.info(
        "abuse_tripwire.sweep",
        extra={
            "shared_sessions": len(session_rows),
            "concentration_candidates": len(concentration_rows),
            "emitted": len(emitted),
            "deferred": deferred,
        },
    )
    return emitted


def _emit(finding: Finding) -> bool:
    # claim_detail keeps findings that share a type fingerprint distinct inside
    # any alerts_once scope; the store claim above already dedupes each one.
    ops_alert(finding.message, fingerprint=finding.fingerprint, tags=finding.tags, claim_detail=finding.claim_key)
    # Claimed either way: a Sentry outage must not re-page every hour.
    return True


def run_hourly_tripwire_sweep(
    settings: Settings,
    store: Any,
    *,
    reader: TripwireReader | None = None,
    now: dt.datetime | None = None,
) -> list[Finding] | None:
    """The scheduled entry point. At most one sweep per UTC hour across replicas.

    Returns None when the sweep did not run (disabled, unconfigured, or another
    replica owns this hour).
    """
    if not settings.abuse_tripwires_enabled:
        return None
    if reader is None:
        if not (
            settings.operational_analytics_clickhouse_url
            and settings.operational_analytics_clickhouse_password
        ):
            return None
        from trusted_router.operational_analytics import OperationalAnalyticsClient

        reader = OperationalAnalyticsClient(
            base_url=settings.operational_analytics_clickhouse_url,
            user=settings.operational_analytics_clickhouse_user,
            password=settings.operational_analytics_clickhouse_password,
            database=settings.operational_analytics_clickhouse_database,
        )
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    if not store.record_webhook_event_once(SWEEP_CLAIM_SOURCE, current.strftime("%Y-%m-%dT%H")):
        return None
    return run_tripwire_sweep(reader, store, now=current)


def alert_new_github_signup(
    *,
    workspace_id: str,
    github_created_at: str | None,
    now: dt.datetime | None = None,
) -> bool:
    """Signup-time context alert for a GitHub account under a day old.

    Detect only: the signup proceeds (it already receives no starter credit).
    """
    current = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    if not github_account_was_new(github_created_at, current):
        return False
    created = parse_time(github_created_at)
    age_minutes = int((current - created).total_seconds() // 60) if created else -1
    return ops_alert(
        "abuse.signal new_github_account "
        f"workspace={workspace_id} signals=new_github_account github_age_minutes={age_minutes}",
        fingerprint=ALERT_FINGERPRINTS["new_github_account"],
        tags={"tr_component": "abuse_tripwire", "abuse_signal": "new_github_account", "workspace_id": workspace_id},
        claim_detail=workspace_id,
    )
