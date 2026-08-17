"""/status.json publishes this cloud's drain lag, on every cloud, always.

The signal existed before this and nothing called it. These tests pin the
wiring: the `analytics` key is present in every outcome, a read failure
publishes `available: false` rather than dropping the key or reusing the last
good number, `_compact_status_json` carries it out to the wire, nothing but a
fixed vocabulary of reasons reaches the uncredentialed page, and a database
that will not answer does not hold the status page open.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    BACKEND_MEMORY,
    BACKEND_POSTGRES,
    BACKEND_UNKNOWN,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    PUBLISHABLE_REASONS,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    OutboxFreshness,
    analytics_status_from_reading,
)
from trusted_router.routes import public as public_routes
from trusted_router.storage import STORE


def _snapshot(monkeypatch) -> dict[str, object]:
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    return public_routes._status_snapshot(Settings(environment="local"))


def test_status_snapshot_publishes_the_analytics_section(monkeypatch) -> None:
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness(
            backend=BACKEND_POSTGRES,
            oldest_enqueued_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=42),
        ),
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(section, dict)
    assert section["available"] is True
    assert section["backend"] == BACKEND_POSTGRES
    assert 40 <= float(section["drain_lag_seconds"]) < DEFAULT_MAX_DRAIN_LAG_SECONDS
    assert section["generated_at"].endswith("Z")


def test_a_drained_outbox_publishes_zero_lag_not_a_missing_key(monkeypatch) -> None:
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=None),
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(section, dict)
    assert section["available"] is True
    assert section["drain_lag_seconds"] == 0.0


def test_a_raising_store_publishes_unavailable_and_never_omits_the_key(monkeypatch) -> None:
    """Dropping the key on failure would read as "this cloud runs older code".

    The fleet checker has a separate, louder branch for that, and it tells the
    operator to redeploy. A database that cannot be read is a different problem
    with a different fix, so it gets a different published shape.
    """

    def boom() -> OutboxFreshness:
        raise RuntimeError("dsql: connection refused")

    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        boom,
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert section == {"available": False, "reason": REASON_UNREACHABLE}


def test_a_failed_read_never_republishes_the_last_good_number(monkeypatch) -> None:
    """A stale-but-plausible lag is indistinguishable from a healthy one."""
    readings = [
        OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=None),
        OutboxFreshness.unavailable(BACKEND_POSTGRES, REASON_UNREACHABLE),
    ]
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: readings.pop(0),
        raising=False,
    )

    healthy = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]
    broken = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert isinstance(healthy, dict) and healthy["available"] is True
    assert broken == {"available": False, "reason": REASON_UNREACHABLE}


def test_the_in_memory_backend_says_not_configured_rather_than_zero() -> None:
    """No outbox and no drain must not publish the healthiest possible number."""
    reading = STORE.operational_analytics_outbox_freshness()

    assert reading.available is False
    assert reading.backend == BACKEND_MEMORY
    assert reading.reason == REASON_NOT_CONFIGURED


def test_status_json_carries_the_section_to_the_wire(
    client: TestClient,
    monkeypatch,
) -> None:
    """_compact_status_json strips tooltip data; it must not strip this."""
    section = {"available": True, "backend": "postgres", "drain_lag_seconds": 1.5}
    monkeypatch.setattr(
        public_routes,
        "_status_snapshot",
        lambda _settings: {"components": [], ANALYTICS_STATUS_KEY: section},
    )

    response = client.get("/status.json")

    assert response.status_code == 200
    assert response.json()["data"][ANALYTICS_STATUS_KEY] == section


# ---------------------------------------------------------------------------
# The stale-cache paths. "Never the last good number" has to be true on the
# paths that exist to serve the last good everything-else.
# ---------------------------------------------------------------------------


def test_the_stale_cache_fallback_re_reads_the_lag_instead_of_replaying_it(
    monkeypatch,
) -> None:
    """The docstring's claim, checked on the path that would have broken it.

    `_status_snapshot` has two fallbacks that re-serve the previous payload
    wholesale when a fresh build fails. Carried along unchanged, the analytics
    section would be exactly the last good number -- a small, plausible lag,
    frozen, ageing into a lie while the outbox grows. Everything else in that
    payload may be stale; this field may not.
    """
    settings = Settings(environment="local")
    cached = {"components": [], ANALYTICS_STATUS_KEY: {"available": True, "drain_lag_seconds": 1.0}}
    # Deliberately EXPIRED, so the fresh build is attempted, fails, and the
    # fallback that re-serves this payload is the path under test.
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", (time.monotonic() - 86_400, cached))

    def boom(**_kwargs):
        raise RuntimeError("live status sample read failed")

    monkeypatch.setattr(public_routes, "_status_samples", boom)
    monkeypatch.setattr(public_routes, "_precomputed_public_analytics_snapshot", lambda _name: None)
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness.unavailable(BACKEND_POSTGRES, REASON_UNREACHABLE),
        raising=False,
    )

    payload = public_routes._status_snapshot(settings)

    assert payload[ANALYTICS_STATUS_KEY] == {
        "available": False,
        "reason": REASON_UNREACHABLE,
    }
    # The rest of the stale payload is still served; only this field is refreshed.
    assert payload["components"] == []


# ---------------------------------------------------------------------------
# Privacy: /status.json carries no credentials, and neither may its contents.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaky_reason",
    [
        'connection to server at "tr-eu.dsql.eu-west-3.on.aws" (10.0.3.17), port 5432 failed',
        'OperationalError: FATAL: role "quill-enclave-role" is not permitted to log in',
        "400 Table not found: tr_operational_analytics_outbox [at 1:22] project=quill-cloud-proxy",
        "",
        "unknown",
    ],
)
def test_an_arbitrary_reason_never_reaches_the_published_dict(leaky_reason: str) -> None:
    """The clamp, checked with the strings that are one edit away from real.

    Both backends build `str(exc)[:500]` on the line immediately above their
    `OutboxFreshness.unavailable(...)` call, for the log. Passing that value
    one field to the right is a tidy-looking change that would put DSQL and
    Spanner error text -- hostnames, VPC addresses, IAM role names, SQLSTATEs,
    project ids -- onto an UNCREDENTIALED page, and the fleet checker would go
    on to format the same string into a PUBLIC GitHub issue body.
    """
    published = analytics_status_from_reading(
        OutboxFreshness.unavailable(BACKEND_POSTGRES, leaky_reason),
        now=dt.datetime.now(dt.UTC),
    )

    assert published["reason"] in PUBLISHABLE_REASONS
    assert published["reason"] == REASON_UNREACHABLE
    # Nothing of the original string survives anywhere in the published object.
    assert published == {"available": False, "reason": REASON_UNREACHABLE}


def test_the_three_real_reasons_survive_the_clamp() -> None:
    """A clamp that flattened everything would be a clamp that says nothing."""
    for reason in PUBLISHABLE_REASONS:
        published = analytics_status_from_reading(
            OutboxFreshness.unavailable(BACKEND_POSTGRES, reason),
            now=dt.datetime.now(dt.UTC),
        )
        assert published["reason"] == reason


@pytest.mark.parametrize("hostile", [["unreachable"], {"reason": "unreachable"}, 17, None])
def test_a_reason_that_is_not_even_a_string_is_narrowed_rather_than_raising(
    hostile: object,
) -> None:
    """`x in frozenset` raises TypeError on an unhashable value.

    The clamp sat outside `_merge_analytics_status`'s try block, so a backend
    returning a list would have taken the exception straight through the status
    build -- dropping the whole `analytics` key, which the fleet checker reads
    as "this deployment runs code too old to publish drain lag" and answers by
    sending somebody to redeploy a healthy service. A clamp that raises on
    hostile input is not a clamp.
    """
    published = analytics_status_from_reading(
        OutboxFreshness(backend=BACKEND_POSTGRES, available=False, reason=hostile),  # type: ignore[arg-type]
        now=dt.datetime.now(dt.UTC),
    )

    assert published == {"available": False, "reason": REASON_UNREACHABLE}


@pytest.mark.parametrize("hostile", [["postgres"], {"backend": "postgres"}, 17])
def test_a_backend_that_is_not_even_a_string_is_narrowed_rather_than_raising(
    hostile: object,
) -> None:
    """Same total-function requirement, same reason, the other clamped field."""
    published = analytics_status_from_reading(
        OutboxFreshness(backend=hostile, oldest_enqueued_at=None),  # type: ignore[arg-type]
        now=dt.datetime.now(dt.UTC),
    )

    assert published["backend"] == BACKEND_UNKNOWN


def test_a_store_that_returns_nonsense_still_leaves_the_key_published(monkeypatch) -> None:
    """Whatever goes wrong in the projection, the section is written.

    An omitted key is not a neutral outcome: it is the one shape that tells the
    fleet checker to send somebody to redeploy.
    """
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at="yesterday"),  # type: ignore[arg-type]
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert section == {"available": False, "reason": REASON_UNREACHABLE}


def test_an_unrecognised_backend_name_is_narrowed_too() -> None:
    """Same contract, applied to every published value, as client_reliability does."""
    published = analytics_status_from_reading(
        OutboxFreshness(backend="dsql://tr-eu.eu-west-3.on.aws", oldest_enqueued_at=None),
        now=dt.datetime.now(dt.UTC),
    )

    assert published["backend"] == BACKEND_UNKNOWN


def test_a_leaky_reason_cannot_reach_the_wire_either(client: TestClient, monkeypatch) -> None:
    """End to end, because the clamp is only worth what the wire says it is."""
    monkeypatch.setattr(public_routes, "_status_samples", lambda **_kwargs: [])
    monkeypatch.setattr(public_routes, "_status_rollups", lambda _window: [])
    monkeypatch.setattr(public_routes, "_STATUS_CACHE", None)
    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        lambda: OutboxFreshness.unavailable(
            BACKEND_POSTGRES, 'FATAL: password authentication failed for "tr_app"'
        ),
        raising=False,
    )

    response = client.get("/status.json")

    assert response.status_code == 200
    assert "password authentication failed" not in response.text
    assert response.json()["data"][ANALYTICS_STATUS_KEY]["reason"] == REASON_UNREACHABLE


# ---------------------------------------------------------------------------
# A database that will not answer must not hold the status page open.
# ---------------------------------------------------------------------------


def test_a_backend_that_answers_slowly_does_not_hold_the_status_build_open(
    monkeypatch,
) -> None:
    """The bound this module OWNS, tested by exceeding it twentyfold.

    The failure it prevents: this read sits on the public /status.json build
    path inside an async handler, so a blocking wait there stops the EVENT LOOP
    -- every request this process is serving, not one worker thread.

    This is written to FAIL against an unbounded implementation, which the
    previous version of it could not: a backend that slept 0.25s under a 2.0s
    assertion passes whether anything bounds it or not. Here the backend takes
    a full second, the bound is 50ms, and the assertion is a tenth of a second
    -- so the only way to pass is to stop waiting. It also does not rely on the
    store's own timeouts: this backend does not have any, which is precisely
    the case (a driver that ignores its cap, a backend added later) the
    caller-side bound exists for.
    """
    monkeypatch.setattr(public_routes, "STATUS_ANALYTICS_READ_TIMEOUT_SECONDS", 0.05)

    def answers_eventually() -> OutboxFreshness:
        time.sleep(1.0)
        return OutboxFreshness(backend=BACKEND_POSTGRES, oldest_enqueued_at=None)

    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        answers_eventually,
        raising=False,
    )

    started = time.monotonic()
    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]
    elapsed = time.monotonic() - started

    assert section == {"available": False, "reason": REASON_UNREACHABLE}
    assert elapsed < 0.5, "the status build waited on the backend rather than bounding it"


def test_a_backend_that_raises_after_its_own_timeout_publishes_unavailable(
    monkeypatch,
) -> None:
    """The other half: the store's cap fires first and the page is still served."""

    def times_out() -> OutboxFreshness:
        time.sleep(0.05)
        raise TimeoutError("statement timeout")

    monkeypatch.setattr(
        STORE.target,
        "operational_analytics_outbox_freshness",
        times_out,
        raising=False,
    )

    section = _snapshot(monkeypatch)[ANALYTICS_STATUS_KEY]

    assert section == {"available": False, "reason": REASON_UNREACHABLE}


