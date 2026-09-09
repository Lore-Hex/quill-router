from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded
from hypothesis import given
from hypothesis import strategies as st

from scripts import expand_billing_shards as repair
from tests.test_key_usage_row_sharding import _auth_body, _seed
from trusted_router.storage_gcp_authorize import authorize_atomic, settle_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE


def state():
    ws = {"id": "ws", "owner_user_id": "owner", "unknown_future": "preserve"}
    credit = {"workspace_id": "ws", "shard_count": 1, "unknown_future": 123}
    key = {"hash": "key", "workspace_id": "ws", "usage_shard_count": 1, "include_byok_in_limit": True, "secret_hash": "inert-hash"}
    rows = [{"workspace_id": "ws", "shard": 0, "total_credits": 1000000, "total_usage": 100, "reserved": 200,
             **dict.fromkeys(repair.CREDIT_BALANCE_TRUST_COLUMNS), "billing_pause_causes": [], "pause_epoch": 3}]
    key_rows = [{"key_hash": "key", "shard": 0, "include_byok": True, "usage": 100, "byok_usage": 7,
                 "reserved": 0, "day_usage": 100, "week_usage": 100, "month_usage": 100}]
    owner = {"id": "owner", "owner_workspace_count": 1}
    return [ws, credit, key, rows, key_rows, owner, {"ws": credit}, 16]


@given(usage=st.integers(0, 10**12), reserved=st.integers(0, 10**12), free=st.integers(0, 10**12), target=st.integers(1, 64))
def test_expansion_conserves_every_integer_and_old_hold(usage, reserved, free, target):
    rows = state()[3]
    rows[0].update(total_credits=usage + reserved + free, total_usage=usage, reserved=reserved)
    before = copy.deepcopy(rows)
    expanded = repair.expand_credit_rows(rows, target)
    assert rows == before
    for name in ("total_credits", "total_usage", "reserved"):
        assert sum(int(row[name]) for row in expanded) == rows[0][name]
    assert expanded[0]["reserved"] == reserved
    assert expanded[0]["total_usage"] == usage
    assert all(row["total_credits"] >= row["total_usage"] + row["reserved"] for row in expanded)
    assert all(row["pause_epoch"] == 3 for row in expanded)
    assert repair.expand_credit_rows(expanded, target) == expanded


def test_plan_preserves_metadata_and_never_mutates_old_usage_or_holds():
    inputs = state()
    before = copy.deepcopy(inputs)
    plan = repair.make_plan(*inputs)
    assert inputs == before
    assert plan["credit"]["unknown_future"] == 123
    assert plan["key"]["secret_hash"] == before[2]["secret_hash"]
    changes = repair.mutations(plan)
    assert {change[next(iter(change))]["table"] for change in changes} == {"tr_credit_balance", "tr_key_limit", "tr_entities"}
    assert "delete" not in str(changes)
    old_credit_update = changes[0]["update"]
    assert old_credit_update["columns"] == ["workspace_id", "shard", "total_credits", "updated_at"]
    new_keys = changes[2]["insert"]
    assert all(int(row[1]) >= 1 for row in new_keys["values"])
    assert "usage" not in new_keys["columns"]
    assert "reserved" not in new_keys["columns"]


@pytest.mark.parametrize("cap", repair.CAP_FIELDS)
def test_key_cap_refuses_all_mutations(cap):
    inputs = state()
    inputs[2][cap] = 1000000
    with pytest.raises(ValueError, match="uncapped"):
        repair.make_plan(*inputs)


@pytest.mark.parametrize("defect", ["paused", "credit_pause", "wrong_workspace", "wrong_key", "key_hold", "typed_cap", "negative_usage", "inventory", "missing_inventory", "extra_shard", "bad_credit", "federated", "consolidation", "trust_divergence"])
def test_unsafe_state_fails_closed(defect):
    inputs = state()
    ws, credit, key, rows, key_rows, owner, inventory, _ = inputs
    if defect == "paused":
        ws["billing_pause_causes"] = ["fraud"]
    elif defect == "credit_pause":
        rows[0]["billing_pause_causes"] = ["fraud"]
    elif defect == "wrong_workspace":
        key["workspace_id"] = "other"
    elif defect == "wrong_key":
        key_rows[0]["key_hash"] = "other"
    elif defect == "key_hold":
        key_rows[0]["reserved"] = 1
    elif defect == "typed_cap":
        key_rows[0]["limit_micro"] = 1
    elif defect == "negative_usage":
        key_rows[0]["usage"] = -1
    elif defect == "inventory":
        owner["owner_workspace_count"] = 2
    elif defect == "missing_inventory":
        inventory.clear()
    elif defect == "extra_shard":
        rows.append({**rows[0], "shard": 1})
    elif defect == "bad_credit":
        rows[0]["total_credits"] = 0
    elif defect == "federated":
        ws["federated_home"] = "remote"
    elif defect == "consolidation":
        inputs[-1] = 0
    elif defect == "trust_divergence":
        credit["shard_count"] = 2
        rows.append({**rows[0], "shard": 1, "pause_epoch": 4})
    with pytest.raises(ValueError):
        repair.make_plan(*inputs)


