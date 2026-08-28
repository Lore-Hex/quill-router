"""Contract for draining the operational outbox to TWO ClickHouse nodes.

The AWS cloud's analytics history lives on ClickHouse EBS volumes. With one
node that is one volume holding every operational row the cloud has ever
produced. This is a DURABILITY property, not an availability one -- the gateway
never reads ClickHouse -- so what is pinned here is not "a node can be down"
but "a row exists in two places before anything forgets it".

The mechanism is the at-least-once property the drain already had, extended:

    SELECT batch -> write EVERY node -> DELETE the outbox rows

with the DELETE gated on ALL of them. Three things must therefore hold, and
each is a test below rather than a comment in a design doc:

* a partial write deletes NOTHING, whichever node failed;
* redelivery after a partial write is harmless, which is true only because the
  tables are ReplacingMergeTree keyed on `ingest_version` and redelivery
  carries the SAME `ingest_version`;
* a deployment with one endpoint configured is byte-identical to the one that
  existed before any of this -- asserted against the literal argv, because
  "no behaviour change" is otherwise unfalsifiable.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import clickhouse.ingest_operational_outbox as writer_module
import clickhouse.ingest_operational_outbox_postgres as drain_module
from clickhouse.ingest_operational_outbox import (
    ClickHouseOperationalWriter,
    OperationalOutboxRow,
    normalise_operational_event,
)
from clickhouse.ingest_operational_outbox_postgres import (
    ClickHouseTarget,
    FanOutOperationalWriter,
    FanOutWriteError,
    build_operational_writer,
    clickhouse_targets_from_env,
    degraded_target_names,
    drain_once,
    drain_shard_once,
)
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import (
    ACTIVITY_EVENT_KIND,
    activity_payload,
    operational_analytics_shard,
)
from trusted_router.types import UsageType

ROOT = Path(__file__).resolve().parents[1]

#: The argv the drain has emitted since before remote endpoints existed. A
#: single-endpoint deployment must still emit exactly this, so it is written
#: out as a literal rather than derived from the code under test.
HISTORICAL_COMMAND = [
    "/usr/bin/clickhouse-client",
    "--user",
    "tr",
    "--database",
    "tr",
    "--query",
    "INSERT INTO activity_generations FORMAT JSONEachRow",
]

STOCKHOLM = "10.60.1.7"

DUAL_ENV = {
    "CH_PASSWORD": "paris-secret",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "default",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "default",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_NAME": "stockholm",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST": STOCKHOLM,
    "CH_REPLICA_PASSWORD": "stockholm-secret",
}


# --------------------------------------------------------------------------
# A fleet of fake ClickHouse nodes, addressed the way clickhouse-client does
# --------------------------------------------------------------------------


@dataclass
class _Call:
    command: list[str]
    payload: bytes
    timeout: float | None

    @property
    def host(self) -> str:
        """Which node this call went to; "local" when no --host was passed."""
        if "--host" not in self.command:
            return "local"
        return self.command[self.command.index("--host") + 1]

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.payload.decode().splitlines() if line]


@dataclass
class _Fleet:
    """Stands in for `subprocess.run`, one entry per node.

    `stored()` models ReplacingMergeTree(ingest_version): rows are keyed on the
    table's sort key and the highest `ingest_version` wins. That collapse is
    what makes redelivery safe, so the fake has to implement it or the
    redelivery test would prove nothing. `test_the_replacing_engine_backing_this_fake_is_real`
    below pins the model to the checked-in DDL.
    """

    down: set[str] = field(default_factory=set)
    calls: list[_Call] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(command: list[str], **kwargs: Any) -> Any:
            call = _Call(
                command=list(command),
                payload=kwargs.get("input", b"") or b"",
                timeout=kwargs.get("timeout"),
            )
            self.calls.append(call)
            if call.host in self.down:
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Connection refused")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(writer_module.subprocess, "run", run)

    def accepted(self, host: str) -> list[_Call]:
        return [call for call in self.calls if call.host == host and host not in self.down]

    def stored(self, host: str) -> list[dict[str, Any]]:
        collapsed: dict[tuple[Any, ...], dict[str, Any]] = {}
        for call in self.accepted(host):
            query = call.command[call.command.index("--query") + 1]
            if "INSERT INTO activity_generations" not in query:
                continue
            for row in call.rows:
                # activity_generations ORDER BY (tenant_id, created_at, generation_id)
                key = (row["tenant_id"], row["created_at"], row["generation_id"])
                previous = collapsed.get(key)
                if previous is None or row["ingest_version"] >= previous["ingest_version"]:
                    collapsed[key] = row
        return list(collapsed.values())


# --------------------------------------------------------------------------
# Outbox fake
# --------------------------------------------------------------------------


def _generation(generation_id: str) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req-{generation_id}",
        workspace_id="ws-private-123",
        key_hash="salted-key-hash-private",
        model="anthropic/claude-haiku-4.5",
        provider="anthropic",
        provider_name="Anthropic",
        app="Test app",
        tokens_prompt=12,
        tokens_completion=3,
        total_cost_microdollars=9,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=7.5,
        finish_reason="stop",
        status="success",
        streamed=True,
        usage_estimated=False,
        created_at="2026-07-31T12:34:56.789Z",
    )


def _row(event_id: str, *, shard: int | None = None) -> OperationalOutboxRow:
    return OperationalOutboxRow(
        shard=(
            operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{event_id}")
            if shard is None
            else shard
        ),
        commit_ts=dt.datetime(2026, 7, 31, 12, 35, tzinfo=dt.UTC),
        event_kind=ACTIVITY_EVENT_KIND,
        event_id=event_id,
        payload=json.dumps(activity_payload(_generation(event_id))),
    )


class _Source:
    """The outbox table, keyed and ORDERED exactly as Postgres orders it.

    `fetch_shard` models SELECT_SHARD_BATCH_SQL literally -- `WHERE shard = %s
    ORDER BY event_kind, event_id LIMIT %s`, optionally resumed past a key --
    because the defect this file now pins is a property of that statement: it
    has no offset, so the DELETE is the only thing that advances it.
    """

    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = list(rows)
        self.delete_calls: list[list[OperationalOutboxRow]] = []

    def fetch_shard(
        self,
        shard: int,
        *,
        limit: int,
        after: tuple[str, str] | None = None,
    ) -> list[OperationalOutboxRow]:
        candidates = [row for row in self.rows if row.shard == shard]
        if after is not None:
            candidates = [
                row for row in candidates if (row.event_kind, row.event_id) > after
            ]
        candidates.sort(key=lambda row: (row.event_kind, row.event_id))
        return candidates[:limit]

    def delete(self, rows: list[OperationalOutboxRow]) -> int:
        self.delete_calls.append(list(rows))
        keys = {(row.shard, row.event_kind, row.event_id) for row in rows}
        before = len(self.rows)
        self.rows = [
            row for row in self.rows if (row.shard, row.event_kind, row.event_id) not in keys
        ]
        return before - len(self.rows)


def _dual_writer() -> Any:
    return build_operational_writer(clickhouse_targets_from_env(DUAL_ENV))


def _event(event_id: str = "gen-1") -> Any:
    [event] = normalise_operational_event(_row(event_id))
    return event


# --------------------------------------------------------------------------
# 1. Single endpoint: nothing changed
# --------------------------------------------------------------------------


def test_one_configured_endpoint_yields_one_target_and_no_fan_out() -> None:
    """The default environment is the deployment that already exists."""
    targets = clickhouse_targets_from_env({"CH_PASSWORD": "x"})

    assert [target.name for target in targets] == ["primary"]
    assert targets[0].user == "tr"
    assert targets[0].database == "tr"
    assert targets[0].host == ""
    # A fan-out of one would be transparent in behaviour but not in exception
    # type or log text. "No behaviour change" has to mean none.
    assert isinstance(build_operational_writer(targets), ClickHouseOperationalWriter)


def test_single_endpoint_emits_the_byte_identical_historical_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this whole feature could most easily cause.

    Connection flags are omitted rather than defaulted precisely so that a
    writer with no host emits the argv it always did. Asserting against the
    literal is the only way that claim can fail loudly.
    """
    fleet = _Fleet()
    fleet.install(monkeypatch)

    writer = build_operational_writer(clickhouse_targets_from_env({"CH_PASSWORD": "x"}))
    writer.insert([_event()])

    assert [call.command for call in fleet.calls] == [HISTORICAL_COMMAND]
    # And no timeout, which is also how it always behaved: a bound belongs on
    # a REMOTE endpoint, and adding one here would be a behaviour change.
    assert fleet.calls[0].timeout is None


