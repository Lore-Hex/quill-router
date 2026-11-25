"""Starter-credit session gate: one client conversation, one free account.

A starter-credit farm signs up fresh accounts and carries one long client
conversation through them, swapping in the next account's key as each signup
credit runs out. The client keeps sending the same ``session_id``, so the
conversation shows up in workspace after workspace (abuse_tripwires.py's
``starter_credit_farm`` alert). This gate refuses that at authorize time.

A request is checked only when all of these hold, so established and paying
customers never reach a store read:

* it carries a client ``session_id``;
* its workspace was created within ``NEW_WORKSPACE_AGE``;
* the workspace has never bought credits: no ``first_purchase_at`` on its
  acquisition record, and (where the store keeps a typed ledger) no more than
  its starter credit plus ``FUNDED_SLACK_MICRODOLLARS`` ever credited. The
  ledger check covers workspaces whose best-effort acquisition record is missing.

Such a workspace claims the session. When a different never-funded workspace
already claimed it within ``CLAIM_WINDOW``, the request is refused with a 402
that says adding credits continues it. Buying credits lifts the gate for that
workspace. Store errors fail open: the gate must never take authorize down.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Any

from trusted_router.services.abuse_tripwires import parse_time

log = logging.getLogger(__name__)

CLAIM_WINDOW = dt.timedelta(days=7)
NEW_WORKSPACE_AGE = dt.timedelta(days=14)
#: Credits beyond the starter grant that still count as unfunded: social
#: rewards ($0.15 signup plus $0.03 actions). The smallest purchase ($1) must
#: stay above it so that adding credits always lifts the gate.
FUNDED_SLACK_MICRODOLLARS = 500_000
MINIMUM_PURCHASE_MICRODOLLARS = 1_000_000

REFUSAL_MESSAGE = (
    "This conversation already used free starter credit on another account. "
    "Add credits to this account to continue it."
)


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
    snapshot_reader = getattr(store, "typed_credit_snapshot", None)
    if snapshot_reader is None:
        return False
    snapshot = snapshot_reader(workspace_id)
    if snapshot is None:
        return False
    granted = max(
        starter_credit_microdollars,
        int(getattr(attribution, "starter_credit_microdollars", 0) or 0),
    )
    return int(snapshot[0]) > granted + FUNDED_SLACK_MICRODOLLARS


def starter_session_conflict(
    store: Any,
    workspace: Any,
    session_id: str | None,
    *,
    now: dt.datetime,
    starter_credit_microdollars: int,
) -> str | None:
    """The never-funded workspace that already holds this session, if any."""
    if not session_id:
        return None
    created = parse_time(getattr(workspace, "created_at", None))
    if created is None or now - created > NEW_WORKSPACE_AGE:
        return None
    try:
        if _funded(store, workspace.id, starter_credit_microdollars=starter_credit_microdollars):
            return None
        holder: str | None = store.claim_starter_session(
            session_key(session_id),
            workspace.id,
            now=int(now.timestamp()),
            window_seconds=int(CLAIM_WINDOW.total_seconds()),
        )
    except Exception:  # noqa: BLE001 - fail open; authorize must not depend on this gate
        log.warning("billing.starter_session_gate_failed workspace_id=%s", workspace.id, exc_info=True)
        return None
    return holder
