"""Request-tag reads for the console Observability page.

Three questions about one workspace's generations since a window start:
which tag keys appear, which values one key takes, and what each value of a
key cost. The in-memory store answers them from its rows with the functions
here. The durable stores answer them in ClickHouse
(``OperationalAnalyticsClient.activity_tag_*``) over every row in the window
and share the assembly below, so both paths return one shape.
``tests/test_activity_tags.py`` holds the two to the same answer on the same
rows.

Tag keys and values are customer text. Nothing here logs them.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from trusted_router.operational_analytics import TAG_ELAPSED_CEILING_MS, settle_elapsed_median
from trusted_router.storage_models import Generation, _is_byok

# How many distinct keys the page offers, and how many values of one key a
# breakdown or a filter lists before the rest are folded together.
TAG_KEY_LIMIT = 100
TAG_VALUE_LIMIT = 50
TAG_EVENT_LIMIT = 50
# A breakdown ranks every distinct value of its key before it keeps
# TAG_VALUE_LIMIT, and that ranking holds one group per value: two hashes and
# two sums, about 85 bytes measured, whatever the value's length. Above this
# many distinct values the breakdown is refused instead of attempted, so the
# ranking never holds more than about 45 MiB of the reader's 1 GiB. A key
# with more values than this is an identifier, not a dimension to allocate
# cost by.
TAG_DISTINCT_LIMIT = 500_000
# When the page did not ask for a key, how many of the most used keys are
# tried before the breakdown is reported as refused.
TAG_DEFAULT_KEY_TRIES = 3

# The histogram is read once without an opened slot, once with every group's
# middle slots opened, and at most twice more for groups whose middle moved
# to another slot between reads: the same allowance as the page's own median.
TAG_HISTOGRAM_READS = 4

TagRows = list[dict[str, Any]]
TagGroup = tuple[str, int, int]


def parse_window_start(start_at: str) -> dt.datetime:
    """Parse an ISO-8601 window start; a naive value is read as UTC."""
    parsed = dt.datetime.fromisoformat(start_at.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def generations_in_window(
    generations: Iterable[Generation],
    *,
    workspace_id: str,
    start_at: str,
    tag_key: str | None = None,
    tag_value: str | None = None,
) -> list[Generation]:
    """One workspace's generations since ``start_at``, optionally one tag.

    ``tag_key`` alone keeps every generation carrying the key; with
    ``tag_value`` the match is exact and case-sensitive. An empty string is a
    real value, distinct from the key being absent.
    """
    start = parse_window_start(start_at)
    return [
        gen
        for gen in generations
        if gen.workspace_id == workspace_id
        and parse_window_start(gen.created_at) >= start
        and (tag_key is None or tag_key in gen.tags)
        and (tag_value is None or gen.tags.get(tag_key or "") == tag_value)
    ]


def count_tag_keys(generations: Iterable[Generation]) -> TagRows:
    """Keys by the number of generations carrying them, most used first."""
    counts: Counter[str] = Counter()
    for gen in generations:
        counts.update(gen.tags.keys())
    return rank_by_requests(
        [{"key": key, "requests": requests, "overcount": 0} for key, requests in counts.items()],
        "key",
    )


def count_tag_values(generations: Iterable[Generation], *, tag_key: str) -> TagRows:
    """One key's values by the number of generations carrying them."""
    counts = Counter(gen.tags[tag_key] for gen in generations if tag_key in gen.tags)
    return rank_by_requests(
        [
            {"value": value, "requests": requests, "overcount": 0}
            for value, requests in counts.items()
        ],
        "value",
    )


def rank_by_requests(rows: TagRows, name: str) -> TagRows:
    """Most requests first, then by text, so equal counts have one order."""
    return sorted(rows, key=lambda row: (-int(row["requests"]), str(row[name])))


