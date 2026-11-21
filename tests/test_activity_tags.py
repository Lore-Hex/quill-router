"""Request tags for the console: the reads, their SQL, parity and privacy.

Tag keys and values in this file are invented.
"""

from __future__ import annotations

import hashlib
import logging
import random
from typing import Any, cast

import httpx
import pytest
import sentry_sdk
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.transport import Transport

from tests.activity_tags_recorded import (
    APPROXIMATE_CASES,
    EMPTY_WORKSPACE,
    EVENT_CASES,
    OTHER_WORKSPACE,
    START_AT,
    SUMMARY_CASES,
    WORKSPACE,
    fixture_generations,
    form_fields,
    limits,
    load_transcript,
    read_name,
    reader_statements,
    recorded_client,
    sql_store,
    statement,
)
from trusted_router import storage_activity_tags
from trusted_router.operational_analytics import (
    ELAPSED_CEILING_MS,
    ELAPSED_LAST_SLOT,
    ELAPSED_SLOT_MS,
    TAG_ELAPSED_CEILING_MS,
    TAG_ELAPSED_SLOTS,
    OperationalAnalyticsClient,
    _cell_array,
    _integer_array,
    _tuple_array,
    bound_values_stay_private,
    is_analytics_unavailable,
    log_analytics_unavailable,
    settle_elapsed_median,
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_activity_tags import (
    TAG_HISTOGRAM_READS,
    TagBreakdown,
    break_down_by_tag,
    clickhouse_breakdown,
    count_tag_keys,
    count_tag_values,
    elapsed_median,
    generations_in_window,
    order_breakdown,
    parse_window_start,
    tag_summary,
)
from trusted_router.storage_operational_analytics import analytics_surrogate

# One name for each statement shape the tag reads send: the five reads, and
# the three breakdown reads again with a key filter and with a tag filter.
READ_SHAPES = sorted(reader_statements())


def _memory_store() -> InMemoryStore:
    store = InMemoryStore()
    for generation in fixture_generations():
        store.add_generation(generation)
    return store


def _client(handler: Any) -> OperationalAnalyticsClient:
    return OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="tr_control_read",
        password="test-reader",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


def _window() -> list[Any]:
    return generations_in_window(fixture_generations(), workspace_id=WORKSPACE, start_at=START_AT)


def _row(kind: str, value: str | None, values: int, *numbers: Any) -> dict[str, Any]:
    names = (
        "requests", "cost_microdollars", "byok_microdollars", "prompt_tokens",
        "completion_tokens", "median_elapsed_ms",
    )
    return {"kind": kind, "value": value, "values": values, **dict(zip(names, numbers, strict=True))}


# ------------------------------------------------------------ pure functions


def test_elapsed_median_is_the_floor_of_the_two_middle_values_mean() -> None:
    assert elapsed_median([]) is None
    assert elapsed_median([30]) == 30
    assert elapsed_median([10, 21]) == 15
    assert elapsed_median([30, 10, 20]) == 20
    assert elapsed_median([40, 10, 30, 21]) == 25
    # At or past the tag ceiling (about 70 minutes) the histogram counts a
    # value in its open-ended last slot, so a median up there is absent, not
    # approximated.
    assert elapsed_median([TAG_ELAPSED_CEILING_MS - 1]) == TAG_ELAPSED_CEILING_MS - 1
    assert elapsed_median([TAG_ELAPSED_CEILING_MS]) is None
    assert elapsed_median([10, TAG_ELAPSED_CEILING_MS + 5]) is None
    assert elapsed_median([10, 20, TAG_ELAPSED_CEILING_MS + 5]) == 20


def test_window_start_reads_zulu_offset_and_naive_times_as_utc() -> None:
    assert parse_window_start("2026-03-01T00:00:00Z") == parse_window_start(
        "2026-03-01T00:00:00+00:00"
    )
    assert parse_window_start("2026-03-01T00:00:00").utcoffset() is not None
    # A fraction sorts after the whole second it extends, which a plain string
    # comparison gets wrong ("…00.250Z" < "…00Z").
    assert parse_window_start("2026-03-01T00:00:00.250Z") > parse_window_start(START_AT)


def test_window_keeps_one_workspace_from_the_start_and_matches_tags_exactly() -> None:
    generations = fixture_generations()

    # The replaced delivery of g02 is in the list twice; a store keeps one.
    assert sorted({gen.id for gen in _window()}) == [
        "g01", "g02", "g03", "g04", "g05", "g06", "g07", "g08", "g09", "g10", "g11", "g14",
        "g15", "g16",
    ]

    def matching(**tag: str | None) -> list[str]:
        store = _memory_store()
        found = generations_in_window(
            store.generation_store.generations.values(),
            workspace_id=WORKSPACE,
            start_at=START_AT,
            **tag,
        )
        return [gen.id for gen in found]

    assert len(generations) == 17
    assert sorted(matching(tag_key="environment")) == ["g01", "g02", "g03", "g05", "g10"]
    assert sorted(matching(tag_key="team", tag_value="search")) == ["g01", "g02", "g03", "g04"]
    # Case-sensitive, and an empty value is a value, not a missing key.
    assert matching(tag_key="team", tag_value="Search") == ["g11"]
    assert matching(tag_key="team", tag_value="") == ["g08"]
    assert matching(tag_key="cost-center") == []


def test_keys_and_values_rank_by_requests_then_text() -> None:
    store = _memory_store()
    window = generations_in_window(
        store.generation_store.generations.values(), workspace_id=WORKSPACE, start_at=START_AT
    )

    assert count_tag_keys(window) == [
        {"key": "team", "requests": 12, "overcount": 0},
        {"key": "environment", "requests": 5, "overcount": 0},
    ]
    # Equal counts fall back to the text: "ads" before "zeta", "" before "Search".
    assert [(row["value"], row["requests"]) for row in count_tag_values(window, tag_key="team")] == [
        ("search", 4), ("ads", 2), ("zeta", 2), ("", 1), ("Search", 1), ("billing", 1), ("delta", 1),
    ]


def test_breakdown_folds_the_tail_into_other_and_keeps_untagged_apart() -> None:
    store = _memory_store()
    window = generations_in_window(
        store.generation_store.generations.values(), workspace_id=WORKSPACE, start_at=START_AT
    )

    found = break_down_by_tag(window, group_by="team", limit=2)
    assert found is not None and found.approximate is False
    rows = order_breakdown(found.rows)

    assert rows == [
        _row("value", "search", 1, 4, 1350, 0, 190, 85, 400),
        # The cut is by cost: "billing" (800, one request) keeps its row and
        # "ads" (700 in credits, two requests) does not. Its one elapsed time
        # is at the ceiling, so it has no median.
        _row("value", "billing", 1, 1, 800, 0, 80, 40, None),
        # BYOK spend is reported apart from credit cost, as the usage chart does.
        _row("other", None, 5, 7, 750, 500, 145, 66, 800),
        _row("untagged", None, 0, 2, 100, 0, 17, 7, 45_150),
    ]
    # Nothing is lost: the rows add up to the window.
    assert sum(row["requests"] for row in rows) == len(window) == 14
    assert sum(row["prompt_tokens"] for row in rows) == sum(gen.tokens_prompt for gen in window)


@pytest.mark.parametrize(
    ("limit", "kept", "folded"),
    [
        # Cost decides before requests: billing over ads.
        (2, ["search", "billing"], 5),
        # At equal cost, more requests first: zeta over Search and delta.
        (5, ["search", "billing", "ads", "", "zeta"], 2),
        # At equal cost and requests the in-memory store orders by text.
        (6, ["search", "billing", "ads", "", "zeta", "Search"], 1),
        (7, ["search", "billing", "ads", "", "zeta", "Search", "delta"], 0),
    ],
)
def test_the_values_that_keep_a_row_are_the_most_expensive(
    limit: int, kept: list[str], folded: int
) -> None:
    found = break_down_by_tag(_memory_window(), group_by="team", limit=limit)
    assert found is not None
    rows = order_breakdown(found.rows)

    assert [row["value"] for row in rows if row["kind"] == "value"] == kept
    assert [row["values"] for row in rows if row["kind"] == "other"] == ([folded] if folded else [])


def _memory_window() -> list[Any]:
    store = _memory_store()
    return generations_in_window(
        store.generation_store.generations.values(), workspace_id=WORKSPACE, start_at=START_AT
    )


def test_a_key_with_too_many_values_is_refused_not_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_activity_tags, "TAG_DISTINCT_LIMIT", 6)

    # team has seven distinct values, environment two.
    assert break_down_by_tag(_memory_window(), group_by="team", limit=50) is None
    assert break_down_by_tag(_memory_window(), group_by="environment", limit=50) is not None


