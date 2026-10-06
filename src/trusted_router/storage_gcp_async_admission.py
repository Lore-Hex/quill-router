"""One strong, bounded admission read. No writes, entity scans, or fleet scans."""
from __future__ import annotations

from typing import Any

from google.cloud.spanner_v1 import param_types

from trusted_router.services.async_settle import Admission

ROW_LIMIT = 1000


def admission_statement(workspace_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    # LIMIT is INSIDE the aggregate. A sentinel row rejects truncation rather
    # than treating a partial SUM as the full exposure. Pending includes leases;
    # dead unresolved rows remain exposure. Shard zero carries replicated trust.
    return (
        "SELECT pending.n, pending.amount, trust.trust_tier FROM "
        "(SELECT COUNT(*) AS n, SUM(actual_cost_micro) AS amount FROM "
        "(SELECT actual_cost_micro FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_workspace_status} "
        "WHERE workspace_id=@ws AND status IN ('pending', 'dead') LIMIT 1001)) AS pending "
        "CROSS JOIN (SELECT trust_tier FROM tr_credit_balance "
        "WHERE workspace_id=@ws AND shard=0 AND trust_latched_at IS NULL "
        "AND COALESCE(ARRAY_LENGTH(billing_pause_causes), 0)=0) AS trust",
        {"ws": workspace_id}, {"ws": param_types.STRING},
    )


def read_admission(database: Any, workspace_id: str) -> Admission:
    sql, params, types = admission_statement(workspace_id)
    with database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(sql, params=params, param_types=types,
                                        timeout=0.2, retry=None,
                                        request_options={"priority": "PRIORITY_LOW"}))
    if len(rows) != 1 or rows[0][0] > ROW_LIMIT:
        raise ValueError("admission unavailable")
    count, amount, tier = rows[0]
    if type(count) is not int or count < 0 or type(tier) is not int:
        raise ValueError("admission unavailable")
    if count == 0 and amount is None:
        amount = 0
    if type(amount) is not int or not 0 <= amount <= (1 << 63) - 1:
        raise ValueError("admission unavailable")
    return Admission(amount, tier)
