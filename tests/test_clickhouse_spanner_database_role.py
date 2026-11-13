"""The ClickHouse node programs connect as ``TR_SPANNER_DATABASE_ROLE`` when set.

Unset, every program opens its database exactly as before:
``instance.database(<id>)`` with no keyword, and the REST rollup creates its
session with an empty body. Set, the client programs pass ``database_role=``
and the rollup sends ``creatorRole``. No test here talks to Spanner: the
client library's ``Client`` and the REST transport are stubbed.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import google.auth
import pytest
from google.cloud import spanner

from clickhouse import (
    _spanner_role,
    ingest_operational_outbox,
    ingest_outbox,
    refresh_workspace_directory,
    rollup_reservation_overruns,
    verify_spanner_delivery,
)

ROLE = "tr_clickhouse_node"


class _RecordingClient:
    """Stands in for ``google.cloud.spanner.Client``; records the open chain."""

    opened: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def instance(self, instance_id: str) -> SimpleNamespace:
        def database(database_id: str, *args: Any, **kwargs: Any) -> object:
            _RecordingClient.opened.append(
                {
                    "client": self._kwargs,
                    "instance": instance_id,
                    "database": database_id,
                    "args": args,
                    "kwargs": kwargs,
                }
            )
            return object()

        return SimpleNamespace(database=database)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _RecordingClient.opened = []
    monkeypatch.setattr(spanner, "Client", _RecordingClient)
    return _RecordingClient.opened


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, None),
        ({"TR_SPANNER_DATABASE_ROLE": ""}, None),
        ({"TR_SPANNER_DATABASE_ROLE": "   "}, None),
        ({"TR_SPANNER_DATABASE_ROLE": ROLE}, ROLE),
        ({"TR_SPANNER_DATABASE_ROLE": f" {ROLE}\n"}, ROLE),
    ],
)
def test_the_role_variable_is_optional_and_blank_means_unset(
    environ: dict[str, str], expected: str | None,
) -> None:
    assert _spanner_role.DATABASE_ROLE_ENV == "TR_SPANNER_DATABASE_ROLE"
    assert _spanner_role.database_role(environ) == expected


def test_the_role_is_read_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TR_SPANNER_DATABASE_ROLE", raising=False)
    assert _spanner_role.database_role() is None
    monkeypatch.setenv("TR_SPANNER_DATABASE_ROLE", ROLE)
    assert _spanner_role.database_role() == ROLE


_CLIENT_SOURCES = {
    "ingest_outbox": lambda role: ingest_outbox.SpannerOutboxSource(
        project="p", instance="i", database="d", database_role=role,
    ),
    "ingest_operational_outbox": lambda role: (
        ingest_operational_outbox.SpannerOperationalOutboxSource(
            project="p", instance="i", database="d", database_role=role,
        )
    ),
    "refresh_workspace_directory": lambda role: (
        refresh_workspace_directory.SpannerWorkspaceSource(
            project="p", instance="i", database="d", database_role=role,
        )
    ),
    "verify_spanner_delivery": lambda role: verify_spanner_delivery.SpannerGenerationSource(
        project="p", instance="i", database="d", database_role=role,
    ),
}


@pytest.mark.parametrize("program", sorted(_CLIENT_SOURCES))
def test_without_a_role_the_database_opens_exactly_as_before(
    opened: list[dict[str, Any]], program: str,
) -> None:
    _CLIENT_SOURCES[program](None)

    assert opened == [
        {
            "client": {"project": "p", "disable_builtin_metrics": True},
            "instance": "i",
            "database": "d",
            "args": (),
            "kwargs": {},
        }
    ]


@pytest.mark.parametrize("program", sorted(_CLIENT_SOURCES))
def test_with_a_role_the_database_opens_as_that_role(
    opened: list[dict[str, Any]], program: str,
) -> None:
    _CLIENT_SOURCES[program](ROLE)

    assert opened == [
        {
            "client": {"project": "p", "disable_builtin_metrics": True},
            "instance": "i",
            "database": "d",
            "args": (),
            "kwargs": {"database_role": ROLE},
        }
    ]


def _rollup_session_request(
    monkeypatch: pytest.MonkeyPatch, role: str | None,
) -> Mapping[str, Any] | None:
    monkeypatch.setattr(google.auth, "default", lambda scopes: (object(), "p"))
    source = rollup_reservation_overruns.SpannerReservationSource(
        project="p", instance="i", database="d", database_role=role,
    )
    calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def fake_request_json(
        url: str, *, method: str = "POST", body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((method, url, body))
        if url.endswith("/sessions"):
            return {"name": "projects/p/instances/i/databases/d/sessions/s1"}
        if url.endswith(":executeSql"):
            return {"metadata": {"rowType": {"fields": []}}, "rows": []}
        return {}

    monkeypatch.setattr(source, "_request_json", fake_request_json)
    source.fetch(
        window_start=dt.datetime(2026, 8, 25, 10, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC),
    )
    [create] = [call for call in calls if call[1].endswith("/sessions")]
    assert create[:2] == (
        "POST", "https://spanner.googleapis.com/v1/projects/p/instances/i/databases/d/sessions",
    )
    # The role rides only on CreateSession; the statement itself is unchanged.
    [execute] = [call for call in calls if call[1].endswith(":executeSql")]
    assert execute[2] is not None
    assert "creatorRole" not in execute[2]
    return create[2]


def test_the_rest_rollup_creates_an_empty_session_without_a_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _rollup_session_request(monkeypatch, None) == {}


def test_the_rest_rollup_creates_its_session_as_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _rollup_session_request(monkeypatch, ROLE) == {"session": {"creatorRole": ROLE}}


# ---------------------------------------------------------------------------
# Each program's entry point hands the environment's role to its source.
# ---------------------------------------------------------------------------


class _Constructed:
    """Replaces a source class; records the keyword arguments it was built with."""

    def __init__(self, source: Any) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self._source = source

    def __call__(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return self._source


def _run_ingest_operational_outbox(monkeypatch: pytest.MonkeyPatch) -> _Constructed:
    constructed = _Constructed(object())
    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    monkeypatch.setattr(ingest_operational_outbox, "SpannerOperationalOutboxSource", constructed)
    monkeypatch.setattr(ingest_operational_outbox, "ClickHouseOperationalWriter", lambda **_: None)
    monkeypatch.setattr(ingest_operational_outbox, "sd_notify", lambda _: None)
    monkeypatch.setattr(
        ingest_operational_outbox,
        "drain_once",
        lambda *_, **__: ingest_operational_outbox.DrainResult(
            fetched=0, inserted=0, rows_per_second=0.0,
        ),
    )
    monkeypatch.setattr(
        ingest_operational_outbox,
        "OutboxHeartbeatPublisher",
        lambda _source: SimpleNamespace(after_pass=lambda _result: True),
    )
    assert ingest_operational_outbox.main() == 0
    return constructed


def _run_ingest_outbox(monkeypatch: pytest.MonkeyPatch) -> _Constructed:
    constructed = _Constructed(SimpleNamespace(check_scan_floors=lambda: None))
    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    monkeypatch.setattr(ingest_outbox, "SpannerOutboxSource", constructed)
    monkeypatch.setattr(ingest_outbox, "ClickHouseWriter", lambda **_: None)
    monkeypatch.setattr(
        ingest_outbox,
        "drain_once",
        lambda *_, **__: ingest_outbox.DrainResult(fetched=0, inserted=0, rows_per_second=0.0),
    )
    assert ingest_outbox.main() == 0
    return constructed


def _run_refresh_workspace_directory(monkeypatch: pytest.MonkeyPatch) -> _Constructed:
    constructed = _Constructed(SimpleNamespace(fetch=lambda: []))
    monkeypatch.setattr(refresh_workspace_directory, "SpannerWorkspaceSource", constructed)
    assert refresh_workspace_directory.main(["--dry-run"]) == 0
    return constructed


def _run_rollup(monkeypatch: pytest.MonkeyPatch) -> _Constructed:
    constructed = _Constructed(SimpleNamespace(fetch=lambda **_: []))
    monkeypatch.setattr(rollup_reservation_overruns, "SpannerReservationSource", constructed)
    assert rollup_reservation_overruns.main(["--dry-run"]) == 0
    return constructed


def _run_verify_spanner_delivery(monkeypatch: pytest.MonkeyPatch) -> _Constructed:
    constructed = _Constructed(object())
    monkeypatch.setenv("CH_PASSWORD", "local-fake")
    monkeypatch.setattr(sys, "argv", ["verify"])
    monkeypatch.setattr(verify_spanner_delivery, "SpannerGenerationSource", constructed)
    monkeypatch.setattr(verify_spanner_delivery, "ClickHouse", lambda **_: None)
    monkeypatch.setattr(verify_spanner_delivery, "verify_delivery", lambda *_, **__: {"ok": True})
    monkeypatch.setattr(verify_spanner_delivery, "write_history", lambda *_: None)
    assert verify_spanner_delivery.main() == 0
    return constructed


_ENTRY_POINTS = {
    "ingest_operational_outbox": _run_ingest_operational_outbox,
    "ingest_outbox": _run_ingest_outbox,
    "refresh_workspace_directory": _run_refresh_workspace_directory,
    "rollup_reservation_overruns": _run_rollup,
    "verify_spanner_delivery": _run_verify_spanner_delivery,
}


@pytest.mark.parametrize("program", sorted(_ENTRY_POINTS))
@pytest.mark.parametrize("role", [None, ROLE])
def test_each_entry_point_passes_the_environment_role_to_its_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    program: str, role: str | None,
) -> None:
    if role is None:
        monkeypatch.delenv("TR_SPANNER_DATABASE_ROLE", raising=False)
    else:
        monkeypatch.setenv("TR_SPANNER_DATABASE_ROLE", role)

    constructed = _ENTRY_POINTS[program](monkeypatch)

    [kwargs] = constructed.kwargs
    assert kwargs["database_role"] == role
    capsys.readouterr()