def test_summary_spends_one_read_on_a_workspace_without_tags() -> None:
    calls: list[str] = []

    def read_keys() -> list[dict[str, Any]]:
        calls.append("keys")
        return []

    def never(*_args: Any) -> Any:
        raise AssertionError("no further read is needed when there are no tags")

    summary = tag_summary(
        read_keys=read_keys,
        read_values=never,
        read_breakdown=never,
        group_by="team",
        tag_key="team",
    )

    assert calls == ["keys"]
    assert summary == {
        "keys": [],
        "keys_truncated": False,
        "keys_approximate": False,
        "group_by": None,
        "breakdown": [],
        "breakdown_too_many_values": False,
        "breakdown_approximate": False,
        "filter_values": [],
        "filter_values_truncated": False,
        "filter_values_approximate": False,
    }


def test_summary_tries_further_default_keys_but_not_an_asked_one() -> None:
    keys = [{"key": name, "requests": count, "overcount": 0}
            for name, count in (("request", 9), ("session", 8), ("team", 7), ("tier", 6))]
    tried: list[str] = []

    def read_breakdown(key: str, _limit: int) -> TagBreakdown | None:
        tried.append(key)
        return TagBreakdown([]) if key in {"team", "tier"} else None

    def summary(group_by: str | None) -> dict[str, Any]:
        tried.clear()
        return tag_summary(
            read_keys=lambda: list(keys),
            read_values=lambda _key: [],
            read_breakdown=read_breakdown,
            group_by=group_by,
            tag_key=None,
        )

    # No key asked for: a request id on every request is the most used key and
    # cannot be broken down, so the next keys are tried, three in all.
    default = summary(None)
    assert tried == ["request", "session", "team"]
    assert (default["group_by"], default["breakdown_too_many_values"]) == ("team", False)
    # A key the viewer picked is answered for that key, refusal included.
    asked = summary("request")
    assert tried == ["request"]
    assert (asked["group_by"], asked["breakdown_too_many_values"]) == ("request", True)
    # A key that is not in the range falls back to the defaults.
    assert summary("cost-center")["group_by"] == "team"


def test_summary_reports_cut_and_approximate_lists() -> None:
    keys = [{"key": f"key-{index:03d}", "requests": 1, "overcount": 0} for index in range(150)]
    values = [{"value": f"v{index:02d}", "requests": 2, "overcount": 1} for index in range(60)]

    summary = tag_summary(
        read_keys=lambda: list(reversed(keys)),
        read_values=lambda _key: list(reversed(values)),
        read_breakdown=lambda _key, _limit: TagBreakdown([], approximate=True),
        group_by="key-140",
        tag_key="key-000",
    )

    assert [row["key"] for row in summary["keys"]] == [f"key-{index:03d}" for index in range(100)]
    assert (summary["keys_truncated"], summary["keys_approximate"]) == (True, False)
    # The asked key is past the 100 listed keys. The list was cut, so the key
    # may well exist and is kept instead of being replaced by a default.
    assert summary["group_by"] == "key-140"
    assert [row["value"] for row in summary["filter_values"]] == [f"v{index:02d}" for index in range(50)]
    assert (summary["filter_values_truncated"], summary["filter_values_approximate"]) == (True, True)
    assert "overcount" not in summary["keys"][0] and "overcount" not in summary["filter_values"][0]
    assert (summary["breakdown_too_many_values"], summary["breakdown_approximate"]) == (False, True)

    # One count that may be too high, even on a key past the listed hundred,
    # makes the key list approximate. An exact value list is not.
    keys[149]["overcount"] = 3
    approximate = tag_summary(
        read_keys=lambda: list(keys),
        read_values=lambda _key: [{"value": "search", "requests": 2, "overcount": 0}],
        read_breakdown=lambda _key, _limit: TagBreakdown([]),
        group_by=None,
        tag_key="key-000",
    )
    assert (approximate["keys_truncated"], approximate["keys_approximate"]) == (True, True)
    assert (approximate["filter_values_truncated"], approximate["filter_values_approximate"]) == (False, False)


# ------------------------------------------- the median, one histogram a group


