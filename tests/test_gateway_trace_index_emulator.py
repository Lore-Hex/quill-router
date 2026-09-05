"""Real Spanner unique-index regression; opt in with a loopback emulator only."""
from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest

from trusted_router.storage_gcp_request_records import mark_gateway_authorization_settled
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType


def test_real_spanner_shared_trace_migration_preserves_each_authorization() -> None:
    host = os.environ.get("TR_TRACE_TEST_SPANNER_EMULATOR", "")
    if not host:
        pytest.skip("local Spanner emulator not requested")
    assert host.startswith(("127.0.0.1:", "localhost:")), "emulator must be loopback"
    from google.api_core.exceptions import AlreadyExists
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner
    from google.cloud.spanner_v1 import param_types

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SPANNER_EMULATOR_HOST", host)
        client = spanner.Client(project="trace-index-test", credentials=AnonymousCredentials())
        instance = client.instance("trace-" + uuid.uuid4().hex[:12])
        instance.create().result(timeout=30)
        database = instance.database("trace-test", ddl_statements=[
            "CREATE TABLE tr_gateway_authorization ("
            "authorization_id STRING(64) NOT NULL, settled BOOL NOT NULL, "
            "payload STRING(MAX), finalization_outcome STRING(32), "
            "finalized_cost_microdollars INT64, gateway_request_id STRING(37)"
            ") PRIMARY KEY (authorization_id)",
            "CREATE UNIQUE NULL_FILTERED INDEX tr_gateway_authorization_by_gateway_request_id "
            "ON tr_gateway_authorization (gateway_request_id)",
        ])
        try:
            database.create().result(timeout=30)
            first = GatewayAuthorization(
                id="gwa-first", workspace_id="test-workspace", key_hash="test-key-hash",
                model_id="vendor/model", provider="vendor", usage_type=UsageType.CREDITS,
                estimated_microdollars=100, credit_reservation_id="reservation-first",
                gateway_request_id="rlog_00112233445566778899aabbccddeeff",
                finalized_cost_microdollars=7, finalization_outcome="settled",
            )
            second = replace(first, id="gwa-second", finalized_cost_microdollars=11)
            with database.batch() as batch:
                batch.insert("tr_gateway_authorization", ("authorization_id", "settled"),
                             [(first.id, False), (second.id, False)])

            def settle(authorization: GatewayAuthorization) -> int:
                return database.run_in_transaction(
                    lambda tx: mark_gateway_authorization_settled(tx, param_types, authorization)
                )

            assert settle(first) == 1
            with pytest.raises(AlreadyExists):
                settle(second)
            # Preparing the nonunique index cannot remove the old guard.
            database.update_ddl([
                "CREATE NULL_FILTERED INDEX tr_gateway_authorization_by_trace_id "
                "ON tr_gateway_authorization (gateway_request_id)"
            ]).result(timeout=30)
            with pytest.raises(AlreadyExists):
                settle(second)
            database.update_ddl([
                "DROP INDEX tr_gateway_authorization_by_gateway_request_id"
            ]).result(timeout=30)
            assert settle(second) == 1
            assert settle(first) == settle(second) == 0
            with database.snapshot() as snapshot:
                rows = list(snapshot.execute_sql(
                    "SELECT authorization_id, finalized_cost_microdollars FROM "
                    "tr_gateway_authorization "
                    "WHERE gateway_request_id='rlog_00112233445566778899aabbccddeeff' "
                    "AND gateway_request_id IS NOT NULL "
                    "ORDER BY authorization_id"
                ))
            assert rows == [["gwa-first", 7], ["gwa-second", 11]]
        finally:
            instance.delete()
