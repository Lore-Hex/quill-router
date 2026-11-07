"""Python's all mode (docs/design/fast-admission-widening.md section 11, PR 5):
the settings and their validators, all mode in ``pilot_reason`` with the
per-process copy of the switch, the three reasons that match the edge, the
copy's dormancy, async admission's rule under all mode and the edge policy's
optional members."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from tests.fakes import spanner as spanner_fake
from tests.test_edge_policy_publish import VECTORS
from tests.test_fastpath_frontdoor import _Door, _transport
from tests.test_fastpath_pilot_route import (
    ADMIT,
    WON,
    WORKSPACE,
    _body,
    _credit,
    _pilot,
    _request,
    _seed,
)
from trusted_router.config import Settings
from trusted_router.edge_policy import canonical_edge_policy_bytes, parse_edge_policy
from trusted_router.routes.internal import gateway
from trusted_router.security import lookup_hash_api_key
from trusted_router.services import edge_policy_publisher as publisher_module
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.storage import STORE

TYPED = {"storage_backend": "spanner-clickhouse"}


def _all(**overrides: Any) -> Settings:
    """A valid all-mode configuration: the typed Spanner store, an empty
    explicit list and no async admission."""
    values: dict[str, Any] = {"environment": "test", "sentry_dsn": None, "fastpath_pilot_enabled": True,
                              "fastpath_pilot_all": True, **TYPED}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# -- the settings and their validators -----------------------------------------


def test_all_mode_is_off_by_default_and_reads_its_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, environment="test")  # type: ignore[call-arg]
    assert settings.fastpath_pilot_all is False and settings.fastpath_pilot_exclude_ids == frozenset()
    monkeypatch.setenv("TR_FASTPATH_PILOT_ALL", "true")
    monkeypatch.setenv("TR_FASTPATH_PILOT_EXCLUDE", " ws-b, ws-a, ws-b, ")
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    monkeypatch.setenv("TR_FASTPATH_PILOT_WORKSPACES", "")
    settings = Settings(_env_file=None, environment="test")  # type: ignore[call-arg]
    assert settings.fastpath_pilot_all is True
    assert settings.fastpath_pilot_exclude_ids == frozenset({"ws-a", "ws-b"})


def test_a_valid_all_mode_configuration_starts() -> None:
    settings = _all(fastpath_pilot_exclude="ws-probe,ws-x", async_settle_enabled=True, async_settle_protection=True,
                    async_settle_pilot_workspaces="ws-ops,ws-probe",
                    async_settle_fastpath_overlap_workspaces="ws-ops")
    assert settings.fastpath_pilot_all and settings.fastpath_covers("ws-anyone")


@pytest.mark.parametrize("backend", ["memory", "postgres"])
def test_all_mode_needs_the_typed_spanner_store(backend: str) -> None:
    with pytest.raises(ValidationError, match="requires TR_STORAGE_BACKEND=spanner-clickhouse"):
        _all(storage_backend=backend)


def test_all_mode_needs_an_empty_explicit_list() -> None:
    with pytest.raises(ValidationError, match="requires an empty TR_FASTPATH_PILOT_WORKSPACES"):
        _all(fastpath_pilot_workspaces="ws-pilot")


def test_all_mode_refuses_async_admission_for_every_workspace() -> None:
    """Section 9's first validator: async admission on with an empty async
    pilot set; with admission off an empty set is allowed."""
    with pytest.raises(ValidationError, match="empty TR_ASYNC_SETTLE_PILOT_WORKSPACES"):
        _all(async_settle_enabled=True, async_settle_protection=True)
    assert _all(async_settle_protection=True).fastpath_pilot_all


def test_all_mode_refuses_an_async_pilot_workspace_on_neither_list() -> None:
    """Section 9's second validator: every async pilot workspace is on the
    exclude list or the overlap list; the message counts, never names."""
    lists = {"async_settle_pilot_workspaces": "ws-ops,ws-probe",
             "async_settle_fastpath_overlap_workspaces": "ws-ops"}
    with pytest.raises(ValidationError, match="1 TR_ASYNC_SETTLE_PILOT_WORKSPACES on neither") as caught:
        _all(**lists)
    assert "ws-probe" not in str(caught.value)
    # With admission off too: turning admission on later must move nobody.
    with pytest.raises(ValidationError, match="on neither"):
        _all(async_settle_protection=True, **lists)
    assert _all(fastpath_pilot_exclude="ws-probe", **lists).fastpath_covers("ws-ops")


@pytest.mark.parametrize("value", ["a b", "*", "w" * 65, ",".join(f"ws-{i}" for i in range(65))])
def test_the_exclude_list_is_bounded_and_formed(value: str) -> None:
    with pytest.raises(ValidationError, match="invalid fast path pilot exclude set"):
        Settings(_env_file=None, environment="test", fastpath_pilot_exclude=value)  # type: ignore[call-arg]
    sixty_four = ",".join(f"ws-{i}" for i in range(64))
    assert len(Settings(_env_file=None, environment="test",  # type: ignore[call-arg]
                        fastpath_pilot_exclude=sixty_four).fastpath_pilot_exclude_ids) == 64


def test_the_exclude_list_changes_nothing_with_all_mode_off() -> None:
    """The list is read in either mode (the enabler reads it before all mode
    turns on) but covers by itself nobody new and leaves nobody out."""
    settings = Settings(_env_file=None, environment="test", fastpath_pilot_workspaces="ws-a",  # type: ignore[call-arg]
                        fastpath_pilot_exclude="ws-a,ws-b")
    assert settings.fastpath_covers("ws-a") and not settings.fastpath_covers("ws-b")
    assert settings.async_settle_fastpath_excluded_workspace_ids == frozenset({"ws-a"})


def test_fastpath_covers_is_the_exclude_list_in_all_mode_and_the_set_otherwise() -> None:
    settings = _all(fastpath_pilot_exclude="ws-out")
    assert settings.fastpath_covers("ws-in") and not settings.fastpath_covers("ws-out")
    with pytest.raises(ValueError, match="no finite fast-path excluded set"):
        _ = settings.async_settle_fastpath_excluded_workspace_ids


# -- the copy of the switch -------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_the_copy_enables_a_workspace_only_while_fresh() -> None:
    clock, rows = _Clock(), [frozenset({"ws-on"})]
    copy = fd.EnabledWorkspaceCopy(lambda: rows[0], clock=clock)
    # Never read: it enables nobody.
    assert not copy.enables("ws-on") and not copy.lists("ws-on")
    assert copy.refresh()
    assert copy.enables("ws-on") and not copy.enables("ws-off")
    clock.now += fd.ENABLED_COPY_MAX_AGE_SECONDS
    assert copy.enables("ws-on")
    clock.now += 0.001
    # Stale: it enables nobody, but still lists what it last read.
    assert not copy.enables("ws-on") and copy.lists("ws-on")
    rows[0] = frozenset({"ws-off"})
    assert copy.refresh()
    assert copy.enables("ws-off") and not copy.enables("ws-on") and not copy.lists("ws-on")


def test_a_failed_read_keeps_the_rows_and_lets_them_age(caplog: pytest.LogCaptureFixture) -> None:
    clock, rows = _Clock(), [frozenset({"ws-on"})]

    def read() -> frozenset[str]:
        if isinstance(rows[0], Exception):
            raise rows[0]
        return rows[0]

    copy = fd.EnabledWorkspaceCopy(read, clock=clock)
    assert copy.refresh()
    rows[0] = RuntimeError("deadline exceeded for ws-on")  # type: ignore[call-overload]
    clock.now += 10
    with caplog.at_level(logging.WARNING, logger="trusted_router.fastpath"):
        assert not copy.refresh()
    assert copy.failures == 1 and copy.enables("ws-on")
    # Logged by class: a message could carry an id.
    assert "RuntimeError" in caplog.text and "ws-on" not in caplog.text
    clock.now += 6
    assert not copy.enables("ws-on")


def test_the_refresh_thread_logs_once_by_class_when_it_exits(caplog: pytest.LogCaptureFixture) -> None:
    """A refresh catches a failed read; anything that escapes it (here the
    clock, after one read) ends the thread, and that is logged once, by the
    exception's class alone, never its text. The copy then only ages."""
    calls = [0]

    class Broken(Exception):
        pass

    def clock() -> float:
        calls[0] += 1
        if calls[0] > 3:
            raise Broken("text naming ws-on")
        return 0.0

    copy = fd.EnabledWorkspaceCopy(lambda: frozenset({"ws-on"}), clock=clock, interval_seconds=0.0)
    with caplog.at_level(logging.ERROR, logger="trusted_router.fastpath"):
        copy.start()
        assert copy._thread is not None
        copy._thread.join(timeout=5)
    assert not copy._thread.is_alive()
    exits = [r for r in caplog.records if "refresh thread exited" in r.getMessage()]
    assert len(exits) == 1 and exits[0].levelno == logging.ERROR
    assert "Broken" in exits[0].getMessage() and "ws-on" not in exits[0].getMessage()
    assert copy.reads == 1