def _histogram(
    elapsed: list[int | None], opened: set[int]
) -> list[tuple[int | None, int | None, int]]:
    """One group's counts, by the rule the tag histogram's SQL states."""
    counts: dict[tuple[int | None, int | None], int] = {}
    for value in elapsed:
        if value is None:
            cell: tuple[int | None, int | None] = (None, None)
        elif value >= TAG_ELAPSED_CEILING_MS:
            cell = (ELAPSED_LAST_SLOT, None)
        elif value // ELAPSED_SLOT_MS in opened:
            cell = (value // ELAPSED_SLOT_MS, value)
        else:
            cell = (value // ELAPSED_SLOT_MS, None)
        counts[cell] = counts.get(cell, 0) + 1
    return [(slot, value, count) for (slot, value), count in counts.items()]


Group = tuple[str, int, int]


def _histogram_reads(
    snapshots: list[dict[Group, list[int | None]]],
    costs: dict[Group, list[tuple[str, int]]] | None = None,
) -> tuple[Any, list[tuple[list[tuple[int, int]], list[tuple[str, int, int, int]]]]]:
    """A histogram read over fake rows. Each read sees the next snapshot, then the last.

    A group costs three for each generation, all Credits, unless ``costs``
    gives its cost by stored usage type.
    """
    asked: list[tuple[list[tuple[int, int]], list[tuple[str, int, int, int]]]] = []

    def read(top: list[tuple[int, int]], opened: list[tuple[str, int, int, int]]) -> list[dict[str, Any]]:
        groups = snapshots[min(len(asked), len(snapshots) - 1)]
        asked.append((top, opened))
        return [
            {
                "kind": kind, "hash_a": hash_a, "hash_b": hash_b,
                "requests": len(elapsed),
                "cost_by_usage_type": (costs or {}).get(
                    (kind, hash_a, hash_b), [("Credits", 3 * len(elapsed))]
                ),
                "prompt_tokens": 5 * len(elapsed),
                "completion_tokens": len(elapsed),
                "histogram": _histogram(
                    elapsed, {cell[3] for cell in opened if cell[:3] == (kind, hash_a, hash_b)}
                ),
            }
            for (kind, hash_a, hash_b), elapsed in groups.items()
        ]

    return read, asked


def _no_candidates(_count: int) -> dict[str, Any]:
    return {"distinct_values": 0, "by_cost": [], "by_requests": []}


def _never(*_args: Any) -> Any:
    raise AssertionError("this read is not needed")


def _breakdown_of(snapshots: list[dict[Group, list[int | None]]]) -> tuple[Any, Any]:
    """Break down groups that are all ``other`` or ``untagged``: nothing to rank."""
    read, asked = _histogram_reads(snapshots)
    found = clickhouse_breakdown(
        read_candidates=_no_candidates,
        read_candidate_totals=_never,
        read_histogram=read,
        limit=50,
    )
    assert found is not None and found.approximate is False
    return found.rows, [opened for _top, opened in asked]


def _totals_row(digest: int, value: str, usage_type: str, requests: int, cost: int) -> dict[str, Any]:
    # The second hash is the first plus one: enough for a fake.
    return {"hash_a": digest, "hash_b": digest + 1, "usage_type": usage_type, "value": value,
            "requests": requests, "cost_microdollars": cost}


def test_breakdown_ranks_the_candidates_by_credit_cost_then_requests_then_text() -> None:
    asked: list[Any] = []

    def candidates(count: int) -> dict[str, Any]:
        asked.append(("candidates", count))
        return {
            # The estimate is one too high. With every value listed, the
            # count of the list is used and not the estimate.
            "distinct_values": 6,
            # Most generations first, then most cost: the two lists overlap.
            "by_requests": [(40, 9, 0), (10, 4, 0), (20, 2, 0), (30, 2, 0), (50, 1, 0)],
            "by_cost": [(40, 900, 0), (10, 12, 0), (20, 6, 0), (30, 6, 0), (50, 6, 0)],
        }

    def totals(digests: list[int]) -> list[dict[str, Any]]:
        asked.append(("totals", digests))
        return [
            _totals_row(10, "search", "Credits", 4, 12),
            # Equal in credit cost: more generations first, then the text.
            _totals_row(20, "beta", "Credits", 2, 6),
            _totals_row(30, "alpha", "Credits", 2, 6),
            _totals_row(50, "solo", "Credits", 1, 6),
            # The most cost of all, none of it credits: " byok " and "BYOK"
            # are BYOK by the rule a generation is billed under.
            _totals_row(40, "own-keys", "BYOK", 5, 500),
            _totals_row(40, "own-keys", " byok ", 4, 400),
        ]

    groups: dict[Group, list[int | None]] = {
        ("value", 10, 11): [400, 200, 600, None],
        ("value", 30, 31): [150, 300],
        ("value", 20, 21): [100, 500],
        ("other", 0, 0): [700] * 10,
    }
    read, reads = _histogram_reads([groups])

    found = clickhouse_breakdown(
        read_candidates=candidates, read_candidate_totals=totals, read_histogram=read, limit=3,
    )

    # One candidates read for as many as the module allows, then one totals
    # read for exactly those candidates, each named once.
    assert asked == [
        ("candidates", storage_activity_tags.TAG_RANK_CANDIDATES),
        ("totals", [40, 10, 20, 30, 50]),
    ]
    # search, then alpha before beta on text; solo has fewer generations and
    # own-keys has no credit cost at all.
    assert [top for top, _opened in reads] == [[(10, 11), (30, 31), (20, 21)]] * 2
    assert found == TagBreakdown(
        [
            _row("value", "search", 1, 4, 12, 0, 20, 4, 400),
            _row("value", "alpha", 1, 2, 6, 0, 10, 2, 225),
            _row("value", "beta", 1, 2, 6, 0, 10, 2, 300),
            # Five values, three with a row: two folded, counted exactly.
            _row("other", None, 2, 10, 30, 0, 50, 10, 700),
        ],
        approximate=False,
    )


def test_a_wide_key_is_ranked_from_what_the_sketches_found_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_activity_tags, "TAG_RANK_CANDIDATES", 2)
    groups: dict[Group, list[int | None]] = {("value", 10, 11): [100], ("other", 0, 0): [100] * 7}
    listed = {"by_requests": [(10, 4, 0), (20, 2, 0)], "by_cost": [(10, 12, 0), (30, 6, 0)]}

    asked: list[list[int]] = []

    def breakdown(facts: dict[str, Any]) -> TagBreakdown | None:
        read, _reads = _histogram_reads([groups])

        def totals(digests: list[int]) -> list[dict[str, Any]]:
            asked.append(digests)
            return [_totals_row(digest, f"value-{digest}", "Credits", 1, 100 - digest) for digest in digests]

        return clickhouse_breakdown(
            read_candidates=lambda _count: facts, read_candidate_totals=totals, read_histogram=read, limit=1,
        )

    # A requests list as long as it was allowed to be may have left values
    # out: the ranking is approximate, and the folded values are estimated.
    full = breakdown({"distinct_values": 40, **listed})
    assert full is not None and full.approximate is True
    # The candidates are the heaviest by generations and the heaviest by
    # cost, each once: a value heavy in cost alone is still ranked.
    assert asked == [[10, 20, 30]]
    assert [(row["kind"], row["value"], row["values"]) for row in full.rows] == [
        ("value", "value-10", 1), ("other", None, 39),
    ]
    # So may a list whose counts the sketch could not vouch for.
    doubtful = breakdown({"distinct_values": 1, "by_requests": [(10, 4, 3)], "by_cost": []})
    assert doubtful is not None and doubtful.approximate is True
    # A shorter list with exact counts is every value.
    exact = breakdown({"distinct_values": 1, "by_requests": [(10, 4, 0)], "by_cost": [(10, 12, 0)]})
    assert exact is not None and exact.approximate is False


def test_values_that_arrive_after_the_candidates_are_folded_and_never_ranked() -> None:
    """The reviewer's interleaving: one value when the candidates are read,
    a million more before the next statement."""
    asked: list[list[int]] = []

    def totals(digests: list[int]) -> list[dict[str, Any]]:
        asked.append(digests)
        return [_totals_row(10, "search", "Credits", 1, 100)]

    arrived = 1_000_000
    read, reads = _histogram_reads(
        [{("value", 10, 11): [200], ("other", 0, 0): [300] * 3}],
        costs={("other", 0, 0): [("Credits", arrived)]},
    )

    found = clickhouse_breakdown(
        read_candidates=lambda _count: {
            "distinct_values": 1, "by_requests": [(10, 1, 0)], "by_cost": [(10, 100, 0)],
        },
        read_candidate_totals=totals,
        read_histogram=read,
        limit=50,
    )

    # The totals read was asked about the one candidate and the histogram
    # about the one chosen value: neither grows with what arrived since.
    assert asked == [[10]]
    assert [top for top, _opened in reads] == [[(10, 11)]] * 2
    assert found is not None
    assert found.rows == [
        _row("value", "search", 1, 1, 3, 0, 5, 1, 200),
        # Everything that arrived is in one row.
        _row("other", None, 1, 3, arrived, 0, 15, 3, 300),
    ]


def test_a_byok_generation_that_arrives_between_reads_is_counted_as_byok() -> None:
    """The reviewer's interleaving: the candidates and their totals see one
    Credits generation costing 100; the tenant's first BYOK generation,
    costing 900 with the same tag, arrives before the histogram is read."""
    read, _reads = _histogram_reads(
        [{("value", 10, 11): [200, 400]}],
        costs={("value", 10, 11): [("BYOK", 900), ("Credits", 100)]},
    )

    found = clickhouse_breakdown(
        read_candidates=lambda _count: {
            "distinct_values": 1, "by_requests": [(10, 1, 0)], "by_cost": [(10, 100, 0)],
        },
        read_candidate_totals=lambda _digests: [_totals_row(10, "search", "Credits", 1, 100)],
        read_histogram=read,
        limit=50,
    )

    # What a fresh read gives: 100 of credits and 900 of BYOK, not 1,000 of credits.
    assert found is not None
    assert found.rows == [_row("value", "search", 1, 2, 100, 900, 10, 2, 300)]


def test_a_candidate_gone_before_the_totals_or_the_histogram_keeps_no_row() -> None:
    read, reads = _histogram_reads([{("value", 20, 21): [100], ("untagged", 0, 0): [100]}])

    found = clickhouse_breakdown(
        read_candidates=lambda _count: {
            "distinct_values": 3, "by_requests": [(10, 2, 0), (20, 1, 0), (30, 1, 0)], "by_cost": [],
        },
        # The first candidate's rows were replaced away before this read, and
        # the third's before the histogram.
        read_candidate_totals=lambda _digests: [
            _totals_row(20, "ads", "Credits", 1, 5), _totals_row(30, "billing", "Credits", 1, 9),
        ],
        read_histogram=read,
        limit=50,
    )

    assert [top for top, _opened in reads] == [[(30, 31), (20, 21)]] * 2
    assert found is not None
    assert [(row["kind"], row["value"]) for row in found.rows] == [("value", "ads"), ("untagged", None)]


def test_breakdown_opens_each_groups_own_middle_slots_and_stops_when_all_are_known() -> None:
    groups: dict[Group, list[int | None]] = {
        ("value", 11, 12): [400, 200, 600, None],
        ("value", 21, 22): [150, 300],
        ("other", 0, 0): [700, 800, 900, 1100, 1200, 4_800_000],
        ("untagged", 0, 0): [300, 90_000],
    }
    read, reads = _histogram_reads([groups])

    found = clickhouse_breakdown(
        read_candidates=lambda _count: {
            "distinct_values": 3,
            "by_requests": [(11, 4, 0), (21, 2, 0), (31, 6, 0)],
            "by_cost": [(11, 12, 0), (21, 6, 0), (31, 1, 0)],
        },
        read_candidate_totals=lambda _digests: [
            {"hash_a": 11, "hash_b": 12, "usage_type": "Credits", "value": "search",
             "requests": 4, "cost_microdollars": 12},
            {"hash_a": 21, "hash_b": 22, "usage_type": "Credits", "value": "ads",
             "requests": 2, "cost_microdollars": 6},
            {"hash_a": 31, "hash_b": 32, "usage_type": "Credits", "value": "zzz",
             "requests": 6, "cost_microdollars": 1},
        ],
        read_histogram=read,
        limit=2,
    )

    assert found is not None
    assert found.rows == [
        _row("value", "search", 1, 4, 12, 0, 20, 4, 400),
        _row("value", "ads", 1, 2, 6, 0, 10, 2, 225),
        # Three values, two with a row: one folded. Six elapsed times: the
        # middle two are 900 and 1100, in two slots.
        _row("other", None, 1, 6, 18, 0, 30, 6, 1000),
        _row("untagged", None, 0, 2, 6, 0, 10, 2, 45_150),
    ]
    # Two reads: one that opens nothing, one that opens each group's own
    # middle slots and no other group's.
    assert [opened for _top, opened in reads] == [
        [],
        [("other", 0, 0, 0), ("other", 0, 0, 1), ("untagged", 0, 0, 0), ("untagged", 0, 0, 87),
         ("value", 11, 12, 0), ("value", 21, 22, 0)],
    ]


@pytest.mark.parametrize(
    ("elapsed", "reads"),
    [
        ([None, None], 1),  # nothing timed: no slot to open
        ([4_800_000], 1),  # 80 minutes: in the open-ended last slot
        ([10, 4_800_000], 1),
        ([10, 20, 4_800_000], 2),
        ([1023, 1024], 2),  # either side of a slot's edge
        ([0, 0, 0, 0], 2),
        ([TAG_ELAPSED_CEILING_MS - 1, TAG_ELAPSED_CEILING_MS - 1], 2),
        ([TAG_ELAPSED_CEILING_MS - 1, TAG_ELAPSED_CEILING_MS], 1),  # either side of the ceiling
    ],
)
def test_a_groups_median_is_the_one_its_sorted_latencies_give(
    elapsed: list[int | None], reads: int
) -> None:
    rows, asked = _breakdown_of([{("untagged", 0, 0): elapsed}])

    assert [row["median_elapsed_ms"] for row in rows] == [
        elapsed_median([value for value in elapsed if value is not None])
    ]
    assert len(asked) == reads


def test_the_breakdown_gives_every_group_its_exact_median_for_any_spread() -> None:
    generator = random.Random(20261121)  # noqa: S311 - test data, not a secret
    for _ in range(150):
        groups: dict[Group, list[int | None]] = {}
        for group in [("other", 0, 0), ("untagged", 0, 0)][: generator.randint(1, 2)]:
            spread = generator.choice([200, 20_000, 3_000_000, 9_000_000])
            groups[group] = [
                None if generator.random() < 0.1 else generator.randint(0, spread)
                for _ in range(generator.randint(1, 60))
            ]
        rows, asked = _breakdown_of([groups])
        assert {row["kind"]: row["median_elapsed_ms"] for row in rows} == {
            kind: elapsed_median([value for value in elapsed if value is not None])
            for (kind, _a, _b), elapsed in groups.items()
        }
        assert {row["kind"]: row["requests"] for row in rows} == {
            kind: len(elapsed) for (kind, _a, _b), elapsed in groups.items()
        }
        assert len(asked) <= 2


def test_a_group_whose_middle_moved_is_read_again_and_the_others_stay_settled() -> None:
    steady: list[int | None] = [100, 300]
    before: dict[Group, list[int | None]] = {("untagged", 0, 0): [500, 700, 900], ("other", 0, 0): steady}
    # Rows arriving between the first and the second read move the untagged
    # middle from slot 0 to slot 4.
    after: dict[Group, list[int | None]] = {
        ("untagged", 0, 0): [500, 700, 900, 4200, 4300, 4400, 4500], ("other", 0, 0): steady,
    }
    rows, asked = _breakdown_of([before, after])

    # The third read opens the moved group's new slot and keeps the settled
    # group's slot open: totals and medians all come from that one read.
    assert asked == [
        [],
        [("other", 0, 0, 0), ("untagged", 0, 0, 0)],
        [("other", 0, 0, 0), ("untagged", 0, 0, 4)],
    ]
    assert rows == [
        _row("other", None, 1, 2, 6, 0, 10, 2, 200),
        _row("untagged", None, 0, 7, 21, 0, 35, 7, 4200),
    ]


def test_too_many_values_is_decided_by_the_candidates_read_itself() -> None:
    limit = storage_activity_tags.TAG_DISTINCT_LIMIT

    def breakdown(estimate: int, **reads: Any) -> TagBreakdown | None:
        return clickhouse_breakdown(
            read_candidates=lambda _count: {
                "distinct_values": estimate, "by_requests": [(10, 1, 0)], "by_cost": [],
            },
            read_candidate_totals=reads.get("totals", _never),
            read_histogram=reads.get("histogram", _never),
            limit=50,
        )

    # Past the limit nothing else is read: the statement that counted the
    # values is the one that refuses.
    assert breakdown(limit + 1) is None
    # At the limit the breakdown goes ahead.
    read, _reads = _histogram_reads([{("value", 10, 11): [100]}])
    at_limit = breakdown(
        limit, totals=lambda _digests: [_totals_row(10, "search", "Credits", 1, 5)], histogram=read,
    )
    assert at_limit is not None and [row["value"] for row in at_limit.rows] == ["search"]


def test_breakdown_gives_up_on_a_median_that_keeps_moving() -> None:
    # A group whose middle is in an unopened slot on every read, as when rows
    # keep arriving between reads, next to a group that settles at once.
    snapshots: list[dict[Group, list[int | None]]] = [
        {("untagged", 0, 0): [2000 * read + 10] * 5, ("other", 0, 0): [100, 300]}
        for read in range(TAG_HISTOGRAM_READS + 2)
    ]
    rows, asked = _breakdown_of(snapshots)

    # Bounded: the reads stop, the totals stand, and only the restless
    # group's median is absent.
    assert len(asked) == TAG_HISTOGRAM_READS == 4
    assert rows == [
        _row("other", None, 1, 2, 6, 0, 10, 2, 200),
        _row("untagged", None, 0, 5, 15, 0, 25, 5, None),
    ]


def test_the_breakdown_settles_with_the_helper_the_page_median_uses() -> None:
    # One definition of the median on the page: the reliability panel's and
    # the tag breakdown's are the same function over the same histogram.
    assert storage_activity_tags.settle_elapsed_median is settle_elapsed_median
    sql, _bound = reader_statements()["histogram"]
    from trusted_router.operational_analytics import elapsed_histogram_columns

    # The one difference from the page's histogram: an elapsed time at or past
    # the tag ceiling is counted at the shared ceiling, in the last slot, so a
    # group never holds more than 4,097 slots.
    assert (TAG_ELAPSED_SLOTS, TAG_ELAPSED_CEILING_MS) == (4_096, 4_096 * ELAPSED_SLOT_MS)
    assert TAG_ELAPSED_CEILING_MS < ELAPSED_CEILING_MS == 65_535 * 1024
    assert storage_activity_tags.TAG_ELAPSED_CEILING_MS == TAG_ELAPSED_CEILING_MS
    assert elapsed_histogram_columns(
        "if(elapsed_milliseconds >= 4194304, 67107840, elapsed_milliseconds)",
        "(kind, hash_a, hash_b, intDiv(elapsed_milliseconds, 1024))"
        " IN {opened:Array(Tuple(String, UInt64, UInt64, UInt64))}",
    ) in sql


# ------------------------------------------------------------- query builders


def test_recorded_answers_belong_to_the_sql_the_reader_sends_today() -> None:
    assert len(READ_SHAPES) == 11
    current = {
        hashlib.sha256(sql.encode()).hexdigest() for sql, _bound in reader_statements().values()
    }
    # An answer is recorded under the exact text of its statement. The
    # transcript holds one for every text the reader sends today and for no
    # other, apart from the row read that predates the tag panel, so a
    # changed query has no answer, and fails here, until it is recorded
    # again from ClickHouse.
    recorded = {identity.split(" ", 1)[0] for identity in load_transcript()}
    assert recorded == current | {"generations"}, (
        "a tag query changed: record the transcript again (tests/activity_tags_recorded.py)"
    )


@pytest.mark.parametrize("name", READ_SHAPES)
def test_tag_reads_bind_every_value_and_cover_the_whole_window(name: str) -> None:
    sql, bound = reader_statements()[name]
    sources = sql.count("FROM activity_generations FINAL")

    # Deduplicated rows of one tenant since the window start: no sample.
    assert sources == 1 and sql.count("FROM activity_generations") == 1
    assert sql.count("tenant_id = {tenant_id:String}") == 1
    assert sql.count("created_at >= parseDateTime64BestEffort({start_at:String}, 3)") == 1
    assert bound["tenant_id"] == analytics_surrogate("workspace", WORKSPACE)
    assert bound["start_at"] == START_AT
    # No workspace id, tag key or tag value is ever part of the SQL text.
    for text in (WORKSPACE, "team", "environment", "production", "search"):
        assert text not in sql
    assert read_name(sql) == name.split("-")[0]
    by_key, by_tag = name.endswith("-by-key"), name.endswith("-by-tag")
    if name != "values":
        assert ("tag_key" in bound, "tag_value" in bound) == (by_key or by_tag, by_tag)
        assert sql.count("mapContains(tags, {tag_key:String})") == int(by_key or by_tag)
    assert sql.count("tags[{tag_key:String}] = {tag_value:String}") == int(by_tag)
    # No exact quantile, no exact distinct count and no array of the table's
    # rows: none of them is bounded.
    for unbounded in ("quantile", "uniqExact", "median("):
        assert unbounded not in sql
    if name.startswith("histogram"):
        # The one array packs the histogram's own rows, a bounded number a
        # group, after they are grouped: it never collects generations.
        assert sql.count("groupArray(") == 1
        assert sql.index("groupArray(") < sql.index("FROM\n(\n") < sql.index("FROM activity_generations FINAL")
    else:
        assert "groupArray(" not in sql
    # No statement is told which usage types are BYOK: each groups by the
    # stored usage type and the caller decides.
    assert "byok" not in sql.lower() and "byok" not in bound
    if name in {"keys", "values"}:
        assert "topK(" in sql and "'counts'" in sql and "GROUP BY" not in sql
    if name.startswith("candidates"):
        # Fixed-size sketches over a hash of the value and no grouping at
        # all: nothing here grows with the number of distinct values.
        assert "GROUP BY" not in sql and "ORDER BY" not in sql
        assert "uniqCombined(cityHash64(tags[{group_by:String}])) AS distinct_values" in sql
        assert "topKWeighted({candidates:UInt32}, 3, 'counts')(cityHash64(tags[{group_by:String}])," in sql
        assert "topK({candidates:UInt32}, 3, 'counts')(cityHash64(tags[{group_by:String}])) AS by_requests" in sql
        assert bound["candidates"] == "2000"
    if name.startswith("totals"):
        # Only the named candidates are grouped, by hashes and usage type.
        assert "AND cityHash64(tags[{group_by:String}]) IN {candidates:Array(UInt64)}\n" in sql
        assert "GROUP BY hash_a, hash_b, usage_type\n" in sql
        assert bound["candidates"] == "[1,3]"
    if name.startswith("histogram"):
        assert "sumMap([toString(usage_type)], [total_cost_microdollars]) AS cost_at_value" in sql
        # The helper's own grouping, with the tag group in front, then one
        # row a group.
        assert "  GROUP BY kind, hash_a, hash_b, slot, elapsed_value\n)\nGROUP BY kind, hash_a, hash_b\n" in sql
        assert bound["top"] == "[(1,2),(3,4)]"
        assert bound["opened"] == "[('value',1,2,3),('other',0,0,5)]"


def test_array_parameters_are_built_from_integers_and_fixed_kinds_only() -> None:
    assert _integer_array([]) == "[]"
    assert _integer_array([18446744073709551615, 0]) == "[18446744073709551615,0]"
    assert _tuple_array([(1, 2)]) == "[(1,2)]"
    assert _cell_array([("untagged", 0, 0, 65535)]) == "[('untagged',0,0,65535)]"
    # Nothing but the three group kinds can be written into a parameter.
    with pytest.raises(ValueError, match="unknown tag group kind"):
        _cell_array([("value') OR 1 --", 0, 0, 0)])
    with pytest.raises(ValueError):
        _integer_array(cast(Any, ["1) OR (1"]))


def test_reader_rows_are_typed_whatever_the_profile_quotes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sql, _bound = statement(request)
        answers: dict[str, list[dict[str, Any]]] = {
            # A reader profile that quotes 64-bit integers returns strings.
            "keys": [{"tag_key": "team", "requests": "9", "overcount": "0"}],
            "values": [{"tag_value": "", "requests": "2", "overcount": "1"}],
            "candidates": [{"distinct_values": "7",
                            "by_cost": [["18446744073709551615", "12", "0"]],
                            "by_requests": [["18446744073709551615", "4", "0"], [11, 2, 1]]}],
            "totals": [{"hash_a": "18446744073709551615", "hash_b": 2, "usage_type": "BYOK",
                        "named_value": "search", "requests": "4", "cost_microdollars": "12"}],
            # A packed histogram: -1 stands for NULL, and 64-bit counts are quoted.
            "histogram": [{"kind": "other", "hash_a": "0", "hash_b": "0", "requests": "6",
                           "cost_by_usage_type": [["BYOK", "Credits"], ["4", "7"]],
                           "prompt_tokens": "5", "completion_tokens": "2",
                           "histogram": [[-1, -1, "1"], [3, -1, "2"], [4, 4100, "3"]]}],
        }
        return httpx.Response(200, json={"data": answers[read_name(sql)]})

    client = _client(handler)
    window: dict[str, Any] = {"tenant_id": "t", "start_at": START_AT}

    assert client.activity_tag_keys(**window) == [{"key": "team", "requests": 9, "overcount": 0}]
    assert client.activity_tag_values(tag_key="team", **window) == [
        {"value": "", "requests": 2, "overcount": 1}
    ]
    assert client.activity_tag_candidates(group_by="team", candidates=0, **window) == {
        "distinct_values": 7,
        "by_cost": [(18446744073709551615, 12, 0)],
        "by_requests": [(18446744073709551615, 4, 0), (11, 2, 1)],
    }
    assert client.activity_tag_candidate_totals(group_by="team", candidates=[11], **window) == [
        {"hash_a": 18446744073709551615, "hash_b": 2, "usage_type": "BYOK", "value": "search",
         "requests": 4, "cost_microdollars": 12}
    ]
    assert client.activity_tag_histogram(group_by="team", top=[], opened=[], **window) == [{
        "kind": "other", "hash_a": 0, "hash_b": 0, "requests": 6,
        "cost_by_usage_type": [("BYOK", 4), ("Credits", 7)],
        "prompt_tokens": 5, "completion_tokens": 2,
        "histogram": [(None, None, 1), (3, None, 2), (4, 4100, 3)],
    }]


def test_reader_refuses_answers_it_does_not_know() -> None:
    def unknown_kind(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"kind": "sample", "hash_a": 0, "hash_b": 0}]})

    with pytest.raises(RuntimeError, match="unknown row kind"):
        _client(unknown_kind).activity_tag_histogram(
            tenant_id="t", group_by="team", start_at=START_AT, top=[], opened=[],
        )

    def lopsided(_request: httpx.Request) -> httpx.Response:
        row = {"kind": "other", "hash_a": 0, "hash_b": 0, "requests": 1, "prompt_tokens": 1,
               "completion_tokens": 1, "histogram": [], "cost_by_usage_type": [["BYOK", "Credits"], [4]]}
        return httpx.Response(200, json={"data": [row]})

    # A cost without its usage type could not be told apart: an error, not a guess.
    with pytest.raises(ValueError):
        _client(lopsided).activity_tag_histogram(
            tenant_id="t", group_by="team", start_at=START_AT, top=[], opened=[],
        )


