"""Every ClickHouse node timer runs on the wall clock (OnCalendar).

A timer whose only triggers are OnBootSec= and OnUnitActiveSec= can be left
with no next run. If systemd reloads after the boot offset has passed and before
the service has run once in this boot, neither trigger has a base to count
from: on 2026-11-11 the workspace directory timer on tr-clickhouse-1 stayed
active (waiting) with no next elapse for 33 hours after the node-config roll's
daemon-reload. A calendar time always has a next occurrence, and
Persistent=true runs one missed occurrence after downtime.
"""

from __future__ import annotations

from pathlib import Path

TIMERS = sorted((Path(__file__).resolve().parents[1] / "clickhouse").glob("*.timer"))


def _settings(timer: Path) -> list[str]:
    return [line for line in timer.read_text().splitlines() if line and not line.startswith(("#", "["))]


def test_there_are_timers_to_check() -> None:
    assert len(TIMERS) >= 10


def test_every_timer_runs_on_a_calendar_and_catches_up_after_downtime() -> None:
    for timer in TIMERS:
        settings = _settings(timer)
        assert any(line.startswith("OnCalendar=") for line in settings), timer.name
        assert "Persistent=true" in settings, timer.name


def test_no_timer_counts_from_boot_or_from_its_last_run() -> None:
    for timer in TIMERS:
        monotonic = [
            line for line in _settings(timer)
            if line.startswith(("OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec", "OnActiveSec"))
        ]
        assert not monotonic, (timer.name, monotonic)