def break_down_by_tag(
    generations: Iterable[Generation], *, group_by: str, limit: int
) -> TagRows | None:
    """Totals per value of ``group_by``, unordered; None for too many values.

    The ``limit`` most expensive values each keep a row. Every other value is
    summed into one ``other`` row, and generations without the key into one
    ``untagged`` row. Ranking is by credit cost, then requests, as in the
    ClickHouse ranking. Values equal in both are ordered by text here and by
    a hash there, so the two can keep different ones of them at the cut.
    """
    rows = list(generations)
    by_value: dict[str, list[Generation]] = {}
    untagged: list[Generation] = []
    for gen in rows:
        if group_by in gen.tags:
            by_value.setdefault(gen.tags[group_by], []).append(gen)
        else:
            untagged.append(gen)
    if len(by_value) > TAG_DISTINCT_LIMIT:
        return None
    totals = {value: _totals(members) for value, members in by_value.items()}
    ranked = sorted(
        totals,
        key=lambda value: (
            -totals[value]["cost_microdollars"],
            -totals[value]["requests"],
            value,
        ),
    )
    result: TagRows = [
        {"kind": "value", "value": value, "values": 1, **totals[value]}
        for value in ranked[:limit]
    ]
    folded = ranked[limit:]
    if folded:
        members = [gen for value in folded for gen in by_value[value]]
        result.append(
            {"kind": "other", "value": None, "values": len(folded), **_totals(members)}
        )
    if untagged:
        result.append(
            {"kind": "untagged", "value": None, "values": 0, **_totals(untagged)}
        )
    return result