# ---------------------------------------------------- the fake analytics client


class _FakeAnalytics:
    """Stands in for the ClickHouse reader, like the other console tests' fakes."""

    def __init__(self, *, distinct_values: int = 3) -> None:
        self.distinct_values = distinct_values
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def activity_tag_keys(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("keys", kwargs))
        return [{"key": "team", "requests": 3, "overcount": 0}, {"key": "tier", "requests": 1, "overcount": 0}]

    def activity_tag_values(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("values", kwargs))
        return [{"value": "search", "requests": 2, "overcount": 0}]

    def activity_tag_candidates(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("candidates", kwargs))
        return {"distinct_values": self.distinct_values, "by_cost": [(11, 9, 0)], "by_requests": [(11, 3, 0)]}

    def activity_tag_candidate_totals(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("totals", kwargs))
        return [{"hash_a": 11, "hash_b": 12, "usage_type": "Credits", "value": "search",
                 "requests": 3, "cost_microdollars": 9}]

    def activity_tag_histogram(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("histogram", kwargs))
        return [{
            "kind": "value", "hash_a": 11, "hash_b": 12, "requests": 3,
            "cost_by_usage_type": [("Credits", 9)], "prompt_tokens": 30, "completion_tokens": 15,
            "histogram": [(None, None, 3)],
        }]

    def activity_generations(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("generations", kwargs))
        return fixture_generations()[: kwargs["limit"]]


