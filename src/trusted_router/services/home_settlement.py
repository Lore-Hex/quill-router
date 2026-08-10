"""PEER side of deferred settlement: deliver recorded debt to the home plane.

The forwarder reads pending rows from the home-settlement outbox, presents
each to the home plane's apply-usage endpoint, and advances the row's state
machine on the VERDICT — never on the transport.

CLASSIFICATION IS THE WHOLE SAFETY ARGUMENT HERE. Dead-lettering is reserved
for STRUCTURED verdicts (a TrustedRouter error body carrying the exact code):

  * 200 {outcome: applied|already}  -> forwarded (insert-once at home makes
                                       at-least-once delivery exactly-once)
  * 409 settlement_terms_conflict   -> dead_letter (corruption, not weather)
  * 404 workspace_unknown           -> dead_letter (workspace gone at home)
  * 429 settlement_clamped          -> stays pending (home's aggregate clamp;
                                       backpressure, retries next window)
  * ANYTHING else                   -> stays pending, and the PASS STOPS

"Anything else" deliberately includes a bare 404. A home plane that rolled
back past 2c answers apply-usage with a route-not-found 404; a proxy answers
with an HTML default page; a half-deployed home answers with who-knows-what.
Classifying those as verdicts would silently destroy the entire backlog on
the exact day home is having problems — so an unparseable answer is an
OUTAGE, and one outage signal parks the whole pass (per-row hammering of a
struggling home is how outages get worse).

Single-flight per process: the scheduler and the operator endpoint share one
non-blocking lock, so overlapping triggers collapse into one pass.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import httpx

from trusted_router.synthetic.alerts import ops_alert
from trusted_router.types import ErrorType

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 15.0

_pass_lock = threading.Lock()

#: Verdict classifications.
FORWARDED = "forwarded"
DEAD_LETTER = "dead_letter"
CLAMPED = "clamped"
RETRY = "retry"

_DEAD_LETTER_CODES = {
    str(ErrorType.SETTLEMENT_TERMS_CONFLICT): "terms conflict recorded at home",
    str(ErrorType.WORKSPACE_UNKNOWN): "workspace unknown at home",
}


def classify_apply_response(status_code: int, body: bytes) -> tuple[str, str]:
    """Map one apply-usage response to (classification, reason).

    Keys on the STRUCTURED body, never the status alone — see module
    docstring for why a bare 404 must read as an outage.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return RETRY, f"unparseable body (http {status_code})"
    if not isinstance(payload, dict):
        return RETRY, f"non-object body (http {status_code})"

    if status_code == 200:
        outcome = str((payload.get("data") or {}).get("outcome") or "")
        if outcome in {"applied", "already"}:
            return FORWARDED, outcome
        return RETRY, f"200 with unrecognized outcome {outcome!r}"

    error_type = str((payload.get("error") or {}).get("type") or "")
    if error_type in _DEAD_LETTER_CODES:
        return DEAD_LETTER, _DEAD_LETTER_CODES[error_type]
    if error_type == str(ErrorType.SETTLEMENT_CLAMPED):
        return CLAMPED, "home's aggregate clamp; retries next window"
    return RETRY, f"http {status_code} type {error_type!r}"


def drain_home_settlements(settings: Any, *, limit: int = 50) -> dict[str, Any]:
    """Run one forwarding pass. Safe to call from anywhere, any time."""
    from trusted_router.storage import STORE

    pending_reader = getattr(STORE, "pending_home_settlements", None)
    if pending_reader is None:
        return {"skipped": "store has no home-settlement outbox"}
    home = str(getattr(settings, "federation_home_base_url", "") or "").rstrip("/")
    token = str(getattr(settings, "federation_settlement_home_token", "") or "")
    if not home or not token:
        return {"skipped": "no settlement home configured"}

    if not _pass_lock.acquire(blocking=False):
        return {"skipped": "a pass is already running"}
    try:
        return _run_pass(STORE, home, token, limit)
    finally:
        _pass_lock.release()


#: Backoff schedule. Clamped rows wait out home's daily window (the route's
#: own Retry-After says 3600); outage-shaped rows back off exponentially so a
#: parked backlog does not hammer a struggling home from every instance's
#: loop, and CAPPED so a long outage still retries within a bounded delay.
CLAMP_RETRY_SECONDS = 3600
OUTAGE_RETRY_CAP_SECONDS = 1800


def _outage_backoff_seconds(attempts: int) -> int:
    return min(60 * (2 ** min(int(attempts), 5)), OUTAGE_RETRY_CAP_SECONDS)


def _run_pass(store: Any, home: str, token: str, limit: int) -> dict[str, Any]:
    rows = store.pending_home_settlements(limit=limit)
    counts = {
        "examined": len(rows),
        "forwarded": 0,
        "dead_lettered": 0,
        "clamped": 0,
        "outage": 0,
        # Rows another drainer resolved between our read and our mark. The
        # transitions COUNTED here are only ones this pass actually made —
        # a count that includes the race loser's no-op reports transitions
        # that never happened, and the drain endpoint is the observability
        # surface operators trust during an incident.
        "raced": 0,
    }
    if not rows:
        return counts

    url = f"{home}/v1/internal/federation/apply-usage"
    timeout = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        for row in rows:
            authorization_id = row["authorization_id"]
            try:
                response = client.post(
                    url,
                    json={
                        "authorization_id": authorization_id,
                        "workspace_id": row["workspace_id"],
                        "cost_microdollars": row["cost_microdollars"],
                    },
                    headers={"x-trustedrouter-federation-settlement-token": token},
                )
            except httpx.HTTPError as exc:
                store.bump_home_settlement_attempt(
                    authorization_id,
                    error=f"transport: {exc}",
                    retry_in_seconds=_outage_backoff_seconds(row.get("attempts", 0)),
                )
                counts["outage"] += 1
                log.warning("home settlement pass parked: transport error to home")
                break

            classification, reason = classify_apply_response(response.status_code, response.content)
            if classification == FORWARDED:
                if store.mark_home_settlement_forwarded(authorization_id):
                    counts["forwarded"] += 1
                else:
                    counts["raced"] += 1
            elif classification == DEAD_LETTER:
                if store.mark_home_settlement_dead_letter(authorization_id, reason=reason):
                    counts["dead_lettered"] += 1
                    ops_alert(
                        f"home settlement {authorization_id} dead-lettered: {reason}",
                        fingerprint=["home-settlement", "dead-letter"],
                        tags={"authorization_id": authorization_id},
                    )
                else:
                    counts["raced"] += 1
            elif classification == CLAMPED:
                store.bump_home_settlement_attempt(
                    authorization_id, error=reason, retry_in_seconds=CLAMP_RETRY_SECONDS
                )
                counts["clamped"] += 1
            else:  # RETRY — an outage signal parks the whole pass.
                store.bump_home_settlement_attempt(
                    authorization_id,
                    error=reason,
                    retry_in_seconds=_outage_backoff_seconds(row.get("attempts", 0)),
                )
                counts["outage"] += 1
                log.warning("home settlement pass parked: %s", reason)
                break
    return counts