def test_single_endpoint_keeps_todays_exception_rather_than_a_fan_out_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet(down={"local"})
    fleet.install(monkeypatch)

    writer = build_operational_writer(clickhouse_targets_from_env({"CH_PASSWORD": "x"}))

    with pytest.raises(RuntimeError, match="ClickHouse activity insert failed"):
        writer.insert([_event()])


# --------------------------------------------------------------------------
# 2. Two endpoints: configuration
# --------------------------------------------------------------------------


def test_the_second_endpoint_is_purely_additive() -> None:
    """Adding Stockholm must not move anything the control plane reads.

    `_USER` / `_DATABASE` keep describing the endpoint they always described,
    and the replica is a separate block underneath the same prefix.
    """
    targets = clickhouse_targets_from_env(DUAL_ENV)

    assert [target.name for target in targets] == ["primary", "stockholm"]
    paris, stockholm = targets
    assert (paris.host, paris.user, paris.database) == ("", "default", "default")
    assert stockholm.host == STOCKHOLM
    assert stockholm.port == 9000
    # Its OWN credential, not the primary's: two independent installations.
    assert stockholm.password == "stockholm-secret"  # noqa: S105 - test stub
    assert paris.password == "paris-secret"  # noqa: S105 - test stub
    # A remote endpoint is bounded; the local one is not (see above).
    assert stockholm.timeout_seconds == 60.0
    assert paris.timeout_seconds is None