def test_store_reads_one_tenant_and_passes_the_filter_to_every_breakdown_read() -> None:
    fake = _FakeAnalytics()
    tenant_id = analytics_surrogate("workspace", WORKSPACE)
    window = {"tenant_id": tenant_id, "start_at": START_AT}
    tag = {"tag_key": "tier", "tag_value": "gold"}

    summary = sql_store(cast(Any, fake)).activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="team", **tag
    )

    assert fake.calls == [
        ("keys", window),
        ("values", {"tag_key": "tier", **window}),
        ("candidates", {"group_by": "team", "candidates": 2000, **window, **tag}),
        ("totals", {"group_by": "team", "candidates": [11], **window, **tag}),
        ("histogram", {"group_by": "team", "top": [(11, 12)], "opened": [], **window, **tag}),
    ]
    assert summary["breakdown"] == [_row("value", "search", 1, 3, 9, 0, 30, 15, None)]
    for _name, kwargs in fake.calls:
        assert WORKSPACE not in kwargs.values()


def test_store_asks_nothing_more_about_a_key_with_too_many_values() -> None:
    fake = _FakeAnalytics(distinct_values=storage_activity_tags.TAG_DISTINCT_LIMIT + 1)

    summary = sql_store(cast(Any, fake)).activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")

    assert [name for name, _kwargs in fake.calls] == ["keys", "candidates"]
    assert summary["breakdown"] == [] and summary["breakdown_too_many_values"] is True