def test_the_copys_age_runs_from_the_start_of_its_read() -> None:
    clock = _Clock()

    def slow_read() -> frozenset[str]:
        clock.now += 10
        return frozenset({"ws-on"})

    copy = fd.EnabledWorkspaceCopy(slow_read, clock=clock)
    copy.refresh()
    clock.now += 5.5
    assert not copy.enables("ws-on")


def test_the_copy_reads_the_switchs_enabled_rows_within_its_timeout() -> None:
    seen: list[tuple[str, float | None]] = []

    class Snapshot:
        def __enter__(self) -> Snapshot:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute_sql(self, sql: str, timeout: float | None = None) -> list[tuple[str]]:
            seen.append((sql, timeout))
            return [("ws-a",), ("ws-b",)]

    class Database:
        def snapshot(self) -> Snapshot:
            return Snapshot()

    assert fd.spanner_enabled_workspaces(Database())() == frozenset({"ws-a", "ws-b"})
    assert seen == [(fd.FAST_PATH_ENABLED_WORKSPACES, fd.ENABLED_COPY_READ_TIMEOUT_SECONDS)]
    assert "FROM tr_fastpath_workspace WHERE enabled" in fd.FAST_PATH_ENABLED_WORKSPACES


# -- pilot_reason in all mode -----------------------------------------------------


