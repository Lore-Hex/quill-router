"""Judge catalog freshness at one pinned instant for the whole test process.

The hourly price refresh runs this suite against the catalog it is about to
publish, and a provider held at its last published prices keeps its old
manifest. A test about routes, prices or billing must not fail, and block
every provider's publication, because a committed manifest passed its
deadline. Tests about freshness pass ``at=`` or build their deadlines from
``catalog_data._utc_now()``. The EXPIRED sentinel (``datetime.min``), which
marks an invalid manifest, stays non-current.

The pin is set at import, not in a fixture: ``trusted_router.main`` builds an
app at import, and that app prewarms the public catalog cache before any
fixture runs. conftest imports this module before the app.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trusted_router import catalog_data

# Before every committed provider manifest's deadline.
CATALOG_FRESHNESS_INSTANT = datetime(2000, 1, 1, tzinfo=UTC)

REAL_CATALOG_CLOCK = catalog_data._utc_now
catalog_data._utc_now = lambda: CATALOG_FRESHNESS_INSTANT