def test_store_tagged_requests_are_bounded_in_time_and_number() -> None:
    fake = _FakeAnalytics()
    tenant_id = analytics_surrogate("workspace", WORKSPACE)

    found = sql_store(cast(Any, fake)).activity_tag_events(
        WORKSPACE, start_at=START_AT, tag_key="team", tag_value="search", limit=5000
    )

    # Never more than 50 rows, whatever the caller asks for, and always from a
    # window start: the unbounded list read this replaces scanned all history.
    assert fake.calls == [
        (
            "generations",
            {
                "tenant_id": tenant_id, "tag_key": "team", "tag_value": "search",
                "start_at": START_AT, "limit": 51,
            },
        )
    ]
    assert found.scan_limit == 50


def test_store_without_a_reader_says_so() -> None:
    store = sql_store(cast(Any, None))

    with pytest.raises(RuntimeError, match="not configured"):
        store.activity_tag_summary(WORKSPACE, start_at=START_AT)
    with pytest.raises(RuntimeError, match="not configured"):
        store.activity_tag_events(WORKSPACE, start_at=START_AT, tag_key="team")


# ------------------------------------------------- in-memory vs ClickHouse parity


@pytest.mark.parametrize(
    ("workspace", "kwargs", "overrides"),
    [case[1:] for case in SUMMARY_CASES],
    ids=[case[0] for case in SUMMARY_CASES],
)
def test_memory_and_clickhouse_agree_on_the_tag_summary(
    workspace: str, kwargs: dict[str, Any], overrides: dict[str, int]
) -> None:
    requests: list[httpx.Request] = []

    with limits(overrides):
        from_sql = sql_store(recorded_client(requests)).activity_tag_summary(
            workspace, start_at=START_AT, **kwargs
        )
        from_memory = _memory_store().activity_tag_summary(workspace, start_at=START_AT, **kwargs)

    assert from_memory == from_sql
    assert requests
    for request in requests:
        _sql, bound = statement(request)
        assert bound["tenant_id"] == analytics_surrogate("workspace", workspace)


