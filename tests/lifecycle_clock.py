"""Which side of a scheduled cutover this test process's catalog was built on.

`trusted_router.catalog_registry` resolves retirements ONCE, at import: a
retired endpoint is absent from `MODEL_ENDPOINTS`, and a model whose every
route has retired is absent from `MODELS` entirely. Monkeypatching
`provider_lifecycle._utc_now` inside a test cannot put those rows back, so a
test that wants to assert the pre-cutover catalog has to ask which clock built
it rather than assume the answer is "before".

Assuming it is what turned main red for every PR at 2026-08-17 00:00 UTC (CI
run 31980690855). Taking a second clock reading here instead of asking the
registry is what turned main red once more at 2026-09-25 00:00 UTC (CI run
36074914138): this module imported before midnight, the request-time filters
ran after it. `CATALOG_CLOCK` is therefore the instant the registry itself
recorded, and tests/conftest.py pins the whole process to one instant through
`TR_LIFECYCLE_CLOCK_OVERRIDE` (see tests/lifecycle_freeze.py). The
`test-post-cutover` job in ci.yml runs the suite with that override pinned
past the latest scheduled cutover so a cutover-dependent assumption fails on
the pull request that introduces it instead of at midnight.
"""

from __future__ import annotations

from datetime import datetime

from tests.lifecycle_freeze import OVERRIDE_WAS_EXPLICIT
from trusted_router import catalog_registry

# The clock the catalog was resolved with: not a fresh reading.
CATALOG_CLOCK: datetime = catalog_registry.CATALOG_RESOLVED_AT
LIFECYCLE_CLOCK_OVERRIDDEN: bool = OVERRIDE_WAS_EXPLICIT


def catalog_predates(cutover: datetime) -> bool:
    """True when the import-time catalog still carries `cutover`'s routes."""
    return CATALOG_CLOCK < cutover