_ELIGIBLE: dict[str, Any] = {
    "enabled": True, "workspace_id": "ws-1", "pilot_workspace_ids": frozenset(), "region": "us-central1",
    "pilot_region_ids": frozenset({"us-central1"}), "federated": False, "async_shadow": False,
    "monitor_key": False, "stream": False, "standard_endpoint_pricing": True, "credit_funded": True,
    "boot_verified": True, "service_tier": None, "key_capped": False, "budget_strict": False,
    "app_markup": False, "records_durable": True, "pilot_all": True, "pilot_exclude_ids": frozenset({"ws-out"}),
    "switch_enabled": True,
}


def test_all_mode_takes_an_enabled_workspace_off_no_list() -> None:
    assert fd.pilot_reason(**_ELIGIBLE) is None


@pytest.mark.parametrize(("change", "reason"), [
    # The copy stale or missing the workspace: switch_enabled false.
    ({"switch_enabled": False}, "workspace"),
    # Excluded, whatever the copy says.
    ({"workspace_id": "ws-out"}, "workspace"),
    ({"region": "us-east4"}, "region"),
    ({"region": "us-east4", "switch_enabled": False}, "region"),
    ({"enabled": False}, "disabled"),
    # The three reasons that match the edge.
    ({"federated": True}, "federated"),
    ({"async_shadow": True}, "async_shadow"),
    ({"monitor_key": True}, "monitor_key"),
    ({"federated": True, "async_shadow": True, "monitor_key": True}, "federated"),
    ({"async_shadow": True, "monitor_key": True}, "async_shadow"),
    # Not covered comes before the new reasons.
    ({"switch_enabled": False, "federated": True}, "workspace"),
    ({"records_durable": False}, "records"),
])
def test_all_mode_departures_name_themselves(change: dict[str, Any], reason: str) -> None:
    assert fd.pilot_reason(**{**_ELIGIBLE, **change}) == reason


def test_the_explicit_list_ignores_the_switch_copy_and_the_exclude_list() -> None:
    explicit = {**_ELIGIBLE, "pilot_all": False, "pilot_workspace_ids": frozenset({"ws-1"})}
    assert fd.pilot_reason(**{**explicit, "switch_enabled": False}) is None
    assert fd.pilot_reason(**{**explicit, "pilot_exclude_ids": frozenset({"ws-1"})}) is None
    assert fd.pilot_reason(**{**explicit, "workspace_id": "ws-2"}) == "workspace"


