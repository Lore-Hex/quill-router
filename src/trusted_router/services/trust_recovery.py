"""Operational alerts for durable trust recovery work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.synthetic.alerts import ops_alert

PROVIDER_CONSISTENCY_DELAY_SECONDS = {"stripe": 900, "x402": 900}


def alert_unrecovered_principal(result: Any) -> bool:
    if result.unrecovered_micro <= 0 or result.workspace_id is None:
        return False
    return ops_alert(
        "trust.unrecovered_principal "
        f"workspace_id={result.workspace_id} "
        f"unrecovered_micro={result.unrecovered_micro}",
        fingerprint=["trust.unrecovered_principal", result.workspace_id],
        tags={"workspace_id": result.workspace_id},
    )


def alert_stale_trust_inbox(store: Any, *, now: datetime | None = None) -> int:
    observed_at = now or datetime.now(UTC)
    alerted = 0
    for provider, delay in PROVIDER_CONSISTENCY_DELAY_SECONDS.items():
        rows = store.list_stale_trust_inbox(
            older_than=observed_at - timedelta(seconds=delay)
        )
        for row in rows:
            if row.provider != provider:
                continue
            ops_alert(
                "trust.inbox_stale "
                f"provider={row.provider} adverse_ref={row.adverse_ref} "
                f"received_at={row.received_at.isoformat()}",
                fingerprint=["trust.inbox_stale", row.provider, row.adverse_ref],
                tags={"provider": row.provider, "adverse_ref": row.adverse_ref},
            )
            alerted += 1
    return alerted


# PR 1b: fact ingestion stays live while lease qualification remains off.
PROVIDER_CONSISTENCY_DELAY_SECONDS.update({"paypal": 10_800, "adyen": 0})
