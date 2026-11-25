"""Starter-credit session gate: one client conversation, one free account.

A starter-credit farm signs up fresh accounts and carries one long client
conversation through them, swapping in the next account's key as each signup
credit runs out. The client keeps sending the same ``session_id``, so the
conversation shows up in workspace after workspace (abuse_tripwires.py's
``starter_credit_farm`` alert). This gate refuses that at authorize time.

A request is checked only when all of these hold, so established and paying
customers never reach a store read:

* it can spend credits (the gateway skips BYOK-only requests) and carries a
  distinctive client ``session_id`` (``distinctive_session_id``): caller-chosen
  strings like ``research-session-42`` from the docs collide between unrelated
  users, so only machine-generated identifiers count as one conversation;
* its workspace was created within ``NEW_WORKSPACE_AGE``;
* the workspace has never bought credits: no ``first_purchase_at`` on its
  acquisition record, and no more than its starter credit plus
  ``FUNDED_SLACK_MICRODOLLARS`` on its live ledger. The ledger covers backends
  and workspaces without an acquisition record. Known gap: a new workspace that
  buys credits and then transfers them out can read as unfunded again; it is
  told to add credits.

Two steps, so a request that never gets admitted never claims anything:
``check`` reads before the credit reservation and refuses when a different
never-funded workspace holds the session within ``CLAIM_WINDOW``; ``claim``
records this workspace as the holder after the authorization succeeds. Store
errors fail open in both: the gate must never take authorize down. Each step's
store calls run under their own ``GATE_RPC_SECONDS`` Spanner deadline, nested in
the request's shared one, so a slow gate read can never spend the time the
billing reservation needs; the gate is skipped when less than
``GATE_MIN_REMAINING_SECONDS`` of the shared budget is left. On Postgres the
claim's statements carry their own ``statement_timeout``; the funding read is
the same ledger read (and pool) the reservation itself makes on every
request, so it adds no stall the reservation would not already hit.

Claims are small entities written only for distinctive sessions of
never-funded workspaces under 14 days old, so they grow with new free signups
(tens a day), not with traffic. They carry no TTL yet: ``tr_entities``' row
deletion policy covers ``rate_limit`` only, and extending it is a schema
migration (scripts/deploy/migrate_entity_ttl.sh).

Known limits, each costing a farmer at most a request or two:

* Two never-funded workspaces authorizing an unclaimed session at the same
  instant can both be admitted once; the store keeps one holder and the
  other's next request is refused.

The fast path's edge never decides for these workspaces: it admits only under
a lease, and a never-funded workspace is trust tier 0, which takes none
(fastpath/internal/store/store.go). Their requests always reach this route.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

from trusted_router.services.abuse_tripwires import parse_time
from trusted_router.typed_balance import live_credit_summary

log = logging.getLogger(__name__)

CLAIM_WINDOW = dt.timedelta(days=7)
NEW_WORKSPACE_AGE = dt.timedelta(days=14)
#: Credits beyond the starter grant that still count as unfunded: social
#: rewards ($0.15 signup plus $0.03 actions). The smallest purchase ($1) must
#: stay above it so that adding credits always lifts the gate.
FUNDED_SLACK_MICRODOLLARS = 500_000
MINIMUM_PURCHASE_MICRODOLLARS = 1_000_000

#: The gate's own Spanner budget per step, and the shared budget it leaves alone.
GATE_RPC_SECONDS = 0.5
GATE_MIN_REMAINING_SECONDS = 3.0

REFUSAL_MESSAGE = (
    "This conversation already used free starter credit on another account. "
    "Add credits to this account to continue it."
)


@dataclasses.dataclass(frozen=True)
class GateDecision:
    #: The other never-funded workspace holding the session; refuse when set.
    holder: str | None = None
    #: The session digest this workspace claims once admitted; None when exempt.
    claim_key: str | None = None


EXEMPT = GateDecision()


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DISTINCTIVE_MIN_LENGTH = 20


def distinctive_session_id(session_id: str) -> bool:
    """A machine-generated id two unrelated callers will not both send.

    A UUID, or at least ``DISTINCTIVE_MIN_LENGTH`` characters mixing upper case,
    lower case and digits (agent session ids such as ``ses_df6da8c96ffeDTQAdcz``).
    Human-chosen names and short counters are left alone.
    """
    if _UUID.fullmatch(session_id):
        return True
    return (
        len(session_id) >= DISTINCTIVE_MIN_LENGTH
        and any(char.islower() for char in session_id)
        and any(char.isupper() for char in session_id)
        and any(char.isdigit() for char in session_id)
    )


class _BudgetTooLow(Exception):
    """Less than GATE_MIN_REMAINING_SECONDS of the request's budget is left."""