def test_each_endpoint_can_carry_its_own_user_and_database() -> None:
    targets = clickhouse_targets_from_env(
        {
            **DUAL_ENV,
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_USER": "tr_drain",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_DATABASE": "tr",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_PORT": "9440",
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_SECURE": "true",
        }
    )

    stockholm = targets[1]
    assert (stockholm.user, stockholm.database) == ("tr_drain", "tr")
    assert (stockholm.port, stockholm.secure) == (9440, True)


def test_a_third_endpoint_is_configuration_not_code() -> None:
    targets = clickhouse_targets_from_env(
        {
            **DUAL_ENV,
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_2_HOST": "10.70.1.9",
            "CH_REPLICA_2_PASSWORD": "third-secret",
        }
    )

    assert [target.name for target in targets] == ["primary", "stockholm", "replica_2"]
    assert isinstance(build_operational_writer(targets), FanOutOperationalWriter)


def test_a_replica_missing_its_password_fails_at_startup_not_mid_batch() -> None:
    """Learning the credentials are wrong after a batch has been read is the
    worst possible moment; the same lesson as the --user/--database fix."""
    env = dict(DUAL_ENV)
    del env["CH_REPLICA_PASSWORD"]

    with pytest.raises(ValueError, match="CH_REPLICA_PASSWORD is required"):
        clickhouse_targets_from_env(env)


def test_a_replica_pointed_at_this_machine_is_refused() -> None:
    """Two copies on one EBS volume are one copy that reports as two."""
    for host in ("localhost", "127.0.0.1", "::1"):
        with pytest.raises(ValueError, match="not a second copy"):
            clickhouse_targets_from_env(
                {**DUAL_ENV, "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST": host}
            )


def test_a_replica_pointed_at_this_machines_own_private_ip_is_refused() -> None:
    """REGRESSION. The same-machine guard used to match NAMES only.

    Nothing in the deployment addresses ClickHouse by name -- it listens on the
    private IP and the provisioning script's instructions paste one -- so the
    realistic form of this mistake is pasting the LOCAL node's private address
    as the replica host. That sailed past a `localhost`/`127.0.0.1` allowlist
    and produced two writes to one EBS volume, logged as `copies=2`: the exact
    "one copy that reports as two" the guard exists to make unrepresentable.
    """
    own = "10.42.7.9"

    with pytest.raises(ValueError, match="not a second copy"):
        clickhouse_targets_from_env(
            {**DUAL_ENV, "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST": own},
            local_hosts={own},
        )

    # A genuinely remote address is still accepted.
    targets = clickhouse_targets_from_env(DUAL_ENV, local_hosts={own})
    assert [target.host for target in targets] == ["", STOCKHOLM]


def test_the_own_address_check_reaches_a_real_local_address() -> None:
    """Pins the default detector, not just the injected one.

    `local_hosts` is injectable so the tests need no sockets, which would be a
    fine way to test a guard that never actually runs. 127.0.0.1 is bindable on
    any machine this suite runs on, so the real implementation is exercised.
    """
    assert drain_module._is_own_address("127.0.0.1") is True
    # Not an address of this machine, and not a resolvable name either: the
    # check must not fall back to DNS.
    assert drain_module._is_own_address("10.60.1.7") is False
    assert drain_module._is_own_address("clickhouse.invalid") is False


def test_replica_settings_without_a_replica_host_are_refused() -> None:
    """REGRESSION. Validation used to run in one direction only.

    `_HOST` without a password was fatal; a password (or name, or port) without
    a `_HOST` was skipped in silence. The drain then built ONE target, logged
    `copies=1 degraded_targets=-` -- indistinguishable from a deliberate
    single-node deployment -- and, because one target IS deletable, began
    deleting rows the second node never received. One misspelled variable in a
    hand-edited env file was enough, and the only symptom was `copies=1`.
    """
    base = {
        "CH_PASSWORD": "paris-secret",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "default",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "default",
    }
    orphaned = [
        # A password for a replica that was never given a host.
        {"CH_REPLICA_PASSWORD": "stockholm-secret"},
        {"TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_NAME": "stockholm"},
        {"TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_PORT": "9000"},
        # Misspellings of the one load-bearing variable.
        {
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOSTS": STOCKHOLM,
            "CH_REPLICA_PASSWORD": "stockholm-secret",
        },
        {
            "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA1_HOST": STOCKHOLM,
            "CH_REPLICA1_PASSWORD": "stockholm-secret",
        },
    ]
    for extra in orphaned:
        with pytest.raises(ValueError) as failure:
            clickhouse_targets_from_env({**base, **extra})
        assert "single-node" in str(failure.value), extra

    # A blank host is the same mistake with whitespace on it.
    with pytest.raises(ValueError, match="single-node"):
        clickhouse_targets_from_env(
            {
                **base,
                "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST": "   ",
                "CH_REPLICA_PASSWORD": "stockholm-secret",
            }
        )


