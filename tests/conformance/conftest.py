"""Backend registry for the storage conformance suite.

Every test in this package talks to the `Store` Protocol and nothing else —
no backend-specific imports, no GCP types, no SQL. That is the whole point:
a new backend becomes conformant by being added to `BACKENDS` below and
passing this suite unchanged.

Why this exists
---------------
`tests/test_store_protocol_conformance.py` checks that both backends declare
the same method *names* and signatures. It cannot see behaviour, so a backend
whose `credit_workspace_once` double-credits on retry, or whose
`consume_verification_token` allows a token to be redeemed twice, passes it
happily. Those are exactly the divergences that cost money or leak access.

This suite pins the *semantics* instead, so they are an executable contract
rather than tribal knowledge living in one implementation.

Backend availability
--------------------
`memory` always runs. Backends that need a live server (the Spanner/Bigtable
emulators today, a Postgres container later) are opt-in: their factory calls
`pytest.skip()` when the server isn't reachable, so the suite stays green on a
laptop and gains real cross-backend enforcement in CI. A backend that is
skipped proves nothing — see `test_backend_coverage.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from trusted_router.store_protocol import Store

# --------------------------------------------------------------------------
# Backend factories
# --------------------------------------------------------------------------


def _memory_store() -> Store:
    from trusted_router.storage import InMemoryStore

    return InMemoryStore()


def _spanner_emulator_store() -> Store:
    """`SpannerBigtableStore` pointed at the Google emulators.

    `SpannerBigtableStore.__init__` eagerly opens clients, and the Google
    client libraries route to a local emulator when SPANNER_EMULATOR_HOST /
    BIGTABLE_EMULATOR_HOST are exported. Both are required: this store spans
    two services and a half-configured one fails in a confusing way partway
    through a test rather than skipping cleanly here.

    Schema provisioning against the emulator is deliberately NOT done here —
    it is the next increment. Until then this backend skips, and the suite
    documents the gap instead of pretending to cover it.
    """
    spanner_host = os.environ.get("SPANNER_EMULATOR_HOST")
    bigtable_host = os.environ.get("BIGTABLE_EMULATOR_HOST")
    if not (spanner_host and bigtable_host):
        pytest.skip(
            "Spanner/Bigtable emulators not configured "
            "(export SPANNER_EMULATOR_HOST and BIGTABLE_EMULATOR_HOST)"
        )
    if not os.environ.get("TR_CONFORMANCE_EMULATOR_SCHEMA"):
        pytest.skip(
            "emulator schema provisioning not implemented yet "
            "(set TR_CONFORMANCE_EMULATOR_SCHEMA=1 once it lands)"
        )
    from trusted_router.storage_gcp import SpannerBigtableStore

    return SpannerBigtableStore(
        project_id=os.environ.get("TR_CONFORMANCE_PROJECT", "tr-conformance"),
        spanner_instance_id=os.environ.get("TR_CONFORMANCE_SPANNER_INSTANCE", "tr-test"),
        spanner_database_id=os.environ.get("TR_CONFORMANCE_SPANNER_DB", "tr-test"),
        bigtable_instance_id=os.environ.get("TR_CONFORMANCE_BIGTABLE_INSTANCE", "tr-test"),
        generation_table=os.environ.get(
            "TR_CONFORMANCE_BIGTABLE_TABLE", "trustedrouter-generations"
        ),
    )


#: Add a backend here and it must pass every test in this package.
BACKENDS: dict[str, Callable[[], Store]] = {
    "memory": _memory_store,
    "spanner-emulator": _spanner_emulator_store,
}


@pytest.fixture(params=sorted(BACKENDS), ids=lambda name: f"backend={name}")
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    """A live store for each registered backend.

    The factory may `pytest.skip()` when its server isn't available; that is
    reported per-backend so a skipped backend is visible rather than silently
    counted as a pass.
    """
    backend = BACKENDS[request.param]()
    yield backend


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.fixture
def workspace_id(store: Store) -> str:
    """A workspace with an explicit ZERO starter credit.

    Explicit is load-bearing twice over. The repo-root `auto_credit_test_
    workspaces` autouse fixture grants $10 to any workspace created without an
    explicit amount, which would make credit assertions here measure the
    fixture instead of the backend. And a backend-neutral test must not depend
    on any backend's default granting policy.
    """
    user = store.ensure_user("conformance-user", "conformance@example.com")
    ws = store.create_workspace(user.id, "conformance", trial_credit_microdollars=0)
    return str(ws.id)


def make_benchmark_sample(
    *,
    sample_id: str,
    provider: str,
    model: str,
    status: str = "success",
    created_at: str | None = None,
) -> Any:
    """A minimal `ProviderBenchmarkSample`.

    Only the fields the index contract depends on are set; everything else
    keeps its dataclass default so this helper doesn't quietly encode
    assumptions about unrelated columns.
    """
    from trusted_router.storage_models import ProviderBenchmarkSample
    from trusted_router.types import UsageType

    kwargs: dict[str, Any] = {
        "id": sample_id,
        "model": model,
        "provider": provider,
        "provider_name": provider,
        "status": status,
        "usage_type": UsageType.CREDITS,
        "streamed": True,
        "source": "synthetic",
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    return ProviderBenchmarkSample(**kwargs)