# -- the route ----------------------------------------------------------------------


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> _Door:
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    door = _Door({"/v1/authorize": ADMIT, "/v1/settle": dict(WON)})
    client = fd.FrontDoorClient(lambda _budget: ("10.0.0.1:8080",), timeout_seconds=1.5, clock=lambda: 0.0,
                                transport=_transport({"10.0.0.1:8080": door}))
    monkeypatch.setattr(gateway, "_fastpath_client", client)
    return door


def _copy(monkeypatch: pytest.MonkeyPatch, enabled: frozenset[str], *, age: float = 0.0) -> fd.EnabledWorkspaceCopy:
    clock = _Clock()
    copy = fd.EnabledWorkspaceCopy(lambda: enabled, clock=clock)
    copy.refresh()
    clock.now += age
    monkeypatch.setattr(gateway, "_fastpath_switch", copy)
    return copy


def _all_pilot(**overrides: Any) -> Settings:
    return _pilot(fastpath_pilot_workspaces="", fastpath_pilot_all=True, **TYPED, **overrides)


def _passed_over(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("fastpath pilot passed over")]


def test_all_mode_leases_a_workspace_the_fresh_copy_enables(
    door: _Door, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed()
    _copy(monkeypatch, frozenset({WORKSPACE}))
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _all_pilot())["data"]
    assert store.get_gateway_authorization(data["authorization_id"]).fastpath_envelope == "sealed-pilot"
    assert [path for path, _sent in door.seen] == ["/v1/authorize"]