def test_an_environment_with_no_replica_settings_at_all_is_still_one_target() -> None:
    """The guard above must not make a single-node deployment un-startable."""
    targets = clickhouse_targets_from_env({"CH_PASSWORD": "x", "CH_PASSWORD_FILE": "/x"})

    assert [target.name for target in targets] == ["primary"]


def test_endpoints_may_not_share_a_name() -> None:
    """Names are how the backlog alarm says WHICH copy is behind."""
    with pytest.raises(ValueError, match="must be unique"):
        clickhouse_targets_from_env(
            {**DUAL_ENV, "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_NAME": "primary"}
        )


def test_target_description_never_leaks_the_password() -> None:
    for target in clickhouse_targets_from_env(DUAL_ENV):
        assert target.password not in target.describe()


# --------------------------------------------------------------------------
# 3. Two endpoints: the write itself
# --------------------------------------------------------------------------


def test_dual_write_happy_path_puts_the_batch_on_both_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet()
    fleet.install(monkeypatch)
    row = _row("gen-happy")
    source = _Source([row])

    result = drain_shard_once(source, _dual_writer(), shard=row.shard, batch_size=10)

    assert result.fetched == 1
    assert {call.host for call in fleet.calls} == {"local", STOCKHOLM}
    assert len(fleet.stored("local")) == 1
    assert len(fleet.stored(STOCKHOLM)) == 1
    # Both nodes received the SAME row, not two projections of it.
    assert fleet.accepted("local")[0].payload == fleet.accepted(STOCKHOLM)[0].payload


def test_both_succeeding_deletes_the_rows_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet()
    fleet.install(monkeypatch)
    rows = [_row(f"gen-once-{index}", shard=4) for index in range(3)]
    source = _Source(rows)

    result = drain_shard_once(source, _dual_writer(), shard=4, batch_size=10)

    assert result.deleted == 3
    assert source.rows == []
    assert len(source.delete_calls) == 1
    assert len(source.delete_calls[0]) == 3


def test_stockholm_failing_deletes_nothing_and_the_rows_redeliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node B down: Paris has the row, the outbox still has the row."""
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    row = _row("gen-b-down")
    source = _Source([row])
    writer = _dual_writer()

    with pytest.raises(FanOutWriteError) as failure:
        drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    assert failure.value.failed_targets == ["stockholm"]
    assert failure.value.succeeded_targets == ["primary"]
    assert source.delete_calls == []
    assert source.rows == [row]
    assert degraded_target_names(writer) == ["stockholm"]

    # Stockholm returns; the ordinary sweep delivers the backlog. Nothing
    # special happened -- the row was never deleted, so it was never lost.
    fleet.down.clear()
    drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    assert source.rows == []
    assert len(fleet.stored(STOCKHOLM)) == 1
    assert degraded_target_names(writer) == []


def test_paris_failing_deletes_nothing_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetric: no endpoint is privileged, including the local one."""
    fleet = _Fleet(down={"local"})
    fleet.install(monkeypatch)
    row = _row("gen-a-down")
    source = _Source([row])
    writer = _dual_writer()

    with pytest.raises(FanOutWriteError) as failure:
        drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    assert failure.value.failed_targets == ["primary"]
    assert source.delete_calls == []
    assert source.rows == [row]


