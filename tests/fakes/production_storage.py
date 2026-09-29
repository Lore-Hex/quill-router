"""The fail-closed production storage declaration shared by Settings tests.

Mirrors what rollout.sh renders for every control-plane revision: the
spanner-clickhouse backend with typed request records, the durable outboxes
and the ClickHouse control reader. Tests that need a valid production
``Settings`` spread this in and override single fields.
"""

from __future__ import annotations

from typing import Any

PRODUCTION_SPANNER_STORAGE: dict[str, Any] = {
    "storage_backend": "spanner-clickhouse",
    "spanner_instance_id": "trusted-router",
    "spanner_database_id": "trusted-router",
    "generation_records_enabled": True,
    "request_record_write_mode": "typed",
    "settle_outbox_enabled": True,
    "analytics_outbox_enabled": True,
    "operational_analytics_outbox_enabled": True,
    "operational_analytics_clickhouse_url": "http://10.0.0.1:8123",
    "operational_analytics_clickhouse_password": "control-read-" + "secret",
}
