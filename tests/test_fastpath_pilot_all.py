"""Python's all mode (docs/design/fast-admission-widening.md section 11, PR 5):
the settings and their validators, all mode in ``pilot_reason`` with the
per-process copy of the switch, the three reasons that match the edge, the
copy's dormancy, async admission's rule under all mode and the edge policy's
optional members."""

from __future__ import annotations

import dataclasses
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from tests.fakes import spanner as spanner_fake
from tests.test_async_settle_ticket import runtime as async_runtime
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


def test_all_mode_with_an_empty_async_pilot_set_admits_every_workspace_but_the_reserved_list() -> None:
    """Section 9.1, which replaces section 9's first validator (it refused
    async admission on with an empty async pilot set): the configuration
    starts, and admission covers every workspace but the reserved list's,
    covered by the fast path or on its exclude list alike. The pilot's
    switch changes nothing. The finite excluded set is the reserved list."""
    settings = _all(async_settle_enabled=True, async_settle_protection=True, fastpath_pilot_exclude="ws-probe",
                    async_settle_fastpath_overlap_workspaces="ws-ops",
                    async_settle_fastpath_reserved_workspaces=" ws-held-b, ws-held-a ,ws-held-b")
    assert settings.async_settle_admission_enabled
    assert settings.async_settle_fastpath_reserved_workspace_ids == frozenset({"ws-held-a", "ws-held-b"})
    assert settings.async_settle_fastpath_excluded_workspace_ids == frozenset({"ws-held-a", "ws-held-b"})
    for ws, admitted in (("ws-ops", True), ("ws-probe", True), ("ws-anyone", True), ("ws-enabled", True),
                         ("ws-held-a", False), ("ws-held-b", False)):
        assert settings.async_settle_pilot_allows(ws) is admitted, ws
        assert settings.async_settle_fastpath_excludes(ws) is (not admitted), ws
    off = settings.model_copy(update={"fastpath_pilot_enabled": False})
    assert all(off.async_settle_pilot_allows(ws) is settings.async_settle_pilot_allows(ws)
               for ws in ("ws-ops", "ws-probe", "ws-anyone", "ws-held-a"))
    # No reserved list: every workspace, and nothing excluded.
    everyone = _all(async_settle_enabled=True, async_settle_protection=True)
    assert everyone.async_settle_fastpath_excluded_workspace_ids == frozenset()
    assert all(everyone.async_settle_pilot_allows(ws) for ws in ("ws-ops", "ws-anyone", "ws-held-a"))
    # With admission off an empty set was always allowed.
    assert _all(async_settle_protection=True).fastpath_pilot_all


def test_the_reserved_list_is_all_mode_s_and_names_only_workspaces_the_pilot_covers() -> None:
    """The reserved list's validators: all mode only, never an overlap
    workspace (admitted by definition), never an exclude-list workspace (the
    fast path never takes it); the messages count, never name."""
    with pytest.raises(ValidationError, match="RESERVED_WORKSPACES requires TR_FASTPATH_PILOT_ALL"):
        Settings(_env_file=None, environment="test",  # type: ignore[call-arg]
                 async_settle_fastpath_reserved_workspaces="ws-held")
    with pytest.raises(ValidationError, match="1 TR_ASYNC_SETTLE_FASTPATH_RESERVED_WORKSPACES also on") as caught:
        _all(async_settle_fastpath_reserved_workspaces="ws-held,ws-ops",
             async_settle_fastpath_overlap_workspaces="ws-ops")
    assert "ws-ops" not in str(caught.value)
    with pytest.raises(ValidationError,
                       match="1 TR_ASYNC_SETTLE_FASTPATH_RESERVED_WORKSPACES on TR_FASTPATH_PILOT_EXCLUDE") as caught:
        _all(async_settle_fastpath_reserved_workspaces="ws-held,ws-probe", fastpath_pilot_exclude="ws-probe")
    assert "ws-probe" not in str(caught.value)
    assert _all(async_settle_fastpath_reserved_workspaces="ws-held", fastpath_pilot_exclude="ws-probe",
                async_settle_fastpath_overlap_workspaces="ws-ops").fastpath_pilot_all


@pytest.mark.parametrize("value", ["a b", "*", "w" * 65, ",".join(f"ws-{i}" for i in range(33))])
def test_the_reserved_list_is_bounded_and_formed(value: str) -> None:
    with pytest.raises(ValidationError, match="invalid async settle fast path reserved workspace set"):
        _all(async_settle_fastpath_reserved_workspaces=value)
    thirty_two = ",".join(f"ws-{i}" for i in range(32))
    assert len(_all(async_settle_fastpath_reserved_workspaces=thirty_two)
               .async_settle_fastpath_reserved_workspace_ids) == 32


