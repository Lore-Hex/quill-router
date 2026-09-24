from __future__ import annotations

from typing import Any

import pytest

from trusted_router import activity_mirror_reconcile_cli as cli


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def reconcile_generation_activity(
        self, workspace_id: str, *, date: str | None = None, limit: int = 1000
    ) -> int:
        self.calls.append((workspace_id, date, limit))
        return 7


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
    assert wired["store"].calls == [("ws_1", "2026-09-24", 5)]
    assert wired["configured"] == [wired["store"]]
    assert capsys.readouterr().out.strip() == "repaired=7"


def test_date_is_optional_and_limit_defaults(wired: dict[str, Any]) -> None:
    assert cli.main(["--workspace-id", "ws_2"]) == 0
    assert wired["store"].calls == [("ws_2", None, 1000)]


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