def test_idempotent_rerun_does_not_rebalance_again():
    inputs = state()
    plan = repair.make_plan(*inputs)
    inputs[1] = plan["credit"]
    inputs[2] = plan["key"]
    inputs[3] = plan["credit_rows"]
    inputs[4] += [{**inputs[4][0], "shard": n, "usage": 0, "byok_usage": 0, "day_usage": 0, "week_usage": 0, "month_usage": 0} for n in range(1, 16)]
    inputs[6] = {"ws": inputs[1]}
    repeated = repair.make_plan(*inputs)
    assert not repeated["changed"]
    assert repair.mutations(repeated) == []


def test_aborted_commit_recomputes_entire_plan_and_timeout_is_not_retried(monkeypatch):
    plans = []
    class Client:
        commits = 0
        rollbacks = 0
        def begin_transaction(self, **kwargs):
            assert kwargs["retry"] is None
            return SimpleNamespace(id=b"tx")
        def commit(self, **kwargs):
            self.commits += 1
            assert kwargs["retry"] is None
            if self.commits == 1:
                raise Aborted("concurrent settlement")
        def rollback(self, **kwargs):
            self.rollbacks += 1
    def plan(*args):
        inputs = state()
        inputs[3][0]["total_usage"] += len(plans)
        result = repair.make_plan(*inputs)
        plans.append(result)
        return result
    monkeypatch.setattr(repair.Reader, "plan", plan)
    client = Client()
    result = repair.execute(client, "session", "ws", "key", 16, apply=True)
    assert result["totals"]["total_usage"] == 101
    assert client.commits == 2 and client.rollbacks == 0
    def timeout(**kwargs):
        raise DeadlineExceeded("uncertain commit")
    client.commit = timeout
    with pytest.raises(DeadlineExceeded):
        repair.execute(client, "session", "ws", "key", 16, apply=True)
    assert len(plans) == 3


@pytest.mark.parametrize("refund_old", [False, True])
def test_real_billing_primitives_settle_old_holds_and_new_shards_exactly_once(refund_old):
    store, database, key = _seed(key_shards=1)
    def authorize(index, shard):
        return authorize_atomic(store._database, store._param_types,
            workspace_id=key.workspace_id, key_hash=key.hash, estimate=1000,
            has_credit_candidate=True, reservation_usage_type="Credits",
            idempotency_scope=f"expand-{index}", idempotency_fingerprint="same",
            expires_at="2026-12-01T00:00:00Z", build_auth_body=_auth_body,
            credit_shard=shard, key_shard_candidates=(shard,), skip_key_limit=True)
    old = [authorize(index, 0) for index in range(3)]
    rows = list(database.typed[CREDIT_BALANCE_TABLE].values())
    expanded = repair.expand_credit_rows(rows, 16)
    inputs = state()
    inputs[0] = {"id": key.workspace_id, "owner_user_id": "owner"}
    inputs[1] = {"workspace_id": key.workspace_id, "shard_count": 1}
    inputs[2] = dataclasses.asdict(key)
    inputs[3] = rows
    inputs[4] = list(database.typed[KEY_LIMIT_TABLE].values())
    inputs[6] = {key.workspace_id: inputs[1]}
    plan = repair.make_plan(*inputs)
    # Apply the generated mutations through the store's transaction API.
    def apply(tx):
        for change in repair.mutations(plan):
            body = next(iter(change.values()))
            table, columns = body["table"], body["columns"]
            values = copy.deepcopy(body["values"])
            for row in values:
                for n, column in enumerate(columns):
                    if column in {"shard", "total_credits", "total_usage", "reserved", "pause_epoch", "trust_tier", "trust_override_tier"} and row[n] is not None:
                        row[n] = int(row[n])
                    if row[n] == "spanner.commit_timestamp()":
                        row[n] = None
            tx.insert_or_update(table=table, columns=columns, values=values)
    database.run_in_transaction(apply)
    assert sum(row["reserved"] for row in expanded) == 3000
    new = [authorize(10 + shard, shard) for shard in range(16)]
    for result in old + new:
        assert result["outcome"] == "accepted"
        refund = refund_old and result in old
        for _ in range(2):
            settled = settle_atomic(store._database, store._param_types,
                reservation_id=result["reservation_id"], actual_micro=0 if refund else 900,
                settled_usage_type="Credits", success=not refund)
            assert settled["outcome"] in {"settled", "already_settled"}
    expected = (16 if refund_old else 19) * 900
    assert sum(row["total_usage"] for row in database.typed[CREDIT_BALANCE_TABLE].values()) == expected
    assert sum(row["usage"] for row in database.typed[KEY_LIMIT_TABLE].values()) == expected
    assert sum(row["reserved"] for row in database.typed[CREDIT_BALANCE_TABLE].values()) == 0
    assert len([row for row in database.typed[KEY_LIMIT_TABLE].values() if row["usage"] > 0]) == 16
    assert json.loads(database.rows[("credit", key.workspace_id)].body)["shard_count"] == 16


