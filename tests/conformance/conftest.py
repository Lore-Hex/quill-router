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
emulators or a Postgres container) are opt-in: their factory calls
`pytest.skip()` when the server isn't reachable, so the suite stays green on a
laptop and gains real cross-backend enforcement in CI. A backend that is
skipped proves nothing, which is why `test_memory_backend_is_always_runnable`
deliberately does NOT depend on the parametrized `store` fixture — a guard
that can itself be skipped guards nothing.

Test isolation
--------------
`InMemoryStore` is constructed fresh per test, so it is isolated for free.
Server-backed backends are NOT: `SpannerBigtableStore.reset()` explicitly
refuses to wipe a real database. Tests therefore must not reuse fixed
identifiers across tests — every id is namespaced with the per-test `unique`
fixture below, so a shared emulator database stays order-independent and
uncontaminated between runs.
"""

from __future__ import annotations

import os
import uuid
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


def _spanner_fake_store() -> Store:
    """The REAL `SpannerBigtableStore`, over the in-process Spanner fake.

    This is the only backend in this table that executes `storage_gcp.py`, and
    it is the one that runs unconditionally in CI. That combination is the
    point of it.

    WHY IT EXISTS, since the obvious objection is "a fake proves nothing":
    `spanner-pg` below constructs a **PostgresStore**. It points that store at a
    Spanner server, which tests Spanner's SQL DIALECT — genuinely valuable — but
    it never executes one line of the native-Spanner store, so it cannot cover
    the sharded money code GCP actually runs in production. `spanner-emulator`
    does construct that store, and skips unconditionally (no emulator schema
    provisioning). Between them the native store had NO runnable semantic
    coverage at all, which is how its cross-plane credit transfer sat
    unimplemented behind a comment saying it could not be tested.

    `tests/fakes/spanner.py` is not a stub: it models the read-set validation
    and abort-retry that make Spanner transactions serialize, duplicate-PK
    ALREADY_EXISTS, the DML/mutation mixing ban, and single-use snapshot
    exhaustion — each added because the corresponding real-Spanner behaviour
    leaked a production bug. `tests/test_fake_spanner_fidelity.py` guards those.
    It is the same harness ~28 existing test modules already use for the typed
    counter money path.

    WHAT IT DOES NOT PROVE, stated so nobody reads more into a green run: it is
    not the Spanner query planner and not its lock manager. It cannot catch an
    unsupported SQL construct, a DDL/schema mismatch, or a real ABORTED storm.
    Passing here means the STORE'S LOGIC is right; `spanner-emulator` is still
    the backend that would prove the SQL runs on Spanner, and it still skips.
    """
    from tests.fakes.spanner import make_fake_store

    store, _database, _bigtable = make_fake_store()
    return store


def _with_connect_timeout(dsn: str, seconds: int = 10) -> str:
    """Bound connection ESTABLISHMENT against a wedged server.

    On 2026-08-26 CI shard `test (6)` hung for 2.5 hours: PGAdapter's log shows
    a connection handshake beginning at 09:58:16 and nothing ever again, while
    psycopg waited forever -- its default connect timeout is infinite -- and
    every xdist worker eventually parked behind the same dead emulator. A
    conformance backend that cannot accept a connection within seconds is
    DOWN, and the honest outcome is a failed test naming the backend, not a
    silent six-hour runner burn.
    """
    if "connect_timeout" in dsn:
        return dsn
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}connect_timeout={seconds}"


def _postgres_store() -> Store:
    """A PostgresStore pointed at the conformance database.

    Honours the SAME IAM-auth switches production uses
    (TR_POSTGRES_IAM_AUTH / TR_POSTGRES_IAM_REGION). Without them this
    harness could only ever reach a password-authenticated Postgres, so
    Aurora DSQL — the backend the EU deployment actually runs on — was
    unreachable and every DSQL run died in the fixture with
    `fe_sendauth: no password supplied`, before exercising a single
    behaviour. A conformance suite that cannot connect to the real
    backend proves nothing about it, which is exactly how "Postgres-wire
    compatible" hid three DSQL incompatibilities previously.
    """
    dsn = os.environ.get("TR_CONFORMANCE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Postgres conformance backend not configured (set TR_CONFORMANCE_POSTGRES_DSN)")
    from trusted_router.storage_postgres import PostgresStore

    store = PostgresStore(
        _with_connect_timeout(dsn),
        postgres_iam_auth=os.environ.get("TR_POSTGRES_IAM_AUTH", ""),
        postgres_iam_region=os.environ.get("TR_POSTGRES_IAM_REGION", ""),
    )
    store.apply_schema()
    return store


def _spanner_pg_store() -> Store:
    """`PostgresStore` against **Spanner's PostgreSQL dialect**, via PGAdapter.

    This is the same implementation as the `postgres` backend, pointed at a
    different server — which is the whole claim being tested. Spanner PG is a
    *subset* of PostgreSQL, so "passes on Postgres 17" does not imply "passes
    on Spanner", and the difference lands on the credit path: exactly-once
    credit depends on `INSERT ... ON CONFLICT DO NOTHING` reporting rowcount
    correctly, and entities are stored as `jsonb`.

    If this backend passes, one implementation covers GCP, AWS and Azure, and
    the two-backends-with-different-money-code risk disappears instead of
    multiplying.

    Stand it up with no GCP credentials at all:

        docker run -d --rm --name tr-spanner-pg -p 5434:5432 \\
          gcr.io/cloud-spanner-pg-adapter/pgadapter-emulator:latest
        export TR_CONFORMANCE_SPANNER_PG_DSN=postgresql://localhost:5434/tr-conformance
    """
    dsn = os.environ.get("TR_CONFORMANCE_SPANNER_PG_DSN")
    if not dsn:
        pytest.skip(
            "Spanner PG-dialect conformance backend not configured "
            "(set TR_CONFORMANCE_SPANNER_PG_DSN — see this factory's docstring)"
        )
    from trusted_router.storage_postgres import PostgresStore

    store = PostgresStore(_with_connect_timeout(dsn))
    store.apply_schema()
    return store


#: Add a backend here and it must pass every test in this package.
BACKENDS: dict[str, Callable[[], Store]] = {
    "memory": _memory_store,
    "postgres": _postgres_store,
    "spanner-fake": _spanner_fake_store,
    "spanner-pg": _spanner_pg_store,
    "spanner-emulator": _spanner_emulator_store,
}


# Server-backed backends are marked into one xdist_group each: the Spanner PG
# emulator rejects CONCURRENT schema changes, and every server-backed store
# fixture applies schema on construction. Measured: plain `-n 4` against the
# real containers produced 14 setup errors on backend=spanner-pg; a serial run
# passed 53/53. With `--dist loadgroup` each backend's tests share one worker
# (DDL serialized) while memory-backend tests parallelize freely. The per-test
# `unique` fixture keeps the shared database ORDER-independent; this keeps it
# CONCURRENCY-safe too. Marks ride the fixture params (not
# collection_modifyitems) so xdist's scheduler sees them reliably.
#: Backends with no shared server: a fresh instance per test, so they need no
#: DDL serialization and parallelize freely.
_IN_PROCESS_BACKENDS = frozenset({"memory", "spanner-fake"})

_BACKEND_PARAMS = [
    name
    if name in _IN_PROCESS_BACKENDS
    else pytest.param(name, marks=pytest.mark.xdist_group(f"conformance-{name}"))
    for name in sorted(BACKENDS)
]


_C1_LEGACY_MONEY = (
    "native-Spanner store deliberately removed the legacy JSON reserve/settle/"
    "refund/finalize path (billing phase C1): GCP runs the TYPED authorize+settle "
    "path instead (storage_gcp_authorize.py / storage_gcp_counter_dml.py), which "
    "these Store-protocol methods do not route to. This is a real divergence "
    "from the Store contract, not a gap in the fake — the typed path has its own "
    "tests (tests/test_billing_typed_*.py), but it is NOT this suite's assertions."
)
_FAKE_ROLLUP_ORDERING = (
    "tests/fakes/spanner.py does not reproduce Bigtable's row ordering for "
    "synthetic rollups, so the limit-is-a-newest-first-prefix property cannot be "
    "asserted through it. A fake limitation, not a store claim either way."
)

#: Tests the `spanner-fake` backend is KNOWN not to satisfy, each with the
#: reason. Applied as **strict xfail**, deliberately, not skip:
#:
#:   * a skip is invisible in a green run and would let this backend read as
#:     coverage it does not have — the exact failure mode this suite exists to
#:     prevent (see the module docstring);
#:   * strict xfail fails the run if one of these starts PASSING, so the list
#:     cannot silently rot into a permanent excuse after somebody fixes the
#:     underlying divergence.
#:
#: Anything not listed here is genuinely asserted against the native Spanner
#: store, cross-plane credit transfer included.
_SPANNER_FAKE_KNOWN_GAPS: dict[str, str] = {
    "test_reserve_then_settle_less_releases_unused_hold": _C1_LEGACY_MONEY,
    "test_reserve_then_settle_more_books_full_actual": _C1_LEGACY_MONEY,
    "test_reserve_then_refund_restores_exact_balance": _C1_LEGACY_MONEY,
    "test_settle_is_idempotent": _C1_LEGACY_MONEY,
    "test_refund_is_idempotent": _C1_LEGACY_MONEY,
    "test_concurrent_reserves_cannot_oversubscribe": _C1_LEGACY_MONEY,
    "test_insufficient_reserve_does_not_mutate_balance": _C1_LEGACY_MONEY,
    "test_finalize_gateway_authorization_is_exactly_once": _C1_LEGACY_MONEY,
    "test_finalize_unknown_authorization_is_false_not_error": _C1_LEGACY_MONEY,
    "test_legacy_authorization_missing_frozen_hold_releases_zero": _C1_LEGACY_MONEY,
    "test_synthetic_rollups_apply_ranges_order_limit_and_histogram_option": (_FAKE_ROLLUP_ORDERING),
}


@pytest.fixture(params=_BACKEND_PARAMS, ids=lambda name: f"backend={name}")
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    """A live store for each registered backend.

    The factory may `pytest.skip()` when its server isn't available; that is
    reported per-backend so a skipped backend is visible rather than silently
    counted as a pass.
    """
    if request.param == "spanner-fake":
        gap = _SPANNER_FAKE_KNOWN_GAPS.get(
            getattr(request.node, "originalname", None) or request.node.name
        )
        if gap is not None:
            request.node.add_marker(pytest.mark.xfail(reason=gap, strict=True))
    backend = BACKENDS[request.param]()
    try:
        yield backend
    finally:
        close = getattr(backend, "close", None)
        if close is not None:
            close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.fixture
def unique() -> str:
    """A per-test identifier namespace.

    Server-backed backends share one database across the whole run and cannot
    be reset (see "Test isolation" above), so every id a test invents must be
    unique or tests contaminate each other — e.g. one test consuming event
    `evt-A` would make the next test's "this event is new" assertion fail
    purely because of ordering.
    """
    return uuid.uuid4().hex[:12]


@pytest.fixture
def user_id(store: Store, unique: str) -> str:
    """A REAL user, created through the store.

    Tests must not invent dangling foreign keys. Spanner's generic entity
    table happens to accept a fabricated `user_id`, but a backend with
    referential integrity (any SQL port with an FK, which is the likely shape
    of a Postgres backend) would correctly reject it — and would then fail
    this suite for being correct. Accepting orphaned rows is not a contract we
    want to freeze in.
    """
    user = store.ensure_user(f"conformance-user-{unique}", f"conf-{unique}@example.com")
    return str(user.id)


@pytest.fixture
def workspace_id(store: Store, user_id: str, unique: str) -> str:
    """A workspace with an explicit ZERO starter credit.

    Explicit is load-bearing twice over. The repo-root `auto_credit_test_
    workspaces` autouse fixture grants $10 to any workspace created without an
    explicit amount, which would make credit assertions here measure the
    fixture instead of the backend. And a backend-neutral test must not depend
    on any backend's default granting policy.
    """
    ws = store.create_workspace(user_id, f"conformance-{unique}", trial_credit_microdollars=0)
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


def make_synthetic_probe_sample(
    *,
    sample_id: str,
    target: str,
    probe_type: str,
    monitor_region: str,
    created_at: str,
    status: str = "up",
    latency_milliseconds: int | None = 50,
    ttfb_milliseconds: int | None = 25,
) -> Any:
    """A privacy-safe synthetic sample for the public-status contract."""
    from trusted_router.storage_models import SyntheticProbeSample

    return SyntheticProbeSample(
        id=sample_id,
        probe_type=probe_type,
        target=target,
        target_url=f"https://{target}.example.com/v1",
        monitor_region=monitor_region,
        status=status,
        latency_milliseconds=latency_milliseconds,
        ttfb_milliseconds=ttfb_milliseconds,
        created_at=created_at,
    )