def test_the_reserved_list_reads_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None, environment="test").async_settle_fastpath_reserved_workspace_ids == frozenset()  # type: ignore[call-arg]
    monkeypatch.setenv("TR_FASTPATH_PILOT_ALL", "true")
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    monkeypatch.setenv("TR_FASTPATH_PILOT_WORKSPACES", "")
    monkeypatch.setenv("TR_ASYNC_SETTLE_FASTPATH_RESERVED_WORKSPACES", "ws-b,ws-a")
    settings = Settings(_env_file=None, environment="test")  # type: ignore[call-arg]
    assert settings.async_settle_fastpath_reserved_workspace_ids == frozenset({"ws-a", "ws-b"})


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
    settings = _all(fastpath_pilot_exclude="ws-out", async_settle_pilot_workspaces="ws-out")
    assert settings.fastpath_covers("ws-in") and not settings.fastpath_covers("ws-out")
    # With an async pilot set, all mode's excluded set is not finite.
    with pytest.raises(ValueError, match="no finite fast-path excluded set"):
        _ = settings.async_settle_fastpath_excluded_workspace_ids
    # Without one it is the reserved list (section 9.1), here empty.
    assert _all(fastpath_pilot_exclude="ws-out").async_settle_fastpath_excluded_workspace_ids == frozenset()


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


def _async_request() -> Request:
    """An authorize asking for async-v1, on an app whose async settle runtime
    signs tickets and finds every workspace within its cap."""
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(b"x-tr-boot-auth", b"kid=boot-pilot,sig=x"), (b"x-tr-settlement-mode", b"async-v1")],
                    "app": SimpleNamespace(state=SimpleNamespace(async_settle=async_runtime()))})


@pytest.mark.parametrize(("name", "leased", "lists", "ticket"), [
    ("leased", True, {}, False),
    ("not leased", False, {}, True),
    ("leased, on the overlap list", True, {"async_settle_fastpath_overlap_workspaces": WORKSPACE}, True),
    ("not leased, on the reserved list", False, {"async_settle_fastpath_reserved_workspaces": WORKSPACE}, False),
])
def test_the_authorize_route_signs_no_ticket_for_a_request_the_pilot_leased(
    door: _Door, monkeypatch: pytest.MonkeyPatch, name: str, leased: bool, lists: dict[str, str], ticket: bool,
) -> None:
    """Section 9.1 through the route, with the async pilot set empty: the
    authorization the route hands the async projection is the one it stored,
    lease and all. A leased request's answer carries no ticket unless its
    workspace is on the overlap list; a request the pilot did not lease
    carries one unless its workspace is reserved. Each answer without a
    ticket is the admission-off shape: the snapshot and its hash, nothing
    else of async settle's."""
    store, _database, key = _seed()
    _copy(monkeypatch, frozenset({WORKSPACE}) if leased else frozenset())
    settings = _all_pilot(async_settle_enabled=True, async_settle_protection=True, **lists)
    assert settings.async_settle_pilot_workspace_ids == frozenset()
    data = gateway._authorize_gateway_sync(
        _async_request(), _body(key.hash, invocation_nonce="nonce-route-1"), settings)["data"]
    stored = store.get_gateway_authorization(data["authorization_id"])
    assert (stored.fastpath_envelope is not None) is leased, name
    assert ("settlement_ticket" in data) is ticket, name
    assert data["async_eligible"] is ticket, name
    assert {"billing_snapshot", "billing_snapshot_hash"} <= set(data), name
    if not ticket:
        assert not {"settlement_ticket", "settlement_status_url"} & set(data), name


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


def test_a_named_async_pilot_set_in_all_mode_ignores_the_reserved_list() -> None:
    """Restoring the pilot set (the way back from section 9.1) restores
    section 9's rule whatever the reserved list says: coverage decides, and
    all mode then has no finite excluded set."""
    settings = _all(async_settle_enabled=True, async_settle_protection=True,
                    async_settle_pilot_workspaces="ws-ops,ws-probe", fastpath_pilot_exclude="ws-probe",
                    async_settle_fastpath_overlap_workspaces="ws-ops",
                    async_settle_fastpath_reserved_workspaces="ws-held")
    for ws, admitted in (("ws-ops", True), ("ws-probe", True), ("ws-enabled", False), ("ws-held", False)):
        assert settings.async_settle_pilot_allows(ws) is admitted, ws
    assert settings.async_settle_fastpath_excludes("ws-enabled")
    with pytest.raises(ValueError, match="no finite fast-path excluded set"):
        _ = settings.async_settle_fastpath_excluded_workspace_ids