@contextlib.contextmanager
def _gate_deadline() -> Iterator[None]:
    """A short Spanner deadline nested in the request's shared one.

    On exit the shared deadline is restored untouched, so whatever the gate
    spent, billing keeps the rest of its budget.
    """
    from trusted_router.storage_gcp_io import shared_rpc_budget_remaining, spanner_rpc_deadline

    remaining = shared_rpc_budget_remaining()
    if remaining is not None and remaining < GATE_MIN_REMAINING_SECONDS:
        raise _BudgetTooLow
    # The per-RPC cap also switches off the SDK's fixed two-second reset
    # backoff (configure_spanner_rpc_deadlines), so a transport reset fails
    # open within the gate's budget instead of sleeping past it.
    with spanner_rpc_deadline(time.monotonic() + GATE_RPC_SECONDS, max_rpc_seconds=GATE_RPC_SECONDS):
        yield


def bounded(fn: Any) -> Any:
    """Run one optional gate read under the gate's own deadline; raises on failure."""
    with _gate_deadline():
        return fn()


def session_key(session_id: str) -> str:
    """Digest of the client session id; the raw id is the customer's and is not stored."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _funded(store: Any, workspace_id: str, *, starter_credit_microdollars: int) -> bool:
    try:
        attribution = store.get_acquisition_attribution(workspace_id)
    except NotImplementedError:
        # Postgres keeps no acquisition records; the ledger below still decides.
        attribution = None
    if attribution is not None and getattr(attribution, "first_purchase_at", None):
        return True
    summary = live_credit_summary(workspace_id, store=store)
    if summary is None:
        return False
    granted = max(
        starter_credit_microdollars,
        int(getattr(attribution, "starter_credit_microdollars", 0) or 0),
    )
    return int(summary["total_credits"]) > granted + FUNDED_SLACK_MICRODOLLARS


def check(
    store: Any,
    workspace: Any,
    session_id: str | None,
    *,
    now: dt.datetime,
    starter_credit_microdollars: int,
) -> GateDecision:
    """Read-only: whether this request is exempt, may claim, or must be refused."""
    if not session_id or not distinctive_session_id(session_id):
        return EXEMPT
    created = parse_time(getattr(workspace, "created_at", None))
    if created is None or now - created > NEW_WORKSPACE_AGE:
        return EXEMPT
    try:
        with _gate_deadline():
            if _funded(store, workspace.id, starter_credit_microdollars=starter_credit_microdollars):
                return EXEMPT
            key = session_key(session_id)
            holder: str | None = store.claim_starter_session(
                key,
                workspace.id,
                now=int(now.timestamp()),
                window_seconds=int(CLAIM_WINDOW.total_seconds()),
                record=False,
            )
    except Exception:  # noqa: BLE001 - fail open; authorize must not depend on this gate
        log.warning("billing.starter_session_gate_failed workspace_id=%s", workspace.id, exc_info=True)
        return EXEMPT
    return GateDecision(holder=holder, claim_key=None if holder else key)


def claim(store: Any, decision: GateDecision, workspace_id: str, *, now: dt.datetime) -> None:
    """After admission: record this workspace as the session's holder. Best effort."""
    if decision.claim_key is None:
        return
    try:
        with _gate_deadline():
            store.claim_starter_session(
                decision.claim_key,
                workspace_id,
                now=int(now.timestamp()),
                window_seconds=int(CLAIM_WINDOW.total_seconds()),
            )
    except Exception:  # noqa: BLE001 - the request is already admitted
        log.warning("billing.starter_session_claim_failed workspace_id=%s", workspace_id, exc_info=True)


def starter_session_conflict(
    store: Any,
    workspace: Any,
    session_id: str | None,
    *,
    now: dt.datetime,
    starter_credit_microdollars: int,
) -> str | None:
    """Check and claim in one call: the holder to refuse for, or None after claiming."""
    decision = check(
        store, workspace, session_id, now=now, starter_credit_microdollars=starter_credit_microdollars,
    )
    if decision.holder is None:
        claim(store, decision, workspace.id, now=now)
    return decision.holder
