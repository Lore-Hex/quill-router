from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (
            lambda store: store.earnings_summary(
                "user-display",
                allow_stale=True,
            ),
            {"exact_staleness": dt.timedelta(seconds=5)},
        ),
        (
            lambda store: store.list_credit_movements("user:user-display"),
            {"exact_staleness": dt.timedelta(seconds=30)},
        ),
        (
            lambda store: store.custom_model_earnings_by_model(
                "user-display",
                since="2026-01-01T00:00:00Z",
            ),
            {"exact_staleness": dt.timedelta(seconds=60)},
        ),
        (
            lambda store: store.get_lifetime_topup_microdollars(
                "user-display",
                allow_stale=True,
            ),
            {"exact_staleness": dt.timedelta(seconds=5)},
        ),
        (
            lambda store: store.typed_key_usage("key-display", allow_stale=True),
            {
                "exact_staleness": dt.timedelta(seconds=5),
                "multi_use": True,
            },
        ),
    ],
    ids=[
        "earnings-summary",
        "credit-movement-history",
        "earnings-by-model-aggregate",
        "lifetime-topup-total",
        "api-key-usage-display",
    ],
)
def test_read_only_views_request_their_exact_bounded_staleness(
    read: Callable[[Any], Any],
    expected: dict[str, Any],
) -> None:
    store, database, _ = make_fake_store()

    read(store)

    assert database.snapshot_calls == [expected]


def test_earnings_summary_defaults_to_strong_for_transfer_results() -> None:
    store, database, _ = make_fake_store()

    store.earnings_summary("user-transfer")

    assert database.snapshot_calls == [{}]


def test_lifetime_topup_defaults_to_strong_for_write_verification() -> None:
    store, database, _ = make_fake_store()

    store.get_lifetime_topup_microdollars("user-backfill")

    assert database.snapshot_calls == [{}]


def test_non_display_and_correctness_sensitive_reads_remain_strong() -> None:
    store, database, _ = make_fake_store()

    cases: list[tuple[Callable[[], Any], list[dict[str, Any]]]] = [
        # The missing typed authorization falls back to the legacy entity row;
        # both point reads must stay strong because settlement consumes it.
        (lambda: store.get_gateway_authorization("gwa-missing"), [{}, {}]),
        (lambda: store.typed_credit_snapshot("ws-missing"), [{"multi_use": True}]),
        (lambda: store.read_typed_reservation("res-missing"), [{}]),
        (lambda: store.is_typed_reservation("res-missing", "gwa-missing"), [{}]),
        (
            lambda: store.get_typed_authorization_by_idempotency(
                "ws-missing",
                "key-missing",
                "idem-missing",
            ),
            [{}],
        ),
        # Budget alerts use the default and must observe the just-settled usage;
        # an old below-threshold value could suppress their one-shot email.
        (lambda: store.typed_key_usage("key-alert"), [{"multi_use": True}]),
        (lambda: store._read_entity("user", "user-missing", dict), [{}]),
        (lambda: store._list_entities("member", cls=dict), [{}]),
    ]

    for read, expected in cases:
        database.snapshot_calls.clear()
        read()
        assert database.snapshot_calls == expected
