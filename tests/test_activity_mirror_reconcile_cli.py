from __future__ import annotations

import json
from typing import Any

import pytest

from trusted_router import activity_mirror_reconcile_cli as cli
from trusted_router.storage_gcp_generations import ActivityReconcileResult, SpannerGenerations


class _Store(SpannerGenerations):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.generation_store = self
        self.result = ActivityReconcileResult(mirror_repaired=7)

    def reconcile_activity(self, workspace_id: str | None, **kwargs: Any) -> ActivityReconcileResult:
        self.calls.append({"workspace_id": workspace_id, **kwargs})
        return self.result


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"store": _Store(), "configured": []}
    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(cli, "init_sentry", lambda _settings: None)
    monkeypatch.setattr(cli, "create_store", lambda _settings: state["store"])
    monkeypatch.setattr(cli, "configure_store", state["configured"].append)
    return state


def test_re_mirrors_the_workspace_day_through_the_store(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["--workspace-id", "ws_1", "--date", "2026-09-24", "--limit", "5"])

    assert rc == 0
    assert wired["store"].calls == [{
        "workspace_id": "ws_1", "date": "2026-09-24", "limit": 5,
        "generation_id": None, "detailed": True, "after_id": None,
    }]
    assert wired["configured"] == [wired["store"]]
    assert json.loads(capsys.readouterr().out)["repaired"] == 7


def test_date_is_optional_and_limit_defaults(wired: dict[str, Any]) -> None:
    assert cli.main(["--workspace-id", "ws_2"]) == 0
    assert wired["store"].calls[0]["date"] is None
    assert wired["store"].calls[0]["limit"] == 1000


@pytest.mark.parametrize("argv", [["--workspace-id", "ws", "--date", "2026/09/24"], ["--workspace-id", "ws", "--limit", "0"], []])
def test_rejects_malformed_arguments(wired: dict[str, Any], argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert wired["store"].calls == []


def test_backend_without_reconcile_support_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    monkeypatch.setattr(cli, "create_store", lambda _settings: object())
    assert cli.main(["--workspace-id", "ws_1"]) == 1


@pytest.mark.parametrize("date", ["2026-99-99", "2026-02-29", "2026-04-31", "20260924"])
def test_rejects_noncalendar_dates(wired: dict[str, Any], date: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--workspace-id", "ws", "--date", date])
    assert error.value.code == 2
    assert wired["store"].calls == []


@pytest.mark.parametrize("date,after_id", [
    (None, "other_ws#2026-09-24#previous"),
    ("2026-09-24", "ws#2026-09-23#previous"),
])
def test_rejects_after_id_outside_workspace_day(
    wired: dict[str, Any], date: str | None, after_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["--workspace-id", "ws", "--after-id", after_id]
    if date is not None:
        argv.extend(["--date", date])
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2
    assert "--after-id must belong to the requested workspace/day" in capsys.readouterr().err
    assert wired["store"].calls == []
    assert wired["configured"] == []


@pytest.mark.parametrize("date", [None, "2026-09-24"])
def test_passes_matching_after_id_unchanged(wired: dict[str, Any], date: str | None) -> None:
    after_id = "ws#2026-09-24#previous"
    argv = ["--workspace-id", "ws", "--after-id", after_id]
    if date is not None:
        argv.extend(["--date", date])
    assert cli.main(argv) == 0
    assert len(wired["store"].calls) == 1
    assert wired["store"].calls[0]["after_id"] == after_id


def test_reports_mirror_failures_separately_from_durable_repairs(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    wired["store"].result = ActivityReconcileResult(
        mirror_repaired=1, durable_repaired=2, mirror_failed=["gen_failed"],
        truncated=True, next_after_id="ws#2026-09-24#last",
    )
    assert cli.main(["--workspace-id", "ws", "--after-id", "ws#2026-09-24#previous"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["repaired"] == 1
    assert report["durable_repaired"] == 2
    assert report["mirror_failed"] == ["gen_failed"]
    assert report["truncated"] is True
    assert report["next_after_id"] == "ws#2026-09-24#last"
    assert wired["store"].calls[0]["after_id"] == "ws#2026-09-24#previous"


def test_cli_repairs_typed_settlement_by_generation_id(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.fakes.spanner import make_fake_store
    from tests.test_bigtable_retirement import _authorize, _generation

    store, database, table = make_fake_store(
        request_record_write_mode="typed", operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
    )
    authorization, key = _authorize(store, "ws-typed-repair")
    generation = _generation(authorization, key.hash)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(type(table), "mutate_rows", unavailable)
        store.typed_finalize_gateway_authorization_result(
            authorization.id, success=True, actual_microdollars=900_000,
            selected_usage_type="Credits", generation=generation,
        )
    assert generation.id in database.generation_records
    assert not any(kind == "generation_by_workspace" for kind, _ in database.rows)
    assert not table.committed
    wired["store"] = store
    assert cli.main(["--generation-id", generation.id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["repaired"] == 1
    assert report["durable_repaired"] == 1
    assert len(table.committed) == 3


def test_cli_missing_generation_is_not_a_success(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.fakes.spanner import make_fake_store

    wired["store"], _, _ = make_fake_store(generation_records_enabled=True)
    assert cli.main(["--generation-id", "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["missing"] == ["missing"]
