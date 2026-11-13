"""ClickHouse node timers run on a wall-clock schedule.

OnUnitActiveSec counts from the service's last run. If the timer is restarted
more than one interval after that run, systemd computes a trigger in the past
and the timer never fires again. The 2026-11-11 roll for #2486 stalled the
workspace directory refresh this way for 33 hours.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TIMERS = sorted((ROOT / "clickhouse").glob("*.timer"))


def _timer_settings(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    start = lines.index("[Timer]") + 1
    settings: list[str] = []
    for line in lines[start:]:
        if line.startswith("["):
            break
        if line and not line.startswith("#"):
            settings.append(line)
    return settings


def test_there_are_clickhouse_timers() -> None:
    assert len(TIMERS) >= 6


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_timer_uses_a_wall_clock_schedule(timer: Path) -> None:
    settings = _timer_settings(timer)
    assert not [s for s in settings if s.startswith(("OnUnitActiveSec", "OnUnitInactiveSec", "OnBootSec", "OnStartupSec", "OnActiveSec"))]
    assert [s for s in settings if s.startswith("OnCalendar=")]
    assert "Persistent=true" in settings
