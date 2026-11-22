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

import dataclasses
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
# A key with more distinct values than this is an identifier, not a dimension
# to allocate cost by, and its breakdown is refused. The in-memory store
# counts the values exactly. ClickHouse estimates the count in the statement
# that picks the candidates, within about one percent, so the page words the
# refusal as "about this many or more". No read's memory depends on this
# limit: it is a statement about usefulness, not a guard.
TAG_DISTINCT_LIMIT = 500_000
# How many values each of the two candidate sketches returns. With fewer
# distinct values than this, every value is a candidate. With more, ClickHouse
# ranks the heaviest values its sketches found and the ranking is marked
# approximate.
TAG_RANK_CANDIDATES = 2_000
# When the page did not ask for a key, how many of the most used keys are
# tried before the breakdown is reported as refused.
TAG_DEFAULT_KEY_TRIES = 3

# The histogram is read once without an opened slot, once with every group's
# middle slots opened, and at most twice more for groups whose middle moved
# to another slot between reads: the same allowance as the page's own median.
TAG_HISTOGRAM_READS = 4

TagRows = list[dict[str, Any]]
TagGroup = tuple[str, int, int]


@dataclasses.dataclass(frozen=True)
class TagBreakdown:
    """A breakdown's rows, and whether its reads could have missed a value.

    ``approximate`` is what the reads themselves know: a candidate list that
    may be incomplete. ``tag_summary`` adds what the final numbers show (see
    ``other_outweighs_a_named_row``).
    """

    rows: TagRows
    approximate: bool = False


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
) -> TagBreakdown | None:
    """Totals per value of ``group_by``, unordered; None for too many values.

    The ``limit`` most expensive values each keep a row. Every other value is
    summed into one ``other`` row, and generations without the key into one
    ``untagged`` row. Ranking is by credit cost, then requests, then the
    value's text: the order ``clickhouse_breakdown`` uses. This store counts
    and ranks every value from one copy of the rows.
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
    result: TagRows = [{"kind": "value", "value": value, **totals[value]} for value in ranked[:limit]]
    folded = ranked[limit:]
    if folded:
        members = [gen for value in folded for gen in by_value[value]]
        result.append({"kind": "other", "value": None, **_totals(members)})
    if untagged:
        result.append({"kind": "untagged", "value": None, **_totals(untagged)})
    return TagBreakdown(result)


def clickhouse_breakdown(
    *,
    read_candidates: Callable[[int], dict[str, Any]],
    read_candidate_totals: Callable[[list[int]], TagRows],
    read_histogram: Callable[[list[tuple[int, int]], list[tuple[str, int, int, int]]], TagRows],
    limit: int,
) -> TagBreakdown | None:
    """The breakdown from the bounded ClickHouse reads; None for too many values.

    No read here leans on what an earlier one saw to stay bounded, because
    rows keep arriving between them:

    * The candidates read has no GROUP BY. From fixed-size sketches it says
      about how many distinct values the key has, which decides the refusal,
      and names the values that could keep a row.
    * The totals read groups those candidates and nothing else, however many
      new values have arrived since, and returns their text, generations and
      credit cost. The rows are ranked here by credit cost, then generations,
      then text: the in-memory order.
    * The histogram reads group the at most ``limit`` chosen values, every
      other value as one row and untagged generations as one row.

    Credits and BYOK are told apart inside each statement that sums cost, so
    a usage type that first appears between two reads is classified like any
    other.

    With fewer distinct values than TAG_RANK_CANDIDATES the candidates are
    every value the first read saw, with exact counts. With more, they are
    the heaviest values by cost and by generations that the sketches found,
    and the result is marked approximate: the totals of every row are still
    exact for the rows the last read saw, but a value that belongs among the
    ``limit`` may be in the ``other`` row instead. Rows also arrive between
    the read that chooses the values and the read that totals them, which
    this function cannot see; ``tag_summary`` judges that from the final
    rows.

    Medians are the page's own (``settle_elapsed_median``), settled for each
    group apart: the first histogram read opens no slot and tells each group
    which one or two slots hold its middle values, and the next read opens
    those slots for that group. Every read returns every group with its
    totals, so the totals and the medians both come from the last read alone
    and describe one snapshot. A group whose middle moved to another slot
    between reads, which takes rows arriving in between, is read again with
    the new slots; one that has still not settled after TAG_HISTOGRAM_READS
    reads is reported without a median rather than failing the whole
    breakdown.
    """
    facts = read_candidates(TAG_RANK_CANDIDATES)
    estimate = int(facts["distinct_values"])
    if estimate > TAG_DISTINCT_LIMIT:
        return None
    by_requests = facts["by_requests"]
    # Every value has at least one generation, so a requests list shorter
    # than it was allowed to be, with no count in doubt, is every value.
    complete = len(by_requests) < TAG_RANK_CANDIDATES and all(
        int(error) == 0 for _digest, _count, error in by_requests
    )
    candidates = list(
        dict.fromkeys(int(digest) for digest, _weight, _error in [*by_requests, *facts["by_cost"]])
    )
    ranked: dict[tuple[int, int], dict[str, Any]] = {}
    for row in read_candidate_totals(candidates) if candidates else []:
        pair = (int(row["hash_a"]), int(row["hash_b"]))
        ranked[pair] = {
            "value": str(row["value"]),
            "credit": int(row["credit_microdollars"]),
            "requests": int(row["requests"]),
        }
    order = sorted(
        ranked,
        key=lambda pair: (-ranked[pair]["credit"], -ranked[pair]["requests"], ranked[pair]["value"]),
    )
    top = order[:limit]
    names = {pair: str(ranked[pair]["value"]) for pair in top}
    # The slots opened for each group. A settled group keeps its slots open:
    # the next read has to settle it again.
    opened: dict[TagGroup, set[int]] = {}
    groups: dict[TagGroup, dict[str, Any]] = {}
    medians: dict[TagGroup, int | None] = {}
    for _ in range(TAG_HISTOGRAM_READS):
        cells = sorted((*group, slot) for group, slots in opened.items() for slot in slots)
        groups = {
            (str(row["kind"]), int(row["hash_a"]), int(row["hash_b"])): row
            for row in read_histogram(top, cells)
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
        # A value chosen above and gone by the last read has no row.
        if group in groups:
            result.append({"kind": "value", "value": names[pair], **totals(group)})
    # How many values the other row folds is not reported: by the time it is
    # totalled, values may have arrived that no earlier read counted.
    if ("other", 0, 0) in groups:
        result.append({"kind": "other", "value": None, **totals(("other", 0, 0))})
    if ("untagged", 0, 0) in groups:
        result.append({"kind": "untagged", "value": None, **totals(("untagged", 0, 0))})
    return TagBreakdown(result, approximate=not complete)


def tag_summary(
    *,
    read_keys: Callable[[], TagRows],
    read_values: Callable[[str], TagRows],
    read_breakdown: Callable[[str, int], TagBreakdown | None],
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
    breakdown: TagBreakdown | None = TagBreakdown([])
    for candidate in candidates:
        breakdown = read_breakdown(candidate, TAG_VALUE_LIMIT)
        if chosen is None or breakdown is not None:
            chosen = candidate
        if breakdown is not None:
            break
    rows = order_breakdown(breakdown.rows if breakdown is not None else [])
    return {
        "keys": [{"key": row["key"], "requests": row["requests"]} for row in keys],
        "keys_truncated": keys_truncated,
        "keys_approximate": keys_approximate,
        "group_by": chosen,
        # Ordered by the totals it shows, whatever order the values were
        # chosen in.
        "breakdown": rows,
        "breakdown_too_many_values": breakdown is None,
        "breakdown_approximate": breakdown is not None
        and (breakdown.approximate or other_outweighs_a_named_row(rows)),
        "filter_values": [
            {"value": row["value"], "requests": row["requests"]} for row in values
        ],
        "filter_values_truncated": values_truncated,
        "filter_values_approximate": values_approximate,
    }


def other_outweighs_a_named_row(rows: TagRows) -> bool:
    """Whether the rows themselves fail to vouch for which values were named.

    The named rows are meant to be the largest values by credit cost, then
    generations. No single folded value can be larger than the ``other`` row
    that sums them, so while ``other`` is no larger than the smallest named
    row, no value inside it outweighs a named one. Once it is larger, a value
    inside it may belong among the named rows: one that grew, or first
    appeared, between the read that chose the values and the read that
    totalled them.

    This is deliberately conservative. An ``other`` row made of many small
    values also outweighs the smallest named row, and is reported the same
    way although the ranking is right.
    """
    named = [row for row in rows if row["kind"] == "value"]
    other = [row for row in rows if row["kind"] == "other"]
    if not other:
        return False
    if not named:
        return True

    def weight(row: dict[str, Any]) -> tuple[int, int]:
        return int(row["cost_microdollars"]), int(row["requests"])

    return weight(other[0]) > min(weight(row) for row in named)


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
