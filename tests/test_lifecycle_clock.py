from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests import lifecycle_clock
from tests.lifecycle_freeze import freeze_lifecycle_clock
from trusted_router import catalog_registry, provider_lifecycle
from trusted_router.catalog import endpoints_for_model
from trusted_router.provider_lifecycle import (
    _RETIREMENTS,
    LIFECYCLE_CLOCK_OVERRIDE_ENV,
    provider_model_retired,
)


@pytest.mark.parametrize("start_at_cutoff", [False, True])
def test_registry_import_crossing_fireworks_retirement(start_at_cutoff: bool) -> None:
    environ = dict(os.environ)
    environ.pop(LIFECYCLE_CLOCK_OVERRIDE_ENV, None)
    environ.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [sys.executable, "-c", textwrap.dedent("""
            import sys
            from datetime import UTC, datetime
            from trusted_router import provider_lifecycle

            before = datetime(2026, 9, 24, 23, 59, 59, 999999, tzinfo=UTC)
            after = datetime(2026, 9, 25, tzinfo=UTC)
            build_at = after if sys.argv[1] == "True" else before
            readings = iter([build_at])
            clock_reads = []
            def ticking_now():
                instant = next(readings, after)
                clock_reads.append(instant)
                return instant
            provider_lifecycle._utc_now = ticking_now
            from trusted_router import catalog_registry

            assert catalog_registry.CATALOG_RESOLVED_AT == build_at
            assert len(clock_reads) == 1, len(clock_reads)
            assert any(
                endpoint.provider == "fireworks"
                and endpoint.model_id == catalog_registry.DEEPSEEK_V4_PRO_0813_MODEL_ID
                for endpoint in catalog_registry.MODEL_ENDPOINTS.values()
            ) == (build_at < after)
            # The built registry retains its pre-cutover snapshot, while requests
            # after cutover must still retire these routes using the live clock.
            from trusted_router.catalog import endpoints_for_model
            assert not any(
                endpoint.provider == "fireworks"
                for endpoint in endpoints_for_model(
                    catalog_registry.DEEPSEEK_V4_PRO_0813_MODEL_ID
                )
            )
        """), str(start_at_cutoff)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_early_catalog_import_crossing_retirement_matches_release_contract() -> None:
    environ = dict(os.environ)
    environ.pop(LIFECYCLE_CLOCK_OVERRIDE_ENV, None)
    environ.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [sys.executable, "-c", textwrap.dedent("""
            from datetime import UTC, datetime
            from trusted_router import provider_lifecycle

            before = datetime(2026, 9, 24, 23, 59, 59, 999999, tzinfo=UTC)
            cutoff = datetime(2026, 9, 25, tzinfo=UTC)
            real_now = provider_lifecycle._utc_now
            readings = iter([before])
            provider_lifecycle._utc_now = lambda: next(readings, cutoff)
            from trusted_router import catalog_registry
            provider_lifecycle._utc_now = real_now

            from tests.lifecycle_clock import CATALOG_CLOCK, catalog_predates
            from tests.test_catalog_routing_contracts import (
                test_deepseek_v4_pro_release_routes_are_keyed_and_credits_only,
            )

            assert CATALOG_CLOCK == catalog_registry.CATALOG_RESOLVED_AT == before
            assert provider_lifecycle._utc_now() == before
            assert any(
                endpoint.provider == "fireworks"
                and endpoint.model_id == catalog_registry.DEEPSEEK_V4_PRO_0813_MODEL_ID
                for endpoint in catalog_registry.MODEL_ENDPOINTS.values()
            ) == catalog_predates(cutoff)
            test_deepseek_v4_pro_release_routes_are_keyed_and_credits_only()
        """)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_lifecycle_clock_is_frozen_for_the_whole_process() -> None:
    # conftest pins before catalog import, or reuses an early plugin's catalog clock.
    stamp = os.environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV)
    assert stamp, "conftest must pin TR_LIFECYCLE_CLOCK_OVERRIDE"
    pinned = provider_lifecycle._effective_time(stamp)
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


def test_freeze_pins_the_session_start_when_nothing_is_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "trusted_router.catalog_registry")
    environ: dict[str, str] = {}
    now = datetime(2026, 9, 24, 23, 59, 58, 123456, tzinfo=UTC)
    clock = freeze_lifecycle_clock(environ, now=now)
    assert environ[LIFECYCLE_CLOCK_OVERRIDE_ENV] == "2026-09-24T23:59:58Z"
    assert clock == now.replace(microsecond=0)
    # The stamp round-trips through the production parser.
    assert provider_lifecycle._effective_time(environ[LIFECYCLE_CLOCK_OVERRIDE_ENV]) == clock


def test_freeze_reuses_an_early_catalog_timestamp_without_losing_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = datetime(2026, 9, 24, 23, 59, 59, 999999, tzinfo=UTC)
    after = datetime(2026, 9, 25, tzinfo=UTC)
    monkeypatch.setattr(catalog_registry, "CATALOG_RESOLVED_AT", before)
    environ: dict[str, str] = {}
    assert freeze_lifecycle_clock(environ, now=after) == before
    assert provider_lifecycle._effective_time(environ[LIFECYCLE_CLOCK_OVERRIDE_ENV]) == before


def test_early_lifecycle_plugin_uses_the_same_clock_as_conftest() -> None:
    environ = dict(os.environ)
    environ.pop(LIFECYCLE_CLOCK_OVERRIDE_ENV, None)
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [
            sys.executable, "-c", textwrap.dedent("""
                from datetime import UTC, datetime
                import pytest
                from trusted_router import provider_lifecycle

                before = datetime(2026, 9, 24, 23, 59, 59, 999999, tzinfo=UTC)
                real_now = provider_lifecycle._utc_now
                provider_lifecycle._utc_now = lambda: before
                from trusted_router import catalog_registry
                provider_lifecycle._utc_now = real_now

                result = pytest.main([
                    "-q", "-p", "no:cacheprovider", "-p", "tests.lifecycle_clock",
                    "tests/test_lifecycle_clock.py::test_the_lifecycle_clock_is_frozen_for_the_whole_process",
                ])
                assert result == 0
                from tests.lifecycle_clock import catalog_predates
                from trusted_router.catalog import endpoints_for_model

                assert catalog_registry.CATALOG_RESOLVED_AT == before
                assert catalog_predates(datetime(2026, 9, 25, tzinfo=UTC))
                assert any(
                    endpoint.provider == "fireworks"
                    for endpoint in endpoints_for_model(
                        catalog_registry.DEEPSEEK_V4_PRO_0813_MODEL_ID
                    )
                )
            """),
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