def test_a_leased_request_takes_no_ticket_unless_its_workspace_is_on_the_overlap_list() -> None:
    """async_settle_admits_request, the rule said of the request (section
    9.1): with every workspace admitted, a request the pilot leased is left
    out unless the overlap list names its workspace; one it did not lease
    follows its workspace; a reserved workspace is out either way."""
    settings = _all(async_settle_enabled=True, async_settle_protection=True, fastpath_pilot_exclude="ws-probe",
                    async_settle_fastpath_overlap_workspaces="ws-ops",
                    async_settle_fastpath_reserved_workspaces="ws-held")
    table = {("ws-ops", False): True, ("ws-ops", True): True,
             ("ws-enabled", False): True, ("ws-enabled", True): False,
             ("ws-probe", False): True, ("ws-probe", True): False,
             ("ws-held", False): False, ("ws-held", True): False}
    for (ws, leased), admitted in table.items():
        assert settings.async_settle_admits_request(ws, leased=leased) is admitted, (ws, leased)


def test_the_request_rule_changes_no_answer_of_the_workspace_rule_it_replaces() -> None:
    """Over every settings combination of the admission vectors: a request
    the pilot did not lease gets its workspace's answer
    (async_settle_pilot_allows), and so does a leased one of any workspace
    the pilot can lease (one it covers) under every configuration but
    section 9.1's, where a covered workspace was never admitted outside the
    overlap list. Section 9.1's rows are the only ones the leased clause
    decides."""
    decided = unchanged = 0
    for case in json.loads(VECTORS.OUTPUT_ADMISSION.read_text())["cases"]:
        s = case["settings"]
        if s is None:
            continue
        settings = VECTORS.admission_settings(s["admission"], s["pilots"], s["overlap"], s["fastpath"],
                                              s.get("exclude"), s.get("reserved", ""))
        everyone = settings.fastpath_pilot_all and not settings.async_settle_pilot_workspace_ids
        for ws in VECTORS.ADMISSION_PROBES:
            workspace = settings.async_settle_pilot_allows(ws)
            assert settings.async_settle_admits_request(ws, leased=False) is workspace, (case["name"], ws)
            if not settings.fastpath_covers(ws):
                continue
            leased = settings.async_settle_admits_request(ws, leased=True)
            if everyone and workspace and ws not in settings.async_settle_fastpath_overlap_workspace_ids:
                assert leased is False, (case["name"], ws)
                decided += 1
            else:
                assert leased is workspace, (case["name"], ws)
                unchanged += 1
    assert decided and unchanged


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
    met. All mode with admission on and no pilot set (section 9.1, which
    replaces the refusal to sign it) publishes no pilot set and the reserved
    list as the one bounded exclusion, or neither member without one."""
    settings = VECTORS.admission_settings(True, "ws-synthetic-async-a,ws-synthetic-pilot-1",
                                          "ws-synthetic-pilot-1", "", "ws-synthetic-async-a")
    assert publisher_module.admission_workspaces(settings) == {
        "admission_workspaces": ["ws-synthetic-async-a", "ws-synthetic-pilot-1"]}
    everyone = VECTORS.admission_settings(True, "", "ws-synthetic-pilot-1", "", "ws-synthetic-async-a")
    assert publisher_module.admission_workspaces(everyone) == {}
    reserved = VECTORS.admission_settings(True, "", "ws-synthetic-pilot-1", "", "ws-synthetic-async-a",
                                          "ws-synthetic-pilot-2,ws-synthetic-neither")
    assert publisher_module.admission_workspaces(reserved) == {
        "admission_excluded_workspaces": ["ws-synthetic-neither", "ws-synthetic-pilot-2"]}
    body = canonical_edge_policy_bytes(VECTORS.policy(reserved))
    parsed = parse_edge_policy(body)
    assert parsed.fastpath_pilot_all is True and parsed.async_settle.admission_workspaces is None
    assert parsed.async_settle.admission_excluded_workspaces == ("ws-synthetic-neither", "ws-synthetic-pilot-2")
    assert canonical_edge_policy_bytes(parsed) == body
    # With admission off the reserved list publishes nothing.
    assert publisher_module.admission_workspaces(VECTORS.admission_settings(
        False, "", "", "", "", "ws-synthetic-neither")) == {}
    # A validator-refused overlap removal publishes the workspace as excluded.
    mutated = settings.model_copy()
    mutated.async_settle_fastpath_overlap_workspaces = ""
    mutated.parse_fastpath_overlap_workspaces()
    assert publisher_module.admission_workspaces(mutated)["admission_excluded_workspaces"] == [
        "ws-synthetic-pilot-1"]
