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
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import clickhouse.ingest_operational_outbox as writer_module
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

PARIS = "10.0.4.11"
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
    """The outbox table, keyed exactly as Postgres keys it."""

    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = list(rows)
        self.delete_calls: list[list[OperationalOutboxRow]] = []

    def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
        return [row for row in self.rows if row.shard == shard][:limit]

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
    return normalise_operational_event(_row(event_id))


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
    either way -- but it would hold the healthy node at the SAME staleness as
    the broken one for the whole outage, for no reason.
    """
    fleet = _Fleet(down={"local"})
    fleet.install(monkeypatch)

    with pytest.raises(FanOutWriteError):
        _dual_writer().insert([_event("gen-still-tried")])

    assert {call.host for call in fleet.calls} == {"local", STOCKHOLM}
    assert len(fleet.stored(STOCKHOLM)) == 1


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

    assert ddl.count("ENGINE = ReplacingMergeTree(ingest_version)") == 4
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
