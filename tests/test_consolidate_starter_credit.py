from __future__ import annotations

import copy
from typing import Any

import pytest

from scripts.consolidate_starter_credit import make_plan, mutations
from scripts.expand_billing_shards import CREDIT_COLUMNS
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TRUST_COLUMNS


def fixture_rows() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    workspace = {"id": "ws", "billing_paused": False, "created_at": "2026-10-04T00:00:00Z"}
    credit = {"workspace_id": "ws", "shard_count": 16, "stripe_customer_id": "unchanged"}
    rows = [{
        "workspace_id": "ws", "shard": i, "total_credits": 18_750,
        "total_usage": 1000, "reserved": 0,
        **{name: None for name in CREDIT_BALANCE_TRUST_COLUMNS},
    } for i in range(16)]
    return workspace, credit, rows


def test_consolidation_conserves_totals_and_does_not_touch_keys() -> None:
    ws, account, rows = fixture_rows()
    original = copy.deepcopy((ws, account, rows))
    plan = make_plan(ws, account, rows, open_reservations=0)
    assert plan["totals"] == {"total_credits": 300_000, "total_usage": 16_000, "reserved": 0}
    assert plan["row"]["total_credits"] - plan["row"]["total_usage"] == 284_000
    assert plan["credit"] == {**account, "shard_count": 1}
    assert (ws, account, rows) == original
    changes = mutations(plan)
    assert len(changes) == 3
    assert changes[0]["update"]["table"] == "tr_credit_balance"
    assert changes[0]["update"]["columns"] == [*CREDIT_COLUMNS, "updated_at"]
    assert changes[1]["delete"]["key_set"]["keys"] == [["ws", str(i)] for i in range(1, 16)]
    assert changes[2]["update"]["values"][0][:2] == ["credit", "ws"]
    assert "api_key" not in str(changes) and "tr_key_limit" not in str(changes)


@pytest.mark.parametrize("defect", ["hold", "negative", "debt", "missing", "extra", "identity", "trust", "typed_pause", "pause", "federated", "large", "open", "legacy", "naive", "future", "marked"])
def test_consolidation_fails_closed(defect: str) -> None:
    ws, account, rows = fixture_rows()
    if defect == "hold":
        rows[0]["reserved"] = 1
    elif defect == "negative":
        rows[0]["total_usage"] = -1
    elif defect == "debt":
        rows[0]["total_usage"] = 20_000
    elif defect == "marked":
        for row in rows:
            row["in_debt"] = True
    elif defect == "missing":
        rows.pop()
    elif defect == "extra":
        rows.append(dict(rows[-1]))
    elif defect == "identity":
        rows[0]["workspace_id"] = "other"
    elif defect == "trust":
        rows[0]["trust_tier"] = 3
    elif defect == "typed_pause":
        for row in rows:
            row["billing_pause_causes"] = ["operator"]
    elif defect == "pause":
        ws["billing_paused"] = True
    elif defect == "federated":
        ws["federated_home"] = "aws"
    elif defect == "large":
        rows[0]["total_credits"] = 2_000_000
    elif defect == "legacy":
        ws["created_at"] = "2026-07-01T00:00:00Z"
    elif defect == "naive":
        ws["created_at"] = "2026-10-04T00:00:00"
    elif defect == "future":
        ws["created_at"] = "9999-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        make_plan(ws, account, rows, open_reservations=int(defect == "open"))


def test_consolidation_replay_is_noop() -> None:
    ws, account, rows = fixture_rows()
    first = make_plan(ws, account, rows, open_reservations=0)
    second = make_plan(ws, first["credit"], [first["row"]], open_reservations=0)
    assert mutations(second) == []
    assert second["totals"] == first["totals"]


def test_native_emulator_consolidation_preserves_credit_and_usage() -> None:
    import os

    import grpc
    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.cloud.spanner_v1.services.spanner.transports.grpc import SpannerGrpcTransport

    from scripts.consolidate_starter_credit import execute
    from tests.conformance.spanner_emulator import emulator_resources

    ws, account, rows = fixture_rows()
    with emulator_resources() as (database, _instance):
        import json

        from google.cloud.spanner_v1 import COMMIT_TIMESTAMP

        with database.batch() as batch:
            batch.insert("tr_entities", ("kind", "id", "body", "updated_at"), [
                ("workspace", "ws", json.dumps(ws), COMMIT_TIMESTAMP),
                ("credit", "ws", json.dumps(account), COMMIT_TIMESTAMP),
            ])
            batch.insert("tr_credit_balance", CREDIT_COLUMNS, [
                tuple(row[column] for column in CREDIT_COLUMNS) for row in rows
            ])
        transport = SpannerGrpcTransport(channel=grpc.insecure_channel(os.environ["SPANNER_EMULATOR_HOST"]))
        with SpannerClient(transport=transport) as client:
            session = client.create_session(request={"database": database.name})
            try:
                before = execute(client, session.name, "ws", apply=False)
                with database.batch() as batch:
                    batch.insert("tr_reservation", ("reservation_id", "workspace_id", "settled"), [("open", "ws", False)])
                with pytest.raises(ValueError, match="open reservations"):
                    execute(client, session.name, "ws", apply=True)
                with database.batch() as batch:
                    batch.update("tr_reservation", ("reservation_id", "settled"), [("open", True)])
                applied = execute(client, session.name, "ws", apply=True)
                after = execute(client, session.name, "ws", apply=False)
                assert applied["applied"] is True
                assert before["current_shards"] == 16 and after["current_shards"] == 1
                assert before["totals"] == after["totals"] == applied["totals"]
                assert execute(client, session.name, "ws", apply=True)["applied"] is False
            finally:
                client.delete_session(request={"name": session.name})