def test_the_caller_side_bound_is_above_the_drivers_own_caps() -> None:
    """Ordering matters: the driver's cap should be the one that normally fires.

    It can cancel the statement; this one can only stop waiting for it. A
    caller bound BELOW the driver's would abandon reads the database was about
    to answer and publish `unavailable` on a healthy cloud.
    """
    from trusted_router import storage_postgres

    assert (
        public_routes.STATUS_ANALYTICS_READ_TIMEOUT_SECONDS
        > storage_postgres.OUTBOX_FRESHNESS_TIMEOUT_SECONDS
    )


def test_the_postgres_lag_read_bounds_the_pool_wait_and_the_statement() -> None:
    """Supplementary, and stated as such: this reads source text, not behaviour.

    The behavioural bound is
    `test_a_backend_that_answers_slowly_does_not_hold_the_status_build_open`
    above, which fails against an unbounded implementation. What string
    matching adds is WHICH waits the driver caps -- both of them, because
    either alone is still unbounded: an exhausted or unreachable pool blocks
    before any SQL is issued, so a statement timeout never gets its chance,
    while a healthy connection to a cluster that has stopped answering blocks
    after it. Neither is reachable from a unit test without a real pool, so it
    is checked here as a drift guard rather than offered as the proof.
    """
    import inspect

    from trusted_router import storage_postgres

    source = inspect.getsource(
        storage_postgres.PostgresStore.operational_analytics_outbox_freshness
    )

    assert "connection(timeout=OUTBOX_FRESHNESS_TIMEOUT_SECONDS)" in source
    assert "SET LOCAL statement_timeout" in source
    # NOT through _run_transaction: it retries serialization failures, and a
    # retry loop turns a bounded read back into an unbounded one.
    assert "self._run_transaction(" not in source
    assert storage_postgres.OUTBOX_FRESHNESS_TIMEOUT_SECONDS <= 5.0


