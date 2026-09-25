"""Freeze the lifecycle clock for the whole test process, reusing an early
catalog import's timestamp if necessary.

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
import sys
from collections.abc import MutableMapping
from datetime import UTC, datetime

from trusted_router.provider_lifecycle import LIFECYCLE_CLOCK_OVERRIDE_ENV, _effective_time

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def freeze_lifecycle_clock(
    environ: MutableMapping[str, str], now: datetime | None = None
) -> datetime:
    """Keep an override, or pin to an existing catalog's timestamp or session start."""
    existing = environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV)
    if existing:
        return _effective_time(existing)
    registry = sys.modules.get("trusted_router.catalog_registry")
    if registry is not None:
        instant = registry.CATALOG_RESOLVED_AT
    else:
        instant = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    environ[LIFECYCLE_CLOCK_OVERRIDE_ENV] = instant.isoformat().replace("+00:00", "Z")
    return instant


# Capture caller intent before the automatic pin so live provider-health
# monitors only skip for explicit overrides, not ordinary test sessions.
OVERRIDE_WAS_EXPLICIT: bool = bool(os.environ.get(LIFECYCLE_CLOCK_OVERRIDE_ENV))

# conftest normally imports this before the catalog. Earlier plugin/application
# imports instead supply the already-built catalog's exact timestamp.
FROZEN_AT: datetime = freeze_lifecycle_clock(os.environ)