@pytest.mark.parametrize(("name", "enabled", "age", "overrides", "logged"), [
    ("missing", frozenset({"ws-other"}), 0.0, {}, False),
    ("stale", frozenset({WORKSPACE}), fd.ENABLED_COPY_MAX_AGE_SECONDS + 1, {}, True),
    ("excluded", frozenset({WORKSPACE}), 0.0, {"fastpath_pilot_exclude": WORKSPACE}, False),
])
def test_all_mode_asks_no_front_door_for_a_workspace_the_copy_does_not_enable(
    door: _Door, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    name: str, enabled: frozenset[str], age: float, overrides: dict[str, Any], logged: bool,
) -> None:
    """A stale copy covers nobody and a missing or excluded workspace is not
    covered: today's path, no front door call. The pass-over line names
    only an enabled workspace the policy covers: the stale copy's, so a
    copy that stops reading shows up as `workspace` passes."""
    caplog.set_level(logging.INFO)
    store, database, key = _seed()
    _copy(monkeypatch, enabled, age=age)
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _all_pilot(**overrides))["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization.fastpath_envelope is None, name
    assert _credit(database)["reserved"] == authorization.estimated_microdollars > 0, name
    assert door.seen == [], name
    expected = [f"fastpath pilot passed over reason=workspace workspace_id={WORKSPACE}"] if logged else []
    assert _passed_over(caplog) == expected, name


def _federate_workspace(store: Any) -> None:
    store._write_entity("workspace", WORKSPACE, dataclasses.replace(store.get_workspace(WORKSPACE),
                                                                     federated_home="home-1"))


@pytest.mark.parametrize("pilot_all", [False, True], ids=["explicit", "all"])
@pytest.mark.parametrize("reason", ["federated", "async_shadow", "monitor_key"])
def test_the_three_edge_reasons_keep_a_pilot_workspace_on_todays_path(
    door: _Door, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reason: str, pilot_all: bool,
) -> None:
    """A federated workspace, an async-shadow workspace and the monitor's key
    each take today's path with no front door call, as the edge passes them
    (key.federated or workspace.federated, policy.async_settle_shadow,
    policy.monitor_key), under the explicit list and in all mode, and the
    pass-over line names the reason."""
    caplog.set_level(logging.INFO)
    store, database, key = _seed()
    raw, monitor = store.api_keys.create(workspace_id=WORKSPACE, name="monitor", creator_user_id="user-pilot")
    overrides: dict[str, Any] = {}
    body = _body(key.hash)
    if reason == "federated":
        _federate_workspace(store)
    elif reason == "async_shadow":
        overrides["async_settle_shadow_workspaces"] = WORKSPACE
    else:
        overrides["synthetic_monitor_api_key"] = raw
        body = _body(None, api_key_lookup_hash=lookup_hash_api_key(raw))
    settings = _all_pilot(**overrides) if pilot_all else _pilot(**overrides)
    if pilot_all:
        _copy(monkeypatch, frozenset({WORKSPACE}))
    data = gateway._authorize_gateway_sync(_request(), body, settings)["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization.fastpath_envelope is None
    assert authorization.credit_reservation_id is not None
    assert door.seen == []
    assert _passed_over(caplog) == [f"fastpath pilot passed over reason={reason} workspace_id={WORKSPACE}"]
    # The control: the same request without the fact is leased.
    if reason != "federated":
        settings = _all_pilot() if pilot_all else _pilot()
        control = _body(key.hash) if reason == "async_shadow" else _body(None, api_key_lookup_hash=monitor.lookup_hash)
        data = gateway._authorize_gateway_sync(_request(), control, settings)["data"]
        assert store.get_gateway_authorization(data["authorization_id"]).fastpath_envelope == "sealed-pilot"


def test_a_federated_key_keeps_a_pilot_workspace_on_todays_path(
    door: _Door, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    store, _database, key = _seed()
    federated = dataclasses.replace(key, federated_home="home-1")
    monkeypatch.setattr(type(store), "get_key_by_hash", lambda _self, key_hash: (
        federated if key_hash == key.hash else None))
    data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _pilot())["data"]
    assert store.get_gateway_authorization(data["authorization_id"]).fastpath_envelope is None
    assert door.seen == []
    assert _passed_over(caplog) == [f"fastpath pilot passed over reason=federated workspace_id={WORKSPACE}"]


# -- dormancy -------------------------------------------------------------------


@pytest.fixture
def copy_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every read of the switch's enabled rows the Spanner fake is asked for;
    the copy's thread is replaced by one synchronous read."""
    reads: list[str] = []
    original = spanner_fake._FakeSnapshot.execute_sql

    def execute_sql(self: Any, sql: str, **kwargs: Any) -> Any:
        if sql == fd.FAST_PATH_ENABLED_WORKSPACES:
            reads.append(sql)
            return [(WORKSPACE,)]
        return original(self, sql, **kwargs)

    monkeypatch.setattr(spanner_fake._FakeSnapshot, "execute_sql", execute_sql)
    monkeypatch.setattr(fd.EnabledWorkspaceCopy, "start", lambda self: self.refresh())
    monkeypatch.setattr(gateway, "_fastpath_switch", None)
    return reads


@pytest.mark.parametrize(("name", "settings"), [
    ("explicit list", _pilot()),
    ("all mode outside the pilot regions", _pilot(fastpath_pilot_workspaces="", fastpath_pilot_all=True,
                                                  serving_region="europe-west4", **TYPED)),
    ("all mode with the pilot off", _pilot(fastpath_pilot_workspaces="", fastpath_pilot_all=True,
                                           fastpath_pilot_enabled=False, **TYPED)),
])
def test_no_copy_is_read_with_all_mode_off_or_outside_the_pilot_regions(
    door: _Door, copy_reads: list[str], name: str, settings: Settings,
) -> None:
    _store, _database, key = _seed()
    for _ in range(3):
        gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)
    assert copy_reads == [], name
    assert gateway._fastpath_switch is None, name


def test_all_mode_in_a_pilot_region_reads_the_copy_once_and_keeps_it(door: _Door, copy_reads: list[str]) -> None:
    """The dormancy test's control: in all mode, in a pilot region, the
    copy is built on first use, read from the store's database, and kept."""
    store, _database, key = _seed()
    assert getattr(STORE, "_database", None) is not None
    for _ in range(3):
        data = gateway._authorize_gateway_sync(_request(), _body(key.hash), _all_pilot())["data"]
        assert store.get_gateway_authorization(data["authorization_id"]).fastpath_envelope == "sealed-pilot"
    assert copy_reads == [fd.FAST_PATH_ENABLED_WORKSPACES]
    assert isinstance(gateway._fastpath_switch, fd.EnabledWorkspaceCopy)


# -- async admission ------------------------------------------------------------


@pytest.mark.parametrize("pilots", ["ws-ops", "ws-ops,ws-probe", "ws-probe"])
def test_async_admission_in_all_mode_follows_coverage(pilots: str) -> None:
    """Section 9: in all mode async admission leaves out every covered
    workspace the overlap list does not name. The overlap workspace in both
    pilots keeps async; an async pilot workspace on the exclude list keeps
    async; a workspace the enabler turns on (covered, on no list) is out."""
    listed = set(pilots.split(","))
    settings = _all(async_settle_enabled=True, async_settle_protection=True, async_settle_pilot_workspaces=pilots,
                    async_settle_fastpath_overlap_workspaces="ws-ops",
                    fastpath_pilot_exclude=",".join(listed - {"ws-ops"}))
    for ws in ("ws-ops", "ws-probe", "ws-enabled"):
        assert settings.async_settle_pilot_allows(ws) is (ws in listed), (pilots, ws)
    assert settings.async_settle_fastpath_excludes("ws-enabled")
    assert not settings.async_settle_fastpath_excludes("ws-ops")
    # Coverage, not the switch: the pilot's own switch changes nothing.
    off = settings.model_copy(update={"fastpath_pilot_enabled": False})
    assert all(off.async_settle_pilot_allows(ws) is settings.async_settle_pilot_allows(ws)
               for ws in ("ws-ops", "ws-probe", "ws-enabled"))


def test_a_configuration_the_validator_refuses_still_fails_closed() -> None:
    """Bypassing the second validator (a settings object mutated after
    validation) still keeps the uncovered async pilot workspace out."""
    settings = _all(async_settle_protection=True, async_settle_pilot_workspaces="ws-ops",
                    async_settle_fastpath_overlap_workspaces="ws-ops")
    settings.async_settle_fastpath_overlap_workspaces = ""
    settings.parse_fastpath_overlap_workspaces()
    with pytest.raises(ValueError, match="on neither"):
        settings.validate_fastpath_pilot_all()
    assert not settings.async_settle_pilot_allows("ws-ops")


# -- the edge policy --------------------------------------------------------------


def test_the_published_body_is_byte_identical_with_all_mode_off() -> None:
    """With all mode off the body is the golden one, whatever the exclude
    list says, so a node before PR 3 reads it unchanged."""
    golden = VECTORS.OUTPUT_BODY.read_bytes()
    for exclude in ("", "ws-synthetic-excluded-a", "ws-synthetic-excluded-b,ws-synthetic-excluded-a"):
        settings = VECTORS.fixture_settings(fastpath_pilot_exclude=exclude)
        assert canonical_edge_policy_bytes(VECTORS.policy(settings)) == golden, exclude
    for member in (b"fastpath_pilot_all", b"fastpath_pilot_exclude"):
        assert member not in golden


def test_all_mode_publishes_both_members_and_an_empty_explicit_list() -> None:
    body = canonical_edge_policy_bytes(VECTORS.policy(VECTORS.all_mode_settings()))
    assert body == VECTORS.OUTPUT_ALL_BODY.read_bytes()
    decoded = json.loads(body)
    assert decoded["fastpath_pilot_all"] is True
    assert decoded["fastpath_pilot_exclude"] == sorted(VECTORS.ALL_EXCLUDE)
    assert decoded["fastpath_pilot_workspaces"] == []
    # An empty exclude list is still present with all mode.
    empty = canonical_edge_policy_bytes(VECTORS.policy(VECTORS.fixture_settings(
        fastpath_pilot_workspaces="", fastpath_pilot_all=True, **TYPED)))
    assert json.loads(empty)["fastpath_pilot_exclude"] == []
    assert parse_edge_policy(empty).fastpath_pilot_all is True


def test_the_published_exclusion_stays_within_the_async_pilot_set_in_all_mode() -> None:
    """admission_excluded_workspaces is the async pilot set's members that
    async_settle_fastpath_excludes names: absent when the validators are
    met; and all mode with admission on and no pilot set fails the signing
    rather than publish every workspace."""
    settings = VECTORS.admission_settings(True, "ws-synthetic-async-a,ws-synthetic-pilot-1",
                                          "ws-synthetic-pilot-1", "", "ws-synthetic-async-a")
    assert publisher_module.admission_workspaces(settings) == {
        "admission_workspaces": ["ws-synthetic-async-a", "ws-synthetic-pilot-1"]}
    mutated = settings.model_copy()
    mutated.async_settle_pilot_workspaces = ""
    mutated.parse_pilot_workspaces()
    with pytest.raises(ValueError, match="needs an async pilot set"):
        publisher_module.admission_workspaces(mutated)
    # A validator-refused overlap removal publishes the workspace as excluded.
    mutated = settings.model_copy()
    mutated.async_settle_fastpath_overlap_workspaces = ""
    mutated.parse_fastpath_overlap_workspaces()
    assert publisher_module.admission_workspaces(mutated)["admission_excluded_workspaces"] == [
        "ws-synthetic-pilot-1"]
