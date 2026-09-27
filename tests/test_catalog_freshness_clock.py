"""Tests judge catalog freshness at a pinned instant; live monitors do not.

See catalog_freshness_before_every_deadline in tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trusted_router import catalog_data


def test_tests_judge_catalog_freshness_before_every_committed_deadline() -> None:
    assert catalog_data._utc_now() == datetime(2000, 1, 1, tzinfo=UTC)


@pytest.mark.provider_health
def test_live_monitors_judge_catalog_freshness_on_the_real_clock() -> None:
    # The social-card monitor compares the catalog with cards that
    # scripts/generate_provider_og.py rendered on the real clock.
    assert abs(catalog_data._utc_now() - datetime.now(UTC)) < timedelta(minutes=1)
