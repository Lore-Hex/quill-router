"""Which side of a scheduled cutover this test process was started on.

`trusted_router.catalog_registry` resolves retirements ONCE, at import: a
retired endpoint is absent from `MODEL_ENDPOINTS`, and a model whose every
route has retired is absent from `MODELS` entirely. Monkeypatching
`provider_lifecycle._utc_now` inside a test cannot put those rows back, so a
test that wants to assert the pre-cutover catalog has to ask which clock built
it rather than assume the answer is "before".

Assuming it is what turned main red for every PR at 2026-08-17 00:00 UTC (CI
run 31980690855). The `test-post-cutover` job in ci.yml runs the suite with
`TR_LIFECYCLE_CLOCK_OVERRIDE` pinned past the latest scheduled cutover so that
assumption fails on the pull request that introduces it instead of at midnight.
"""

from __future__ import annotations

import os
from datetime import datetime

from trusted_router import provider_lifecycle

# Read at import, before any test monkeypatches `_utc_now`. Under
# TR_LIFECYCLE_CLOCK_OVERRIDE this is the pinned clock; otherwise it is the
# real one, within milliseconds of when catalog_registry was imported.
CATALOG_CLOCK: datetime = provider_lifecycle._utc_now()

LIFECYCLE_CLOCK_OVERRIDDEN: bool = bool(
    os.environ.get(provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV)
)


def catalog_predates(cutover: datetime) -> bool:
    """True when the import-time catalog still carries `cutover`'s routes."""
    return CATALOG_CLOCK < cutover