def test_a_failing_node_does_not_starve_the_healthy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every target is attempted, even after an earlier one fails.

    Short-circuiting on the first failure would be safe -- nothing is deleted
    either way -- but it would delay the healthy node's copy of THIS batch for
    no reason. (Keeping the healthy node current ACROSS sweeps is a different
    mechanism entirely; see the starvation test below.)
    """
    fleet = _Fleet(down={"local"})
    fleet.install(monkeypatch)

    with pytest.raises(FanOutWriteError):
        _dual_writer().insert([_event("gen-still-tried")])

    assert {call.host for call in fleet.calls} == {"local", STOCKHOLM}
    assert len(fleet.stored(STOCKHOLM)) == 1


def test_a_down_replica_does_not_freeze_the_read_window_on_the_healthy_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION. A down Stockholm used to stop Paris receiving ANYTHING new.

    The batch SELECT has no offset, so the DELETE is the only thing that
    advances it. While Stockholm failed nothing was deleted, so every sweep
    re-read and re-wrote the identical lowest-ordered `batch_size` rows: rows
    sorting above that window reached NO node, and Paris -- up, healthy, and
    holding the only copy of this cloud's history -- went permanently stale.
    Measured before the fix: over 40 sweeps of a 1500-row backlog at
    batch_size=500, Paris saw 500 distinct rows out of 1500 and 20,000
    row-inserts, i.e. 40x write amplification for zero new information.

    The fix is the read cursor: a batch that could not be deleted is stepped
    over, so the rest of the shard still reaches whichever nodes are up. The
    rows are NOT forgotten -- nothing deleted them, and the cursor resets at
    the end of the shard, which is what re-offers them once Stockholm returns.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    backlog = [_row(f"gen-{index:04d}", shard=0) for index in range(6)]
    source = _Source(backlog)
    writer = _dual_writer()
    cursors: dict[int, tuple[str, str]] = {}

    for _ in range(4):
        drain_once(source, writer, batch_size=2, shard_count=1, cursors=cursors)

    delivered = {row["generation_id"] for row in fleet.stored("local")}
    assert delivered == {f"gen-{index:04d}" for index in range(6)}, (
        "the healthy node must reach the whole shard, not just the first batch"
    )
    # And none of it was acknowledged: Stockholm never got a row, so the outbox
    # still holds every one of them.
    assert len(source.rows) == 6
    assert source.delete_calls == []


def test_the_cursor_never_lets_a_row_be_deleted_for_only_one_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stepping over a batch must not be mistaken for delivering it.

    The cursor exists to keep the healthy node current. If advancing it also
    let the rows be deleted, it would be the exact data-loss the whole design
    prevents -- so this pins that a full pass with a node down deletes nothing
    and every row is still queued at the end of it.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    backlog = [_row(f"gen-keep-{index:04d}", shard=0) for index in range(6)]
    source = _Source(backlog)
    cursors: dict[int, tuple[str, str]] = {}

    for _ in range(6):
        drain_once(source, _dual_writer(), batch_size=2, shard_count=1, cursors=cursors)

    assert source.delete_calls == []
    assert {row.event_id for row in source.rows} == {
        f"gen-keep-{index:04d}" for index in range(6)
    }


def test_the_backlog_is_delivered_and_deleted_once_the_node_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor's other half: stepping over a batch is not abandoning it.

    A cursor that only ever advanced would leave the rows it skipped queued
    forever. It resets at the end of the shard, so the next pass re-offers the
    whole backlog -- and when Stockholm is back, that pass delivers and deletes.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    backlog = [_row(f"gen-back-{index:04d}", shard=0) for index in range(6)]
    source = _Source(backlog)
    writer = _dual_writer()
    cursors: dict[int, tuple[str, str]] = {}

    for _ in range(4):
        drain_once(source, writer, batch_size=2, shard_count=1, cursors=cursors)
    assert len(source.rows) == 6

    fleet.down.clear()
    for _ in range(6):
        drain_once(source, writer, batch_size=2, shard_count=1, cursors=cursors)

    assert source.rows == []
    assert len(fleet.stored(STOCKHOLM)) == 6
    assert len(fleet.stored("local")) == 6


def test_a_poison_batch_is_quarantined_and_does_not_block_its_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same root cause, non-ClickHouse trigger.

    A payload this build cannot normalise is written to the quarantine table
    and deleted with the healthy rows, so it cannot crash-loop the drainer.
    """
    fleet = _Fleet()
    fleet.install(monkeypatch)
    poison = OperationalOutboxRow(
        shard=0,
        commit_ts=dt.datetime(2026, 7, 31, 12, 35, tzinfo=dt.UTC),
        event_kind=ACTIVITY_EVENT_KIND,
        event_id="gen-0000-poison",
        payload=json.dumps({"generation_id": "missing-every-other-column"}),
    )
    healthy = [_row(f"gen-{index:04d}", shard=0) for index in range(1, 4)]
    source = _Source([poison, *healthy])
    cursors: dict[int, tuple[str, str]] = {}

    for _ in range(8):
        drain_once(source, _dual_writer(), batch_size=1, shard_count=1, cursors=cursors)

    delivered = {row["generation_id"] for row in fleet.stored("local")}
    assert delivered == {f"gen-{index:04d}" for index in range(1, 4)}
    assert source.rows == []
    quarantine_calls = [
        call
        for call in fleet.accepted("local")
        if "INSERT INTO operational_outbox_quarantine"
        in call.command[call.command.index("--query") + 1]
    ]
    assert quarantine_calls[0].rows[0]["event_id"] == "gen-0000-poison"


