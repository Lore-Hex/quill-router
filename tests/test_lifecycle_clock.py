from __future__ import annotations

import importlib
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests import lifecycle_clock
from tests.lifecycle_freeze import STAMP_FORMAT, freeze_lifecycle_clock
from trusted_router import catalog_registry, provider_lifecycle
from trusted_router.catalog import endpoints_for_model
from trusted_router.provider_lifecycle import (
    _RETIREMENTS,
    LIFECYCLE_CLOCK_OVERRIDE_ENV,
    provider_model_retired,
)


def test_the_lifecycle_clock_is_frozen_for_the_whole_process() -> None:
    # conftest pinned the override before the catalog was imported.
    stamp = os.environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV)
    assert stamp, "conftest must pin TR_LIFECYCLE_CLOCK_OVERRIDE"
    pinned = datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
    first = provider_lifecycle._utc_now()
    time.sleep(0.01)
    second = provider_lifecycle._utc_now()
    assert first == second == pinned
    # Every reader of the clock agrees with the registry's recorded instant.
    assert catalog_registry.CATALOG_RESOLVED_AT == pinned
    assert lifecycle_clock.CATALOG_CLOCK == pinned


def test_freeze_keeps_an_override_ci_already_exported() -> None:
    environ = {LIFECYCLE_CLOCK_OVERRIDE_ENV: "2031-01-02T03:04:05Z"}
    clock = freeze_lifecycle_clock(environ, now=datetime(2026, 9, 25, tzinfo=UTC))
    assert clock == datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert environ[LIFECYCLE_CLOCK_OVERRIDE_ENV] == "2031-01-02T03:04:05Z"


def test_freeze_pins_the_session_start_when_nothing_is_exported() -> None:
    environ: dict[str, str] = {}
    now = datetime(2026, 9, 24, 23, 59, 58, 123456, tzinfo=UTC)
    clock = freeze_lifecycle_clock(environ, now=now)
    assert environ[LIFECYCLE_CLOCK_OVERRIDE_ENV] == "2026-09-24T23:59:58Z"
    assert clock == now.replace(microsecond=0)
    # The stamp round-trips through the production parser.
    assert provider_lifecycle._effective_time(environ[LIFECYCLE_CLOCK_OVERRIDE_ENV]) == clock


def test_catalog_predates_follows_the_registry_clock_not_a_second_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = catalog_registry.CATALOG_RESOLVED_AT
    cutover = resolved + timedelta(hours=1)
    # A helper that took its own reading now would land after the cutover.
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: resolved + timedelta(hours=2))
    try:
        reloaded = importlib.reload(lifecycle_clock)
        assert reloaded.CATALOG_CLOCK == resolved
        assert reloaded.catalog_predates(cutover) is True
    finally:
        monkeypatch.undo()
        importlib.reload(lifecycle_clock)
    assert lifecycle_clock.CATALOG_CLOCK == resolved


def test_a_retirement_the_catalog_clock_has_passed_is_absent_everywhere() -> None:
    # The registry's import-time filter and the request-time filter must give
    # the same answer as catalog_predates for every scheduled retirement.
    checked = 0
    for retirement in _RETIREMENTS:
        if lifecycle_clock.catalog_predates(retirement.effective_at):
            continue
        for model_id in retirement.model_ids:
            assert provider_model_retired(
                retirement.provider, model_id, at=lifecycle_clock.CATALOG_CLOCK
            )
            live = [
                endpoint
                for endpoint in catalog_registry.MODEL_ENDPOINTS.values()
                if endpoint.model_id == model_id and endpoint.provider == retirement.provider
            ]
            assert live == [], (retirement.provider, model_id)
            served = [
                endpoint
                for endpoint in endpoints_for_model(model_id)
                if endpoint.provider == retirement.provider
            ]
            assert served == [], (retirement.provider, model_id)
            checked += 1
    assert checked > 0, "no retirement has passed the catalog clock; nothing verified"