def test_the_agreed_default_summary_is_the_expected_one() -> None:
    requests: list[httpx.Request] = []
    summary = sql_store(recorded_client(requests)).activity_tag_summary(WORKSPACE, start_at=START_AT)

    assert summary == {
        "keys": [{"key": "team", "requests": 12}, {"key": "environment", "requests": 5}],
        "keys_truncated": False,
        "keys_approximate": False,
        "group_by": "team",
        "breakdown": [
            _row("value", "search", 1, 4, 1350, 0, 190, 85, 400),
            _row("value", "billing", 1, 1, 800, 0, 80, 40, None),
            # The delivery of g02 that was replaced is not counted: FINAL in
            # ClickHouse, the last write in memory. Counted, it would make
            # three "ads" requests costing 811.
            _row("value", "ads", 1, 2, 700, 500, 130, 55, 225),
            _row("value", "", 1, 1, 20, 0, 5, 5, 1000),
            _row("value", "zeta", 1, 2, 10, 0, 4, 3, 850),
            _row("value", "Search", 1, 1, 10, 0, 2, 1, 700),
            # One millisecond below the tag histogram's ceiling; "billing",
            # exactly at it, has no median.
            _row("value", "delta", 1, 1, 10, 0, 4, 2, 4_194_303),
            _row("untagged", None, 0, 2, 100, 0, 17, 7, 45_150),
        ],
        "breakdown_too_many_values": False,
        "breakdown_approximate": False,
        "filter_values": [],
        "filter_values_truncated": False,
        "filter_values_approximate": False,
    }
    # Keys, the candidates, their totals, and two histogram reads: the second
    # opens each group's middle slots.
    assert [read_name(statement(request)[0]) for request in requests] == [
        "keys", "candidates", "totals", "histogram", "histogram",
    ]


def test_the_agreed_cut_and_filtered_summaries_are_the_expected_ones() -> None:
    store = _memory_store()

    with limits({"TAG_VALUE_LIMIT": 2}):
        by_cost = store.activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")
    assert by_cost["breakdown"] == [
        _row("value", "search", 1, 4, 1350, 0, 190, 85, 400),
        _row("value", "billing", 1, 1, 800, 0, 80, 40, None),
        _row("other", None, 5, 7, 750, 500, 145, 66, 800),
        _row("untagged", None, 0, 2, 100, 0, 17, 7, 45_150),
    ]
    with limits({"TAG_VALUE_LIMIT": 5}):
        by_requests = store.activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")
    assert by_requests["breakdown"][-2:] == [
        _row("other", None, 2, 2, 20, 0, 6, 3, 2_097_501),
        _row("untagged", None, 0, 2, 100, 0, 17, 7, 45_150),
    ]

    filtered = store.activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="environment", tag_key="team", tag_value="search"
    )
    assert filtered["breakdown"] == [
        _row("value", "production", 1, 2, 1200, 0, 140, 70, 300),
        _row("value", "staging", 1, 1, 100, 0, 30, 10, 600),
        _row("untagged", None, 0, 1, 50, 0, 20, 5, None),
    ]
    # The value list describes the whole window, not the filtered rows.
    assert filtered["filter_values"] == [
        {"value": "search", "requests": 4},
        {"value": "ads", "requests": 2},
        {"value": "zeta", "requests": 2},
        {"value": "", "requests": 1},
        {"value": "Search", "requests": 1},
        {"value": "billing", "requests": 1},
        {"value": "delta", "requests": 1},
    ]

    with limits({"TAG_DISTINCT_LIMIT": 6}):
        refused = store.activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")
        default = store.activity_tag_summary(WORKSPACE, start_at=START_AT)
    assert (refused["group_by"], refused["breakdown"], refused["breakdown_too_many_values"]) == (
        "team", [], True,
    )
    assert (default["group_by"], default["breakdown_too_many_values"]) == ("environment", False)
    assert store.activity_tag_summary(OTHER_WORKSPACE, start_at=START_AT)["group_by"] == "environment"
    assert store.activity_tag_summary(EMPTY_WORKSPACE, start_at=START_AT)["keys"] == []


def test_a_tie_at_the_cut_is_decided_by_text_in_both_stores() -> None:
    # Six rows of seven values: "Search" and "delta" are equal in cost and in
    # requests, and the last row goes to the one that sorts first.
    with limits({"TAG_VALUE_LIMIT": 6}):
        from_sql = sql_store(recorded_client([])).activity_tag_summary(
            WORKSPACE, start_at=START_AT, group_by="team"
        )
        from_memory = _memory_store().activity_tag_summary(WORKSPACE, start_at=START_AT, group_by="team")

    kept = [row["value"] for row in from_sql["breakdown"] if row["kind"] == "value"]
    assert kept == ["search", "billing", "ads", "", "zeta", "Search"]
    assert from_sql["breakdown"][6] == _row("other", None, 1, 1, 10, 0, 4, 2, 4_194_303)
    assert from_sql == from_memory and from_sql["breakdown_approximate"] is False


@pytest.mark.parametrize(
    ("kwargs", "overrides", "kept", "folded"),
    [
        # Three candidates by requests (search, ads, zeta) and three by cost
        # (search, ads, billing): four values keep a row and three are folded.
        (APPROXIMATE_CASES[0][1], APPROXIMATE_CASES[0][2], ["search", "billing", "ads", "zeta"], 3),
        # A list as long as it was allowed to be is not known to be complete,
        # even when, as here, it does hold every value.
        (APPROXIMATE_CASES[1][1], APPROXIMATE_CASES[1][2],
         ["search", "billing", "ads", "", "zeta", "Search", "delta"], 0),
    ],
    ids=[case[0] for case in APPROXIMATE_CASES],
)
def test_a_key_with_more_values_than_candidates_is_ranked_approximately(
    kwargs: dict[str, Any], overrides: dict[str, int], kept: list[str], folded: int
) -> None:
    with limits(overrides):
        from_sql = sql_store(recorded_client([])).activity_tag_summary(
            WORKSPACE, start_at=START_AT, **kwargs
        )
        from_memory = _memory_store().activity_tag_summary(WORKSPACE, start_at=START_AT, **kwargs)

    assert (from_sql["breakdown_approximate"], from_memory["breakdown_approximate"]) == (True, False)
    assert [row["value"] for row in from_sql["breakdown"] if row["kind"] == "value"] == kept
    assert [row["values"] for row in from_sql["breakdown"] if row["kind"] == "other"] == (
        [folded] if folded else []
    )
    # Which values keep a row is approximate. What the rows add up to is not.
    for total in ("requests", "cost_microdollars", "byok_microdollars", "prompt_tokens", "completion_tokens"):
        assert sum(row[total] for row in from_sql["breakdown"]) == sum(
            row[total] for row in from_memory["breakdown"]
        ), total
    # And a row that both stores show is the same row.
    shown = {row["value"]: row for row in from_memory["breakdown"] if row["kind"] == "value"}
    for row in from_sql["breakdown"]:
        if row["kind"] == "value":
            assert row == shown[row["value"]]


