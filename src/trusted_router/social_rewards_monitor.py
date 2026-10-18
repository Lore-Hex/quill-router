"""Bounded GCP campaign receipt monitor; never changes credits or campaign state.

Reads a covering index, not optional marketing logs. Every run starts at the
campaign prefix: no event-time watermark can permanently skip a late commit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

CAMPAIGN = "social-rewards-v1"
INDEX = "tr_trust_event_by_reward_event"
ACTIONS = frozenset({"github", "x_trustedrouter", "x_founder", "linkedin", "youtube"})
TIMEOUT = 10.0
MAX_ROWS = 10_000
COLUMNS = ["event_id", "workspace_id", "kind", "credited_micro", "recorded_at"]


def read_receipts(client: Any, session: str, limit: int) -> list[list[Any]]:
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError("receipt row limit outside safe bounds")
    result = client.read(request={
        "session": session, "table": "tr_trust_event", "index": INDEX,
        "columns": COLUMNS,
        "key_set": {"ranges": [{"start_closed": [CAMPAIGN + ":"],
                                 "end_open": [CAMPAIGN + ";"]}]},
        "limit": limit + 1,
        "transaction": {"single_use": {"read_only": {"strong": True}}},
        "request_options": {"priority": "PRIORITY_LOW", "request_tag": "tr_social_monitor"},
    }, retry=None, timeout=TIMEOUT)
    rows = [list(row) for row in result.rows]
    if len(rows) > limit:
        raise ValueError("receipt row budget exceeded; totals incomplete")
    return rows


def summarize(rows: list[list[Any]], *, now: dt.datetime, window_minutes: int,
              alert_claims: int, qa_hashes: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not 1 <= window_minutes <= 1440 or alert_claims < 1:
        raise ValueError("invalid monitoring thresholds")
    counts = dict.fromkeys(sorted(ACTIONS), 0)
    seen: dict[str, tuple[Any, ...]] = {}
    qa = recent = total = 0
    users: set[str] = set()
    anomalies: set[str] = set()
    cutoff = now - dt.timedelta(minutes=window_minutes)
    for row in rows:
        event, workspace, kind, amount, recorded = row
        if event in seen:
            if seen[event] != tuple(row):
                raise ValueError("conflicting campaign receipt")
            continue
        seen[event] = tuple(row)
        prefix, identity_action = event.split(":", 1)
        user, action = identity_action.rsplit(":", 1)
        if prefix != CAMPAIGN or not user or action not in ACTIONS or kind != "grant":
            raise ValueError("unexpected campaign receipt")
        amount = int(amount)
        stamp = dt.datetime.fromisoformat(str(recorded).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > now + dt.timedelta(minutes=1):
            raise ValueError("invalid receipt timestamp")
        if amount != 30_000:
            anomalies.add("unexpected_reward_amount")
        total += amount
        counts[action] += 1
        users.add(user)
        recent += stamp >= cutoff
        # QA never disappears from operational totals or alert evaluation.
        qa += hashlib.sha256(("tr-account:" + user).encode()).hexdigest() in qa_hashes
    if recent >= alert_claims:
        anomalies.add("redemption_velocity")
    return {"event": "social_rewards.monitor_completed", "campaign": CAMPAIGN,
            "observed_at": now.isoformat(), "complete": True, "claims": len(seen),
            "accounts": len(users), "issued_microdollars": total, "by_action": counts,
            "qa_claims": qa, "customer_claims": len(seen) - qa,
            "recent_claims": recent, "window_minutes": window_minutes,
            "alerts": sorted(anomalies)}


def load_qa_hashes(path: str | None) -> frozenset[str]:
    values = json.loads(Path(path).read_text()) if path else []
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in values
    ):
        raise ValueError("QA exclusion file must contain account SHA256 hashes")
    return frozenset(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--row-limit", type=int, default=5000)
    parser.add_argument("--window-minutes", type=int, default=15)
    parser.add_argument("--alert-claims", type=int, required=True)
    parser.add_argument("--qa-account-hashes")
    args = parser.parse_args()
    try:
        from google.cloud.spanner_v1 import SpannerClient

        qa = load_qa_hashes(args.qa_account_hashes)
        with SpannerClient() as client:
            session = client.create_session(request={"database":
                f"projects/{args.project}/instances/{args.instance}/databases/{args.database}"},
                retry=None, timeout=TIMEOUT)
            try:
                rows = read_receipts(client, session.name, args.row_limit)
            finally:
                client.delete_session(request={"name": session.name}, retry=None, timeout=TIMEOUT)
        report = summarize(rows, now=dt.datetime.now(dt.UTC), window_minutes=args.window_minutes,
                           alert_claims=args.alert_claims, qa_hashes=qa)
        print(json.dumps(report, sort_keys=True))
        return 2 if report["alerts"] else 0
    except Exception as exc:  # noqa: BLE001 - never dump raw receipt identifiers or RPC bodies.
        print(json.dumps({"event": "social_rewards.monitor_failed", "complete": False,
                          "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
