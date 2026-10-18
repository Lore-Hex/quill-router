"""Exercise the actual covering-index Read RPC in an isolated emulator database."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import uuid4

import pytest

from tests.conformance.spanner_ddl import DDL
from trusted_router import social_rewards_monitor as monitor

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


@pytest.fixture(params=["spanner-emulator"], ids=lambda backend: f"backend={backend}")
def monitor_database(request, native_emulator_resources):
    assert request.param == "spanner-emulator"
    schema = next(ddl for ddl in DDL if ddl.startswith("CREATE TABLE tr_trust_event ("))
    database = native_emulator_resources[0]._instance.database(  # noqa: SLF001
        "reward-monitor-" + uuid4().hex[:10], ddl_statements=[schema],
    )
    database.create().result(timeout=120)
    try:
        ddl = (Path(__file__).resolve().parents[2]
               / "scripts/deploy/social_rewards_monitor_index.sql").read_text()
        ddl = "\n".join(line for line in ddl.splitlines() if not line.startswith("--"))
        database.update_ddl([ddl.strip().removesuffix(";")]).result(timeout=120)
        yield database
    finally:
        database.close()
        database.drop()


def test_native_monitor_index_receipts_without_marketing_and_late_commit(monitor_database):
    # The isolated database contains no attribution table: monitor correctness
    # cannot accidentally depend on marketing data. These are authoritative grant
    # facts in the production table shape, not mocked ReadRPC results.
    database = monitor_database
    now = dt.datetime.now(dt.UTC)
    columns = ("workspace_id", "event_id", "kind", "provider", "amount_micro",
               "credited_micro", "occurred_at", "recorded_at")

    def insert(event, timestamp):
        with database.batch() as batch:
            batch.insert("tr_trust_event", columns=columns,
                         values=[("workspace", event, "grant", "system", 30000,
                                  30000, timestamp, timestamp)])

    insert("social-rewards-v1:user:github", now)
    # Neighboring key prefixes must never enter the report.
    insert("social-rewards-v0:user:github", now)
    insert("social-rewards-v1;user:github", now)
    insert("unrelated-grant", now)
    client = database.spanner_api
    session = client.create_session(request={"database": database.name}, retry=None, timeout=10)
    try:
        rows = monitor.read_receipts(client, session.name, 1)
        result = monitor.summarize(rows, now=now, window_minutes=15, alert_claims=10)
        assert result["claims"] == 1 and result["issued_microdollars"] == 30000
        assert rows[0][0] == "social-rewards-v1:user:github"
        # A later commit has an earlier recording time and a key behind the last
        # observation. The next complete-prefix read still finds it.
        insert("social-rewards-v1:earlier:youtube", now - dt.timedelta(hours=1))
        with pytest.raises(ValueError, match="incomplete"):
            monitor.read_receipts(client, session.name, 1)
        rows = monitor.read_receipts(client, session.name, 2)
        result = monitor.summarize(rows, now=now, window_minutes=15, alert_claims=10)
        assert result["claims"] == 2 and result["issued_microdollars"] == 60000
        assert result["recent_claims"] == 1
    finally:
        client.delete_session(request={"name": session.name}, retry=None, timeout=10)
