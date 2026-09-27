"""Tests judge catalog freshness at a pinned instant; live monitors do not.

See tests/catalog_freshness_freeze.py and the provider_health exemption in
tests/conftest.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from tests.catalog_freshness_freeze import CATALOG_FRESHNESS_INSTANT
from trusted_router import catalog_data


def test_tests_judge_catalog_freshness_before_every_committed_deadline() -> None:
    assert catalog_data._utc_now() == CATALOG_FRESHNESS_INSTANT == datetime(2000, 1, 1, tzinfo=UTC)


def test_the_app_built_at_import_judges_freshness_at_the_pinned_instant() -> None:
    # conftest imports trusted_router.main, whose module-level app prewarms
    # the public catalog cache before any fixture runs.
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [sys.executable, "-c", textwrap.dedent("""
            from trusted_router import catalog_data

            seen = []
            judge = catalog_data.ModelEndpoint.catalog_is_current

            def recording(self, *, at=None):
                seen.append(at or catalog_data._utc_now())
                return judge(self, at=at)

            catalog_data.ModelEndpoint.catalog_is_current = recording
            import tests.conftest  # noqa: F401 - builds the app, as pytest does

            from tests.catalog_freshness_freeze import CATALOG_FRESHNESS_INSTANT

            assert seen, "building the app judged no catalog freshness"
            assert set(seen) == {CATALOG_FRESHNESS_INSTANT}, sorted(set(seen))[-3:]
        """)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.provider_health
def test_live_monitors_judge_catalog_freshness_on_the_real_clock() -> None:
    # The social-card monitor compares the catalog with cards that
    # scripts/generate_provider_og.py rendered on the real clock.
    assert abs(catalog_data._utc_now() - datetime.now(UTC)) < timedelta(minutes=1)