def test_read_contract_is_exact_key_bounded_and_uses_same_transaction():
    from google.cloud.spanner_v1.types import ResultSet

    calls = []
    class Client:
        def read(self, **kwargs):
            calls.append(kwargs)
            return ResultSet(rows=[["ws", "0", "123"]])
    reader = repair.Reader(Client(), "session", {"id": b"tx"}, repair.time.monotonic() + 10)
    rows = reader.read("tr_credit_balance", ("workspace_id", "shard", "total_credits"), [["ws", "0"]])
    assert rows == [{"workspace_id": "ws", "shard": "0", "total_credits": "123"}]
    call = calls[0]
    assert call["retry"] is None and call["timeout"] <= 5
    assert call["request"]["transaction"] == {"id": b"tx"}
    assert call["request"]["key_set"] == {"keys": [["ws", "0"]]}
    assert call["request"]["request_options"]["priority"] == "PRIORITY_LOW"


def test_dry_run_never_commits_or_rolls_back(monkeypatch):
    class Client:
        def begin_transaction(self, **kwargs):
            assert kwargs["request"]["options"] == {"read_only": {"strong": True}}
            return SimpleNamespace(id=b"read")
        def commit(self, **kwargs):
            pytest.fail("dry-run tried to commit")
        def rollback(self, **kwargs):
            pytest.fail("dry-run tried to mutate transaction state")
    monkeypatch.setattr(repair.Reader, "plan", lambda *args: repair.make_plan(*state()))
    result = repair.execute(Client(), "session", "ws", "key", 16, apply=False)
    assert result["changed"] and not result["applied"]
    assert "secret_hash" not in json.dumps(result)


def test_transaction_reads_key_before_credit_to_match_settlement(monkeypatch):
    from google.cloud.spanner_v1.types import ResultSet

    inputs = state()
    ws, credit, key, credit_rows, key_rows, owner, _, _ = inputs
    entities = {("workspace", "ws"): ws, ("credit", "ws"): credit, ("api_key", "key"): key, ("user", "owner"): owner}
    class Client:
        def execute_sql(self, **kwargs):
            assert "WHERE owner_user_id=@owner" in kwargs["request"]["sql"]
            assert "LIMIT 33" in kwargs["request"]["sql"]
            return ResultSet(rows=[["ws"]])
    reader = repair.Reader(Client(), "s", {"id": b"tx"}, repair.time.monotonic() + 10)
    monkeypatch.setattr(reader, "entities", lambda keys: {tuple(k): entities[tuple(k)] for k in keys})
    tables = []
    def read(table, columns, keys):
        tables.append(table)
        assert len(keys) == 64
        assert len({tuple(k) for k in keys}) == 64
        return key_rows if table == "tr_key_limit" else credit_rows
    monkeypatch.setattr(reader, "read", read)
    assert reader.plan("ws", "key", 16)["changed"]
    assert tables == ["tr_key_limit", "tr_credit_balance"]