def test_a_hard_down_target_is_skipped_after_a_few_failures_in_one_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged node must not cost one timeout per shard.

    A remote write is bounded at 60s and issued once per target per event kind
    per shard. Retrying a node that has already failed repeatedly this sweep
    would make a single sweep 32 x 2 x 60s ~ 64 MINUTES, during which the
    healthy node receives nothing new and the backlog alarm -- the only bound
    on outbox growth -- is evaluated once.

    The skip is recorded as a FAILURE, never a success, so nothing is deleted
    for a node that did not receive the batch.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    rows = [_row(f"gen-wedged-{index}", shard=index) for index in range(12)]
    source = _Source(rows)
    writer = _dual_writer()

    result = drain_once(source, writer, batch_size=10, shard_count=12)

    attempts = len([call for call in fleet.calls if call.host == STOCKHOLM])
    assert attempts == drain_module.SWEEP_TARGET_FAILURE_LIMIT, (
        "a hard-down target must stop being dialled for the rest of the sweep"
    )
    # Every shard still failed -- a skipped target is an undelivered target.
    assert result.failed_shards == 12
    assert source.delete_calls == []
    assert len(source.rows) == 12
    # And the healthy node still got every shard: the breaker is per-target.
    assert len([call for call in fleet.calls if call.host == "local"]) == 12


