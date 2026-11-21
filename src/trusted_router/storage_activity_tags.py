"""Request-tag reads for the console Observability page.

Three questions about one workspace's generations since a window start:
which tag keys appear, which values one key takes, and what each value of a
key cost. The in-memory store answers them from its rows with the functions
here. The durable stores answer them in ClickHouse
(``OperationalAnalyticsClient.activity_tag_*``) over every row in the window
and share only the assembly below, so both paths return one shape.
``tests/test_activity_tags.py`` holds the two to the same answer on the same
rows.

Tag keys and values are customer text. Nothing here logs them.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from trusted_router.storage_models import Generation, _is_byok

# How many distinct keys the page offers, and how many values of one key a
# breakdown or a filter lists before the rest are folded together.
TAG_KEY_LIMIT = 100
TAG_VALUE_LIMIT = 50
TAG_EVENT_LIMIT = 50

TagRows = list[dict[str, Any]]


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


def count_tag_keys(generations: Iterable[Generation], *, limit: int) -> TagRows:
    """Keys by the number of generations carrying them, most used first."""
    counts: Counter[str] = Counter()
    for gen in generations:
        counts.update(gen.tags.keys())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"key": key, "requests": requests} for key, requests in ranked[:limit]]


def count_tag_values(
    generations: Iterable[Generation], *, tag_key: str, limit: int
) -> TagRows:
    """One key's values by the number of generations carrying them."""
    counts = Counter(gen.tags[tag_key] for gen in generations if tag_key in gen.tags)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"value": value, "requests": requests} for value, requests in ranked[:limit]]


def break_down_by_tag(
    generations: Iterable[Generation], *, group_by: str, limit: int
) -> TagRows:
    """Totals per value of ``group_by``, unordered.

    The ``limit`` most expensive values each keep a row. Every other value is
    summed into one ``other`` row, and generations without the key into one
    ``untagged`` row. Ranking is by credit cost, then requests, then value, the
    same order the ClickHouse query uses.
    """
    rows = list(generations)
    by_value: dict[str, list[Generation]] = {}
    untagged: list[Generation] = []
    for gen in rows:
        if group_by in gen.tags:
            by_value.setdefault(gen.tags[group_by], []).append(gen)
        else:
            untagged.append(gen)
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


def tag_summary(
    *,
    read_keys: Callable[[int], TagRows],
    read_values: Callable[[str, int], TagRows],
    read_breakdown: Callable[[str, int], TagRows],
    group_by: str | None,
    tag_key: str | None,
) -> dict[str, Any]:
    """Assemble the page's tag panel from three reads.

    Each read asks for one row more than it shows so that truncation is
    reported instead of guessed. A workspace with no tags in the window costs
    one read: the breakdown and the value list are skipped.
    """
    keys = read_keys(TAG_KEY_LIMIT + 1)
    keys_truncated = len(keys) > TAG_KEY_LIMIT
    keys = keys[:TAG_KEY_LIMIT]
    known = {str(row["key"]) for row in keys}
    # A key the page asked for is kept when it may simply have fallen outside
    # a truncated key list; otherwise the most used key is the default.
    if group_by is None or (group_by not in known and not keys_truncated):
        group_by = str(keys[0]["key"]) if keys else None
    values: TagRows = []
    values_truncated = False
    if tag_key is not None and keys:
        values = read_values(tag_key, TAG_VALUE_LIMIT + 1)
        values_truncated = len(values) > TAG_VALUE_LIMIT
        values = values[:TAG_VALUE_LIMIT]
    return {
        "keys": keys,
        "keys_truncated": keys_truncated,
        "group_by": group_by,
        "breakdown": (
            order_breakdown(read_breakdown(group_by, TAG_VALUE_LIMIT))
            if group_by is not None
            else []
        ),
        "filter_values": values,
        "filter_values_truncated": values_truncated,
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
        "median_elapsed_ms": lower_median(
            [
                gen.elapsed_milliseconds
                for gen in generations
                if gen.elapsed_milliseconds is not None
            ]
        ),
    }


def lower_median(values: list[int]) -> int | None:
    """The median, or the lower of the two middle values of an even count.

    This is what ClickHouse's ``quantileExactWeighted(0.5)`` returns with unit
    weights. The reader uses that function because its memory grows with the
    number of distinct latencies, not with the number of requests.
    """
    if not values:
        return None
    return sorted(values)[(len(values) - 1) // 2]
