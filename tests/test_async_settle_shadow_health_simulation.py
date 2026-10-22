"""Real worker/storage execution with serialized claim-row transactions."""
import time

import pytest

from tests.shadow_health_cold_start_simulation import simulate


@pytest.mark.parametrize("warm", [False, True])
def test_serialized_fleet_after_warmup(warm):
    started = time.monotonic()
    result = simulate(warm=warm)
    assert result["measured_slots"] >= 120
    assert result["commits_per_slot"] <= 1.1
    assert result["max_60_slot_commits"] <= 1.1
    assert result["rate"] >= .95
    assert result["unknown_ratio"] <= .01
    assert result["max_published_observation_ms"] <= 250
    assert time.monotonic()-started < 20


def test_serialized_fleet_survives_aborted_writer():
    result = simulate(warm=True, abort=True)
    assert result["aborts"] == 1
    assert result["rate"] >= .95
    assert result["unknown_ratio"] == 0
    assert result["max_60_slot_commits"] <= 1.1