def test_the_spanner_lag_read_bounds_the_whole_shard_sweep() -> None:
    """The budget bounds the CALL, and the call is now a single statement.

    The previous shape handed each of 32 statements the REMAINING budget and
    raised TimeoutError itself when it ran out. That arithmetic existed to stop
    a per-statement bound from silently becoming a 32x one -- and it worked,
    but the underlying read still cost 9.76s against a 3.0s budget in
    production, so it timed out every time and published `unreachable` for a
    healthy drain. One statement removes the arithmetic and the problem
    together: what the client is given IS the ceiling on the whole call.

    The remaining risk -- a client that ignores its own timeout -- is covered
    not here but by the caller-side bound, proven behaviourally in
    test_a_backend_that_answers_slowly_does_not_hold_the_status_build_open.
    """
    from trusted_router.storage_gcp_operational_analytics_outbox import (
        SpannerOperationalAnalyticsOutbox,
    )

    calls: list[float] = []

    class _Snapshot:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def execute_sql(self, _sql, **kwargs):
            calls.append(kwargs["timeout"])
            return [[None]]

    class _Database:
        def snapshot(self, **_kwargs):
            return _Snapshot()

    class _ParamTypes:
        INT64 = "INT64"

    outbox = SpannerOperationalAnalyticsOutbox(_Database(), _ParamTypes())

    outbox.oldest_enqueued_at(timeout=0.1)

    assert calls == [0.1]