@pytest.mark.parametrize(
    ("kwargs", "ids", "truncated"),
    [
        (EVENT_CASES[0][1], ["g04", "g03", "g02", "g01"], False),
        (EVENT_CASES[1][1], ["g10", "g05", "g03", "g02", "g01"], False),
        (EVENT_CASES[2][1], ["g08"], False),
        (EVENT_CASES[3][1], ["g16", "g15", "g14"], True),
        (EVENT_CASES[4][1], [], False),
    ],
    ids=[case[0] for case in EVENT_CASES],
)
def test_memory_and_clickhouse_agree_on_tagged_requests(
    kwargs: dict[str, Any], ids: list[str], truncated: bool
) -> None:
    from_sql = sql_store(recorded_client([])).activity_tag_events(
        WORKSPACE, start_at=START_AT, **kwargs
    )
    from_memory = _memory_store().activity_tag_events(WORKSPACE, start_at=START_AT, **kwargs)

    assert from_memory == from_sql
    assert [event["id"] for event in from_sql.data] == ids
    assert from_sql.truncated is truncated


# -------------------------------------------------------------------- privacy


def test_tag_reads_send_every_bound_value_in_the_body_not_the_url() -> None:
    requests: list[httpx.Request] = []
    store = sql_store(recorded_client(requests))

    store.activity_tag_summary(
        WORKSPACE, start_at=START_AT, group_by="environment", tag_key="team", tag_value="search"
    )
    store.activity_tag_events(WORKSPACE, start_at=START_AT, tag_key="team", tag_value="search")

    assert len(requests) == 7
    for request in requests:
        # The URL names the database and nothing else.
        assert str(request.url) == "http://clickhouse.test:8123?database=tr"
        fields = form_fields(request)
        assert fields["param_tenant_id"] == analytics_surrogate("workspace", WORKSPACE)
        assert "query" in fields and "search" not in fields["query"]
    sent = [form_fields(request) for request in requests]
    assert any(fields.get("param_tag_value") == "search" for fields in sent)
    assert any(fields.get("param_group_by") == "environment" for fields in sent)


def test_a_read_outside_the_private_scope_keeps_its_url_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    client.activity_generations(tenant_id="t", tag_key="team", limit=1)
    with bound_values_stay_private():
        client.activity_generations(tenant_id="t", tag_key="team", limit=1)
    client.activity_generations(tenant_id="t", limit=1)

    outside, inside, after = seen
    assert outside.url.params["param_tag_key"] == "team" and not form_fields(outside)
    assert "param_tag_key" not in inside.url.params
    assert form_fields(inside)["param_tag_key"] == "team"
    # The scope ends with the block: later reads are sent as before.
    assert after.url.params["param_tenant_id"] == "t" and not form_fields(after)


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_no_log_record_carries_tag_text_even_with_http_client_logging_on(
    level: int, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx logs every request's URL at INFO and httpcore traces at DEBUG.

    Production runs both loggers above those levels, but the promise is that
    tag text is never logged, so this turns them on and reads every record.
    """
    store = sql_store(recorded_client([]))

    with caplog.at_level(level):
        store.activity_tag_summary(
            WORKSPACE, start_at=START_AT, group_by="environment", tag_key="team", tag_value="search"
        )
        store.activity_tag_events(WORKSPACE, start_at=START_AT, tag_key="team", tag_value="search")

    logged = [record.getMessage() for record in caplog.records]
    assert sum("HTTP Request: POST http://clickhouse.test:8123" in line for line in logged) == 7
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert "param_" not in text, record.name
        for tag_text in ("team", "search", "environment", "production"):
            assert tag_text not in text, record.name


def test_the_plain_read_does_log_them_which_is_why_the_scope_exists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with caplog.at_level(logging.INFO):
        _client(handler).activity_generations(
            tenant_id="t", tag_key="team", tag_value="zq-private-value", limit=1
        )

    assert any("param_tag_value=zq-private-value" in record.getMessage() for record in caplog.records)


class _CapturedEnvelopes(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            self.payloads.append(str(item.payload.json))


def test_tag_reads_leave_no_bound_value_in_sentry() -> None:
    """Sentry's httpx integration records an outgoing request's query string.

    This uses the real SDK: a transaction with tag reads in it, then an error
    event, and nothing sent carries the tag. The plain row reader inside
    another transaction is the positive control that the capture would have
    seen it.
    """
    client = recorded_client([])
    store = sql_store(client)
    transport = _CapturedEnvelopes()
    sentry = sentry_sdk.Client(
        dsn="https://public@sentry.invalid/1",
        transport=transport,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[HttpxIntegration()],
        traces_sample_rate=1.0,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry)
        with sentry_sdk.start_transaction(name="tag-reads"):
            store.activity_tag_summary(
                WORKSPACE, start_at=START_AT, group_by="environment", tag_key="team",
                tag_value="search",
            )
            store.activity_tag_events(WORKSPACE, start_at=START_AT, tag_key="team", tag_value="search")
            sentry_sdk.capture_message("after the tag reads")
        private = "\n".join(transport.payloads)
        assert "after the tag reads" in private and "tag-reads" in private
        assert "param_" not in private and "search" not in private

        transport.payloads.clear()

        def empty(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        with sentry_sdk.start_transaction(name="control"):
            _client(empty).activity_generations(
                tenant_id="t", tag_key="team", tag_value="search", start_at=START_AT, limit=51
            )
        assert "param_tag_value=search" in "\n".join(transport.payloads)
    sentry.close()


@pytest.mark.parametrize("status", [500, 503])
def test_a_clickhouse_error_does_not_name_the_bound_values(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert form_fields(request)["param_tag_value"] == "zq-private-value"
        return httpx.Response(status, text="Code: 241. DB::Exception: Memory limit exceeded")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        client.activity_tag_candidates(
            tenant_id="t", group_by="zq-private-group", start_at=START_AT, candidates=2000,
            tag_key="zq-private-key", tag_value="zq-private-value",
        )

    error = raised.value
    assert "zq-private" not in str(error) and "param_" not in str(error)
    # The second line of defence, which holds whatever the URL carries: the
    # error names the status and no URL at all, and does not chain the one
    # httpx raised.
    assert str(error) == f"ClickHouse answered HTTP {status} to a read with private bound values"
    assert "clickhouse.test" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert error.response.status_code == status
    assert is_analytics_unavailable(error) is (status == 503)
    with caplog.at_level(logging.DEBUG):
        log_analytics_unavailable(logging.getLogger("tests.activity_tags"), "tag_test", error)
    assert caplog.records
    for record in caplog.records:
        assert "zq-private" not in record.getMessage()
        assert "zq-private" not in str(record.__dict__)
