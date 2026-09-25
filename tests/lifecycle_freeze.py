"""Freeze the lifecycle clock for the whole test process, once, before any
catalog import.

`provider_lifecycle._utc_now()` is consulted at three different moments that
must agree: when `catalog_registry` resolves retirements at import, when
`tests.lifecycle_clock` records which side of a cutover the catalog was built
on, and on every request-time filter such as `endpoints_for_model`. Left on
the wall clock, a test process alive across a scheduled cutover sees the
first two before midnight and the third after it (main CI run 36074914138 on
2026-09-24 straddled the Fireworks retirement at 00:00 UTC that way).

Pinning `TR_LIFECYCLE_CLOCK_OVERRIDE` to the session start makes every
reading the same instant. An override that is already present (the
`test-post-cutover` job pins a date past every scheduled cutover) is kept.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from datetime import UTC, datetime

from trusted_router.provider_lifecycle import LIFECYCLE_CLOCK_OVERRIDE_ENV, _effective_time

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def freeze_lifecycle_clock(
    environ: MutableMapping[str, str], now: datetime | None = None
) -> datetime:
    """Pin the override to ``now`` unless one is already set; return the clock in force."""
    existing = environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV)
    if existing:
        return _effective_time(existing)
    instant = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    environ[LIFECYCLE_CLOCK_OVERRIDE_ENV] = instant.strftime(STAMP_FORMAT)
    return instant


# Pin on first import. tests/conftest.py imports this module before anything
# that imports the catalog, so the registry resolves against the same instant
# every later reader sees. Importing it again is harmless: an override that is
# already set is kept.
FROZEN_AT: datetime = freeze_lifecycle_clock(os.environ)