def test_a_target_failing_most_shards_is_reported_degraded_for_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION. `degraded_targets` used to be last-write-wins.

    `consecutive_failures` is reset by ANY success, so a node that rejected 31
    shards and happened to accept the 32nd reported as healthy in the one field
    the runbook and the unit file both tell the operator to watch. Measured
    before the fix: 31 of 32 shards failed, `degraded_targets` logged `-`.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    shards = 3
    rows = [_row(f"gen-mostly-{index}", shard=index) for index in range(shards)]
    source = _Source(rows)
    writer = _dual_writer()

    # Stockholm rejects every shard but the LAST one of the sweep -- the shape
    # that made the old report say "healthy". Recovery happens before the final
    # shard, while the per-sweep breaker has seen fewer failures than its limit,
    # so the last shard really is attempted.
    seen = 0
    original = drain_module.drain_shard_once

    def recovering(*args: Any, **kwargs: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen >= shards:
            fleet.down.clear()
        return original(*args, **kwargs)

    monkeypatch.setattr(drain_module, "drain_shard_once", recovering)

    result = drain_once(source, writer, batch_size=10, shard_count=shards)

    assert result.failed_shards == shards - 1
    # The writer's own counter says "healthy" -- it was reset by the last
    # shard's success. The SWEEP is what the operator is asking about.
    assert degraded_target_names(writer) == []
    assert result.degraded_targets == ("stockholm",)


def test_both_nodes_failing_is_reported_as_both(monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = _Fleet(down={"local", STOCKHOLM})
    fleet.install(monkeypatch)
    writer = _dual_writer()

    with pytest.raises(FanOutWriteError) as failure:
        writer.insert([_event()])

    assert failure.value.failed_targets == ["primary", "stockholm"]
    assert failure.value.succeeded_targets == []
    assert sorted(degraded_target_names(writer)) == ["primary", "stockholm"]


def test_a_persistently_failing_node_keeps_the_backlog_rather_than_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named growth mode, made concrete.

    Stockholm stays down across many sweeps. Nothing is deleted, so the outbox
    grows -- which is the DESIGNED behaviour and the reason the drain logs
    `degraded_targets` and raises a backlog alarm on lag. What must never
    happen is the drain deciding on its own to delete undelivered rows.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    rows = [_row(f"gen-backlog-{index}", shard=index) for index in range(5)]
    source = _Source(rows)
    writer = _dual_writer()

    for _ in range(4):
        result = drain_once(source, writer, batch_size=10)
        assert result.failed_shards == 5

    assert source.delete_calls == []
    assert len(source.rows) == 5
    assert writer.consecutive_failures["stockholm"] == 20
    assert writer.consecutive_failures["primary"] == 0


# --------------------------------------------------------------------------
# 4. Redelivery is safe -- and WHY it is safe
# --------------------------------------------------------------------------


def test_a_redelivered_batch_does_not_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the whole design rests on.

    Paris accepted the batch, Stockholm did not, so nothing was deleted and the
    next sweep sends the SAME rows to Paris again. That is a duplicate insert
    into a table that already has the row.

    It is safe for exactly one reason: `activity_generations` is a
    ReplacingMergeTree whose version column is `ingest_version`, and
    `ingest_version` is derived from the outbox row's commit timestamp -- not
    from the ingest wall clock. So the redelivered row is byte-identical to the
    first, sorts to the same key, and collapses. Had `ingest_version` been
    "now", each redelivery would be a NEWER version of the same key, still
    collapsing to one row but silently re-dating history; had the engine been a
    plain MergeTree, every redelivery would permanently double-count.
    """
    fleet = _Fleet(down={STOCKHOLM})
    fleet.install(monkeypatch)
    row = _row("gen-redelivered")
    source = _Source([row])
    writer = _dual_writer()

    for _ in range(3):
        with pytest.raises(FanOutWriteError):
            drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    paris_calls = fleet.accepted("local")
    assert len(paris_calls) == 3, "Paris really was written three times"
    # Byte-identical, which is the precondition for the collapse.
    assert {call.payload for call in paris_calls} == {paris_calls[0].payload}
    assert len({call.rows[0]["ingest_version"] for call in paris_calls}) == 1
    # And the modelled ReplacingMergeTree collapses them to one.
    assert len(fleet.stored("local")) == 1


def test_the_replacing_engine_backing_this_fake_is_real() -> None:
    """Pins the fake's collapse model to the DDL the nodes actually run.

    The test above is only meaningful if the deployed tables really are
    ReplacingMergeTree keyed on ingest_version. If someone changes the engine,
    the redelivery test would keep passing against a fake that no longer
    describes production -- so assert the schema file directly.
    """
    schema = (ROOT / "clickhouse/006_operational_analytics_single_node.sql").read_text()
    # Comments stripped: the header explains at length why Keeper is NOT used
    # here, and the assertions below are about the statements, not the prose.
    ddl = "\n".join(
        line for line in schema.splitlines() if not line.strip().startswith("--")
    )

    assert ddl.count("ENGINE = ReplacingMergeTree(ingest_version)") == 5
    # Explicitly NOT the Replicated variant: two regions cannot form a Keeper
    # quorum, so a member loss would freeze writes on the survivor.
    assert "ReplicatedReplacingMergeTree" not in ddl
    assert "Keeper" not in ddl
    # The sort key the fake collapses on.
    assert "ORDER BY (tenant_id, created_at, generation_id)" in ddl


def test_the_single_node_schema_matches_the_replicated_one_column_for_column() -> None:
    """One drain, one JSONEachRow batch, two nodes: a column that exists on
    only one of them is a node that silently rejects half the traffic."""

    def columns(text: str) -> list[str]:
        found: list[list[str]] = []
        current: list[str] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("CREATE TABLE"):
                current = []
                continue
            if current is not None and stripped == ")":
                found.append(current)
                current = None
                continue
            if current is not None and stripped and not stripped.startswith(("(", "--")):
                current.append(stripped.split()[0])
        return [name for table in found for name in table]

    single = (ROOT / "clickhouse/006_operational_analytics_single_node.sql").read_text()
    replicated = (ROOT / "clickhouse/004_operational_analytics_replicated.sql").read_text()

    assert columns(single) == columns(replicated)
    assert columns(single), "the column extractor found nothing; the test would pass vacuously"


# --------------------------------------------------------------------------
# 5. The remote endpoint's own argv
# --------------------------------------------------------------------------


def test_the_remote_endpoint_is_addressed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _Fleet()
    fleet.install(monkeypatch)

    _dual_writer().insert([_event()])

    remote = next(call for call in fleet.calls if call.host == STOCKHOLM)
    assert remote.command[:3] == ["/usr/bin/clickhouse-client", "--host", STOCKHOLM]
    assert remote.command[remote.command.index("--port") + 1] == "9000"
    assert remote.command[remote.command.index("--user") + 1] == "default"
    assert remote.command[remote.command.index("--database") + 1] == "default"
    # A node that completes the handshake and then stalls must not wedge the
    # sweep forever inside a daemon that still looks alive.
    assert remote.timeout == 60.0
    assert "--secure" not in remote.command


def test_secure_adds_the_tls_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = _Fleet()
    fleet.install(monkeypatch)

    ClickHouseTarget(
        name="tls",
        password="x",  # noqa: S106 - test stub
        host="10.60.1.7",
        port=9440,
        secure=True,
    ).writer().insert([_event()])

    assert "--secure" in fleet.calls[0].command


def test_a_stalled_remote_node_raises_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hang would be the worst outcome: no error, no delete, no progress,
    and a lag metric nobody is looking at because the process is 'running'."""
    import subprocess

    def run(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(writer_module.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="timed out"):
        ClickHouseTarget(
            name="stalled",
            password="x",  # noqa: S106 - test stub
            host=STOCKHOLM,
            timeout_seconds=1.0,
        ).writer().insert([_event()])


# --------------------------------------------------------------------------
# 6. Fan-out construction
# --------------------------------------------------------------------------


def test_a_fan_out_with_no_targets_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FanOutOperationalWriter([])


def test_degraded_targets_is_empty_for_a_plain_writer() -> None:
    """main() asks any writer for its degraded targets, including the bare one."""
    assert degraded_target_names(ClickHouseOperationalWriter(password="x")) == []  # noqa: S106


# --------------------------------------------------------------------------
# 7. What the operator actually sees
#
# The outbox growing without bound is the designed failure mode, so the ONLY
# thing standing between it and a full database is that a human is told. These
# pin the telling.
# --------------------------------------------------------------------------


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    oldest: dt.datetime | None,
    argv: list[str] | None = None,
    once: bool = True,
) -> int:
    import clickhouse.ingest_operational_outbox_postgres as drain

    class _NoRows:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
            return []

        def delete(self, rows: list[OperationalOutboxRow]) -> int:
            return 0

        def oldest_enqueued_at(self) -> dt.datetime | None:
            return oldest

    monkeypatch.setattr(drain, "PostgresOperationalOutboxSource", _NoRows)
    for key in list(os.environ):
        if key.startswith("TR_OPERATIONAL_ANALYTICS_") or key.startswith("CH_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drain",
            *(["--once"] if once else []),
            "--dsn",
            "host=db.example",
            *(argv or []),
        ],
    )
    return drain.main()


def test_a_misconfigured_replica_stops_the_drain_before_it_reads_anything(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("ERROR")
    env = dict(DUAL_ENV)
    del env["CH_REPLICA_PASSWORD"]

    with pytest.raises(SystemExit) as exit_info:
        _run_main(monkeypatch, env, oldest=None)

    # A CONFIG exit, not a generic one: the unit's RestartPreventExitStatus
    # keys off it so a bad environment stops the unit visibly instead of
    # crash-looping every RestartSec with nothing left running to alarm.
    assert exit_info.value.code == drain_module.CONFIG_EXIT_CODE
    assert "CH_REPLICA_PASSWORD is required" in caplog.text


def test_the_configured_copies_are_logged_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """"How many copies is this actually keeping?" must be answerable from the
    log, not from reading the environment file over someone's shoulder."""
    caplog.set_level("INFO")

    _run_main(monkeypatch, DUAL_ENV, oldest=None)

    startup = next(r for r in caplog.records if "outbox.targets" in r.getMessage())
    assert "copies=2" in startup.getMessage()
    assert "stockholm@10.60.1.7:9000/default" in startup.getMessage()
    assert "stockholm-secret" not in startup.getMessage()


def test_a_long_backlog_raises_an_alarm_naming_the_action(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bound on outbox growth is this log line plus an operator.

    There is deliberately no automatic bound -- the only two available are
    "delete rows a copy never received" and "stop draining" -- so if this line
    does not fire, nothing does.
    """
    caplog.set_level("INFO")
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(hours=9)

    _run_main(monkeypatch, DUAL_ENV, oldest=stale)

    alarms = [r for r in caplog.records if "backlog_alarm" in r.getMessage()]
    assert len(alarms) == 1
    assert alarms[0].levelname == "ERROR"
    assert "action=restore-the-node-or-drop-it-from-the-drain-config" in alarms[0].getMessage()


def test_an_alarm_threshold_that_disables_the_alarm_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION. `lag >= max > 0` silently disabled the alarm at 0.

    That log line is the ONLY bound on outbox growth, so a value that turns it
    off is a configuration error rather than a setting -- and one that would
    otherwise be discovered when the operational database filled up.
    """
    for value in ("0", "-1"):
        with pytest.raises(SystemExit) as exit_info:
            _run_main(
                monkeypatch, DUAL_ENV, oldest=None, argv=["--max-lag-seconds", value]
            )
        assert exit_info.value.code == drain_module.CONFIG_EXIT_CODE


def test_the_drain_reuses_one_read_cursor_across_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor only helps if it survives the sweep that created it.

    A cursor rebuilt per sweep would step over the stuck batch and then forget
    it had, leaving the healthy node exactly as starved as before. `main()`
    therefore owns it, and this pins that the same object reaches every sweep.
    """
    seen: list[int] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(id(kwargs["cursors"]))
        if len(seen) >= 3:
            raise SystemExit(0)
        return drain_module.SweepResult(fetched=0, inserted=0, rows_per_second=0.0)

    monkeypatch.setattr(drain_module, "drain_once", recording)
    monkeypatch.setattr(drain_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit):
        _run_main(monkeypatch, DUAL_ENV, oldest=None, once=False)

    assert len(seen) == 3
    assert len(set(seen)) == 1, "each sweep must resume the previous sweep's cursor"


def test_a_healthy_drain_raises_no_alarm(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An alarm that fires when nothing is wrong is an alarm nobody reads."""
    caplog.set_level("INFO")

    _run_main(monkeypatch, DUAL_ENV, oldest=None)

    assert [r for r in caplog.records if "backlog_alarm" in r.getMessage()] == []
    metrics = next(r for r in caplog.records if "outbox.metrics" in r.getMessage())
    assert "copies=2 degraded_targets=-" in metrics.getMessage()