def clickhouse_breakdown(
    *,
    read_cardinality: Callable[[], dict[str, Any]],
    read_ranking: Callable[[int, list[int]], TagRows],
    read_names: Callable[[list[tuple[int, int]]], TagRows],
    read_histogram: Callable[
        [list[tuple[int, int]], list[int], list[tuple[str, int, int, int]]], TagRows
    ],
    limit: int,
) -> TagRows | None:
    """The breakdown from the bounded ClickHouse reads; None for too many values.

    The cardinality read decides whether the ranking may run at all. The
    ranking picks the values that keep a row, by their hashes, and the names
    read fetches the text of those values only.

    Medians are the page's own (``settle_elapsed_median``), settled for each
    group apart: the first histogram read opens no slot and tells each group
    which one or two slots hold its middle values, and the next read opens
    those slots for that group. Every read returns every group with its
    totals, so the totals and the medians both come from the last read alone
    and describe one snapshot. A group whose middle moved to another slot between reads, which
    takes rows arriving in between, is read again with the new slots; one
    that has still not settled after TAG_HISTOGRAM_READS reads is reported
    without a median rather than failing the whole breakdown.
    """
    facts = read_cardinality()
    if int(facts["distinct_values"]) > TAG_DISTINCT_LIMIT:
        return None
    # The same decision generation_metrics makes for one generation, made
    # here for each stored usage type and handed back as hashes.
    byok = sorted(int(digest) for text, digest in facts["usage_types"] if _is_byok(str(text)))
    ranked = read_ranking(limit, byok)
    top = [(int(row["hash_a"]), int(row["hash_b"])) for row in ranked]
    names: dict[tuple[int, int], str] = {}
    for row in read_names(top) if top else []:
        pair = (int(row["hash_a"]), int(row["hash_b"]))
        # Two values with both hashes equal would share a row; keep one name.
        names[pair] = min(names.get(pair, str(row["value"])), str(row["value"]))
    # The slots opened for each group. A settled group keeps its slots open:
    # the next read has to settle it again.
    opened: dict[TagGroup, set[int]] = {}
    groups: dict[TagGroup, dict[str, Any]] = {}
    medians: dict[TagGroup, int | None] = {}
    for _ in range(TAG_HISTOGRAM_READS):
        cells = sorted((*group, slot) for group, slots in opened.items() for slot in slots)
        groups = {
            (str(row["kind"]), int(row["hash_a"]), int(row["hash_b"])): row
            for row in read_histogram(top, byok, cells)
        }
        medians = {}
        for group, row in groups.items():
            settled = settle_elapsed_median(
                [
                    {"slot": slot, "elapsed_value": value, "generations_at_value": count}
                    for slot, value, count in row["histogram"]
                ]
            )
            if isinstance(settled, dict):
                medians[group] = settled["median_ms"]
            else:
                opened[group] = set(settled)
        if len(medians) == len(groups):
            break

    def totals(group: TagGroup) -> dict[str, Any]:
        row = groups[group]
        return {
            "requests": int(row["requests"]),
            "cost_microdollars": int(row["cost_microdollars"]),
            "byok_microdollars": int(row["byok_microdollars"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "median_elapsed_ms": medians.get(group),
        }

    result: TagRows = []
    for pair in top:
        group = ("value", *pair)
        # A value read by the ranking and gone by the later reads has no row.
        if group in groups and pair in names:
            result.append({"kind": "value", "value": names[pair], "values": 1, **totals(group)})
    if ("other", 0, 0) in groups:
        distinct = int(ranked[0]["value_count"]) if ranked else 0
        result.append(
            {
                "kind": "other",
                "value": None,
                "values": max(1, distinct - len(ranked)),
                **totals(("other", 0, 0)),
            }
        )
    if ("untagged", 0, 0) in groups:
        result.append(
            {"kind": "untagged", "value": None, "values": 0, **totals(("untagged", 0, 0))}
        )
    return result


def tag_summary(
    *,
    read_keys: Callable[[], TagRows],
    read_values: Callable[[str], TagRows],
    read_breakdown: Callable[[str, int], TagRows | None],
    group_by: str | None,
    tag_key: str | None,
) -> dict[str, Any]:
    """Assemble the page's tag panel from its reads.

    ``read_keys`` and ``read_values`` return rows with ``requests`` and an
    ``overcount``, which is 0 unless the read had more distinct keys or
    values than it tracks exactly. A workspace with no tags in the window
    costs one read: the breakdown and the value list are skipped.
    """
    ranked_keys = rank_by_requests(read_keys(), "key")
    keys = ranked_keys[:TAG_KEY_LIMIT]
    keys_approximate = any(int(row["overcount"]) > 0 for row in ranked_keys)
    keys_truncated = len(ranked_keys) > TAG_KEY_LIMIT
    known = [str(row["key"]) for row in keys]
    # A key the page asked for is kept when it may simply have fallen outside
    # a cut key list. Otherwise the most used keys are tried in turn, so that
    # a request id on every request does not make the default view a refusal.
    if group_by is not None and (group_by in known or keys_truncated):
        candidates = [group_by]
    else:
        candidates = known[:TAG_DEFAULT_KEY_TRIES]
    values: TagRows = []
    values_truncated = False
    values_approximate = False
    if tag_key is not None and keys:
        ranked_values = rank_by_requests(read_values(tag_key), "value")
        values = ranked_values[:TAG_VALUE_LIMIT]
        values_truncated = len(ranked_values) > TAG_VALUE_LIMIT
        values_approximate = any(int(row["overcount"]) > 0 for row in ranked_values)
    chosen: str | None = None
    breakdown: TagRows | None = []
    for candidate in candidates:
        breakdown = read_breakdown(candidate, TAG_VALUE_LIMIT)
        if chosen is None or breakdown is not None:
            chosen = candidate
        if breakdown is not None:
            break
    return {
        "keys": [{"key": row["key"], "requests": row["requests"]} for row in keys],
        "keys_truncated": keys_truncated,
        "keys_approximate": keys_approximate,
        "group_by": chosen,
        "breakdown": order_breakdown(breakdown or []),
        "breakdown_too_many_values": breakdown is None,
        "filter_values": [
            {"value": row["value"], "requests": row["requests"]} for row in values
        ],
        "filter_values_truncated": values_truncated,
        "filter_values_approximate": values_approximate,
    }


def order_breakdown(rows: TagRows) -> TagRows:
    """Values by cost, then the ``other`` row, then the ``untagged`` row."""
    position = {"value": 0, "other": 1, "untagged": 2}
    return sorted(
        rows,
        key=lambda row: (
            position[row["kind"]],
            -int(row["cost_microdollars"]),
            -int(row["requests"]),
            str(row["value"] or ""),
        ),
    )


def _totals(generations: list[Generation]) -> dict[str, Any]:
    credits = [gen for gen in generations if not _is_byok(gen.usage_type)]
    byok = [gen for gen in generations if _is_byok(gen.usage_type)]
    return {
        "requests": len(generations),
        "cost_microdollars": sum(gen.total_cost_microdollars for gen in credits),
        "byok_microdollars": sum(gen.total_cost_microdollars for gen in byok),
        "prompt_tokens": sum(gen.tokens_prompt for gen in generations),
        "completion_tokens": sum(gen.tokens_completion for gen in generations),
        "median_elapsed_ms": elapsed_median(
            [
                gen.elapsed_milliseconds
                for gen in generations
                if gen.elapsed_milliseconds is not None
            ]
        ),
    }


def elapsed_median(values: list[int]) -> int | None:
    """The median, or for an even count the floor of the two middle values' mean.

    Absent when there is no value, and when the upper middle value is at or
    beyond TAG_ELAPSED_CEILING_MS (about 70 minutes): the ClickHouse tag
    histogram counts such a value in its open-ended last slot, for which
    ``settle_elapsed_median`` reports no median.
    """
    if not values:
        return None
    ordered = sorted(values)
    low = ordered[(len(ordered) - 1) // 2]
    high = ordered[len(ordered) // 2]
    if high >= TAG_ELAPSED_CEILING_MS:
        return None
    return (low + high) // 2
