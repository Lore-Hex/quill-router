"""Tests for scripts/check_price_coverage.py (price-source coverage audit)."""
from __future__ import annotations

import datetime as dt

from scripts.check_price_coverage import audit


def test_audit_flags_uncovered_provider_and_reports_covered() -> None:
    # Real repo state: Cohere is a prepaid provider with no scraper + no
    # manifest (hand-coded embedding prices) -> must be flagged as a gap.
    # If a Cohere scraper/manifest is ever added, update this expectation.
    now = dt.datetime(2026, 6, 7, tzinfo=dt.UTC)
    warnings, info = audit(max_age_days=14, now=now)
    assert any("cohere" in w for w in warnings), warnings
    # Live-scraped providers are reported as covered.
    assert any("openai" in i for i in info), info
