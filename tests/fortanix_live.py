"""Run the Fortanix live-route tests as of just before its suspension.

Fortanix suspends its GLM-5.3-Flash endpoint from 2026-11-27 00:00 UTC
(provider_lifecycle.FORTANIX_GLM_53_FLASH_SUSPENSION_AT) and is expected back
around early January 2027. The launch-offer and internal-route tests cover the
route while it is live; tests/test_fortanix_suspension.py covers the
suspension. On a lifecycle clock past the cutover (the post-cutover CI job, or
the wall clock after 11-27) this pins request-time lifecycle filters one
second before it and puts back the endpoints the registry left out at import,
for one test, so those tests keep their assertions. Delete this module, its
callers' fixtures and the suspension entry when Fortanix is re-admitted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from tests.fixture_routes import clear_catalog_caches
from trusted_router import catalog_registry
from trusted_router.provider_lifecycle import (
    FORTANIX_GLM_53_FLASH_SUSPENSION_AT,
    LIFECYCLE_CLOCK_OVERRIDE_ENV,
    _utc_now,
)

_ENDPOINTS = (
    catalog_registry._fortanix_endpoint,
    catalog_registry._fortanix_public_endpoint,
)


@pytest.fixture
def fortanix_route_live(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Request with the route live; cached catalog projections never outlive it."""
    if _utc_now() < FORTANIX_GLM_53_FLASH_SUSPENSION_AT:
        yield
        return
    before = FORTANIX_GLM_53_FLASH_SUSPENSION_AT - timedelta(seconds=1)
    monkeypatch.setenv(LIFECYCLE_CLOCK_OVERRIDE_ENV, before.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for endpoint in _ENDPOINTS:
        if endpoint.id not in catalog_registry.MODEL_ENDPOINTS:
            monkeypatch.setitem(catalog_registry.MODEL_ENDPOINTS, endpoint.id, endpoint)
    clear_catalog_caches()
    yield
    monkeypatch.undo()
    clear_catalog_caches()
