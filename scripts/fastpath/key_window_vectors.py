"""Golden vectors for the Go port of the per-key spend-window check
(fastpath/internal/keywindow, docs/design/fast-admission-e2-key-windows.md §7).

Calls Python's own functions, never a re-implementation:
``spend_windows.window_floors`` and ``window_resets_at`` for the ``floors``
cases; ``key_window_limits``, ``enforced_window_limits``,
``key_usage_shard_count`` and ``storage_gcp_authorize.key_window_limit_decision``
itself for the ``decide`` cases, the last over a fake database whose snapshot
returns the case's ``tr_key_limit`` rows (and is never asked for a
reservation), with ``utcnow`` pinned to the case's instant. The route's rule
for when the check runs (``window_limits = enforced_window_limits(api_key)``
for a credits request, and the authorize's ``if window_limits and not
strict_budget``) is read from the source and must equal PINNED_SOURCE, or
this script fails, so a change to the rule fails CI until the vectors and the
design follow.

Each ``decide`` case carries Python's verdict (``none``: the route does not
check, or no rows; ``allow``; ``deny``; ``error``: Python raises) and the
edge's expected outcome: ``no_check`` (no enforced window: the edge decides
the key as an uncapped one and never calls Evaluate), ``decide`` (admit), or
``pass:<reason>`` from keywindow's pass table. The pass rules are the edge's
own policy, not Python's, so they are written out here (``_edge``); Python's
verdict is never computed here.

Instants are integer Unix microseconds (``_us``). No key is real.

The design's third case kind, ``facts`` (an ``api_key`` body read by
``gwstate.ParseKey``), is not here yet: it needs ParseKey's ``KeyWindows``
facts (the design's increment 2), and joins this file with them.

    uv run python scripts/fastpath/key_window_vectors.py
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import os
import random
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Settings must not take inputs from the shell that runs this.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]

from trusted_router import storage_gcp_authorize  # noqa: E402
from trusted_router.spend_windows import (  # noqa: E402
    WINDOWS,
    enforced_window_limits,
    key_window_limits,
    window_floors,
    window_resets_at,
)
from trusted_router.storage_gcp_counters import MAX_KEY_USAGE_SHARDS, key_usage_shard_count  # noqa: E402
from trusted_router.storage_models import ApiKey  # noqa: E402

OUTPUT = ROOT / "fastpath" / "testdata" / "key_window_vectors_v1.json"
GATEWAY = ROOT / "src" / "trusted_router" / "routes" / "internal" / "gateway.py"
STORAGE_GCP = ROOT / "src" / "trusted_router" / "storage_gcp.py"

# keywindow.BoundaryGuard: the edge passes an instant this close to a
# configured window's floor or next reset (both ends inclusive).
BOUNDARY_GUARD_US = 5_000_000
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
# keywindow.MinYear and MaxYear: the edge passes an instant outside them.
MIN_YEAR, MAX_YEAR = 2, 9998

# The rule for when Python checks windows, as the source states it today.
PINNED_SOURCE = {
    "gateway._authorize_gateway_sync_impl: window_limits =": (
        "{} if is_byok_request and (not api_key.include_byok_in_limit) else enforced_window_limits(api_key)"
    ),
    "gateway._authorize_gateway_sync_impl: skip_key_limit=": (
        "api_key.limit_microdollars is None and (not api_key.budget_strict)"
    ),
    "gateway._authorize_gateway_sync_impl: key_capped=": (
        "api_key.limit_microdollars is not None or bool(key_window_limits(api_key))"
    ),
    "storage_gcp.authorize_gateway_typed: if": "window_limits and (not strict_budget)",
}

# The query key_window_limit_decision makes; the fake answers only it, so a
# change to the columns it reads (and their positions) fails here.
SQL = (
    "SELECT shard, day_usage, day_start, week_usage, week_start, "
    "month_usage, month_start FROM tr_key_limit "
    "WHERE key_hash=@kh AND shard>=0 AND shard<@shard_count "
    "ORDER BY shard"
)

PREFIX = {"daily": "day", "weekly": "week", "monthly": "month"}
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _us(at: dt.datetime) -> int:
    delta = at - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _at(us: int) -> dt.datetime:
    return EPOCH + dt.timedelta(microseconds=us)


def _utc(*args: int) -> int:
    return _us(dt.datetime(*args, tzinfo=dt.UTC))  # type: ignore[misc, arg-type]


def _function(path: Path, name: str) -> ast.FunctionDef:
    found = [node for node in ast.walk(ast.parse(path.read_text()))
             if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, (path, name, len(found))
    return found[0]


def source_rule() -> dict[str, str]:
    """The pinned expressions as the source states them now."""
    route = _function(GATEWAY, "_authorize_gateway_sync_impl")
    assigns = [ast.unparse(node.value) for node in ast.walk(route) if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "window_limits" for t in node.targets)]
    keywords: dict[str, list[str]] = {"skip_key_limit": [], "key_capped": []}
    for node in ast.walk(route):
        if isinstance(node, ast.keyword) and node.arg in keywords:
            keywords[node.arg].append(ast.unparse(node.value))
    authorize = _function(STORAGE_GCP, "authorize_gateway_typed")
    gates = [ast.unparse(node.test) for node in ast.walk(authorize) if isinstance(node, ast.If)
             and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                     and call.func.id == "key_window_limit_decision"
                     for stmt in node.body for call in ast.walk(stmt))]
    found = {
        "gateway._authorize_gateway_sync_impl: window_limits =": assigns,
        "gateway._authorize_gateway_sync_impl: skip_key_limit=": keywords["skip_key_limit"],
        "gateway._authorize_gateway_sync_impl: key_capped=": keywords["key_capped"],
        "storage_gcp.authorize_gateway_typed: if": gates,
    }
    for name, values in found.items():
        assert len(values) == 1, (name, values)
    return {name: values[0] for name, values in found.items()}


# ---------------------------------------------------------------- floors


def floors_case(name: str, at_us: int, offset_seconds: int = 0, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    at = _at(at_us)
    floors = window_floors(at)
    resets = {w: window_resets_at(w, at) for w in WINDOWS}
    if offset_seconds:
        # The same instant written in another offset: Python converts to UTC
        # first, so the floors cannot move. Go is held to the same.
        local = at.astimezone(dt.timezone(dt.timedelta(seconds=offset_seconds)))
        assert window_floors(local) == floors, name
        assert {w: window_resets_at(w, local) for w in WINDOWS} == resets, name
    return {
        "name": name, "at_us": at_us, "offset_seconds": offset_seconds, "labels": sorted(labels),
        "floors_us": {w: _us(floors[w]) for w in WINDOWS},
        "resets_us": {w: _us(resets[w]) for w in WINDOWS},
    }


def floors_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def around(name: str, edge_us: int, labels: tuple[str, ...]) -> None:
        cases.append(floors_case(f"{name}_before", edge_us - 1, labels=labels))
        cases.append(floors_case(f"{name}_at", edge_us, labels=labels))
        cases.append(floors_case(f"{name}_after", edge_us + 1, labels=labels))

    around("midnight", _utc(2026, 10, 22), ("midnight",))
    # 2026-11-02 is a Monday: Sunday 23:59:59.999999 is last week.
    around("iso_monday", _utc(2026, 11, 2), ("iso_week", "midnight"))
    for year, month, days in ((2026, 2, 28), (2028, 2, 29), (2026, 4, 30), (2026, 1, 31), (2100, 2, 28)):
        around(f"month_end_{days}d_{year}_{month:02d}", _utc(year, month, days) + 86_400_000_000,
               ("month_end", "midnight"))
    cases.append(floors_case("leap_day_noon", _utc(2028, 2, 29, 12), labels=("leap_day",)))
    cases.append(floors_case("leap_day_2000", _utc(2000, 2, 29, 23, 59, 59), labels=("leap_day",)))
    around("year_end", _utc(2027, 1, 1), ("year_end", "midnight", "month_end"))
    # The ISO week of 2027-01-01 starts Monday 2026-12-28.
    for day in range(7):
        at = _utc(2026, 12, 28) + day * 86_400_000_000 + 13 * 3_600_000_000
        cases.append(floors_case(f"iso_week_across_year_day{day}", at, labels=("iso_week", "year_end")))
    # US and EU daylight-saving changes in 2026 (UTC instants), each written
    # in the zone's offsets on both sides: none is applied.
    for name, at, offsets in (
        ("dst_us_spring", _utc(2026, 3, 8, 7), (-5 * 3600, -4 * 3600)),
        ("dst_us_fall", _utc(2026, 11, 1, 6), (-4 * 3600, -5 * 3600)),
        ("dst_eu_spring", _utc(2026, 3, 29, 1), (3600, 7200)),
        ("dst_eu_fall", _utc(2026, 10, 25, 1), (7200, 3600)),
    ):
        for delta, offset in ((-3_600_000_000, offsets[0]), (0, offsets[1]), (3_600_000_000, offsets[1])):
            cases.append(floors_case(f"{name}_{delta // 3_600_000_000:+d}h", at + delta, offset, ("dst",)))
    # An instant whose local date in a far offset is another day than UTC's.
    cases.append(floors_case("offset_west_other_day", _utc(2026, 10, 22, 2), -12 * 3600, ("offset",)))
    cases.append(floors_case("offset_east_other_day", _utc(2026, 10, 21, 22), 14 * 3600, ("offset",)))
    cases.append(floors_case("epoch", 0, labels=("epoch",)))
    cases.append(floors_case("before_epoch", -1, labels=("epoch",)))
    rng = random.Random(2190)  # noqa: S311 - reproducible test vectors, not secrets.
    for i in range(200):
        at = rng.randint(_utc(1970, 1, 1), _utc(2200, 1, 1))
        offset = rng.choice([0, 0, rng.randint(-12 * 60, 14 * 60) * 60])
        cases.append(floors_case(f"random_{i:03d}", at, offset, ("random",)))
    return cases


# ---------------------------------------------------------------- decide


class _Snapshot:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _Snapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute_sql(self, sql: str, *, params: dict[str, Any], param_types: dict[str, Any]) -> list[tuple[Any, ...]]:
        assert sql == SQL, sql
        assert set(params) == {"kh", "shard_count"}, params
        assert set(param_types) == set(params), param_types
        # The cases' rows are already what this query returns: in range, in
        # shard order (the Go read is the key range [0, shards), the same set).
        shards = [row[0] for row in self._rows]
        assert shards == sorted(shards) and all(0 <= s < params["shard_count"] for s in shards), shards
        return list(self._rows)


class _Database:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def snapshot(self, *, multi_use: bool = False) -> _Snapshot:
        assert multi_use
        return _Snapshot(self._rows)


PARAM_TYPES = types.SimpleNamespace(STRING="STRING", INT64="INT64")


def _key(limits: Mapping[str, int | None], *, alert_only: bool, shards: int) -> ApiKey:
    key = ApiKey(
        hash="kh_synthetic_window_0001", salt="salt-synthetic",
        secret_hash="secret-hash-synthetic",  # noqa: S106 - synthetic test vector, no credential
        lookup_hash="lh_synthetic_window_0001", name="synthetic key", label="synthetic-window-0001",
        workspace_id="ws_synthetic_0001", creator_user_id="user_synthetic_0001",
        created_at="2026-01-01T00:00:00Z",
    )
    return dataclasses.replace(
        key,
        limit_daily_microdollars=limits.get("daily"),
        limit_weekly_microdollars=limits.get("weekly"),
        limit_monthly_microdollars=limits.get("monthly"),
        budget_alert_only=alert_only,
        usage_shard_count=shards,
    )


def _python(key: ApiKey, rows: list[dict[str, Any]], estimate: int, at_us: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "configured": key_window_limits(key),
        "enforced": enforced_window_limits(key),
    }
    if not out["enforced"]:
        # The route passes window_limits={} and the authorize never checks.
        return {**out, "verdict": "none"}
    db_rows = [
        (row["shard"], *[
            value
            for w in WINDOWS
            for value in (row[f"{PREFIX[w]}_usage"],
                          None if row[f"{PREFIX[w]}_start_us"] is None else _at(row[f"{PREFIX[w]}_start_us"]))
        ])
        for row in rows
    ]
    at = _at(at_us)
    try:
        shard_count = key_usage_shard_count(key)
        with mock.patch.object(storage_gcp_authorize, "utcnow", lambda: at):
            decision = storage_gcp_authorize.key_window_limit_decision(
                _Database(db_rows), PARAM_TYPES, key_hash=key.hash, estimate=estimate,
                window_limits=out["enforced"], shard_count=shard_count,
            )
    except (RuntimeError, ValueError, OverflowError) as exc:
        return {**out, "verdict": "error", "error": type(exc).__name__}
    if decision is None:
        return {**out, "verdict": "none"}
    return {
        **out,
        "verdict": "allow" if decision.allowed else "deny",
        "window": decision.window,
        "limit": decision.limit,
        "remaining": decision.remaining,
        "reset_seconds": decision.reset_seconds,
        "resets_at_us": _us(decision.resets_at),
    }


def _edge(case: dict[str, Any], python: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """keywindow's expected outcome, and facts for the labels."""
    enforced: dict[str, int] = python["enforced"]
    facts: dict[str, Any] = {"drift": False, "near": False, "overflow": False, "windows": {}}
    if not enforced:
        return "no_check", facts
    rows, shards = case["rows"], case["shards"]
    if not 1 <= shards <= MAX_KEY_USAGE_SHARDS or [r["shard"] for r in rows] != list(range(shards)):
        return "pass:key_window.rows", facts
    at = _at(case["at_us"])
    if not MIN_YEAR <= at.year <= MAX_YEAR:
        # Python's datetime cannot hold a reset in year 10000.
        facts["overflow"] = True
        return "pass:key_window.overflow", facts
    floors = window_floors(at)
    for w in WINDOWS:
        limit = enforced.get(w)
        if any(r[f"{PREFIX[w]}_limit"] != limit for r in rows):
            facts["drift"] = True
        if limit is None:
            continue
        since = _us(at) - _us(floors[w])
        until = _us(window_resets_at(w, at)) - _us(at)
        if since <= BOUNDARY_GUARD_US or until <= BOUNDARY_GUARD_US:
            facts["near"] = True
        used = 0
        for r in rows:
            start, usage = r[f"{PREFIX[w]}_start_us"], r[f"{PREFIX[w]}_usage"]
            if start is None or start < _us(floors[w]) or usage is None:
                continue
            used += usage
            if not INT64_MIN <= used <= INT64_MAX:  # Go sums in int64, row by row
                facts["overflow"] = True
        if not INT64_MIN <= limit - used <= INT64_MAX:
            facts["overflow"] = True
        facts["windows"][w] = {"used": used, "remaining": max(0, limit - used)}
    if facts["drift"]:
        return "pass:key_window.config_drift", facts
    if facts["near"]:
        return "pass:key_window.boundary", facts
    if facts["overflow"]:
        return "pass:key_window.overflow", facts
    if python["verdict"] != "allow":
        return "pass:key_window.exceeded", facts
    return "decide", facts


def _labels(case: dict[str, Any], python: dict[str, Any], edge: str, facts: dict[str, Any]) -> list[str]:
    labels = set(case.pop("extra_labels"))
    enforced: dict[str, int] = python["enforced"]
    rows = case["rows"]
    at_us = case["at_us"]
    labels.add(python["verdict"])
    if case["alert_only"] and python["configured"]:
        labels.add("alert_only")
    if not python["configured"]:
        labels.add("no_limits")
    if not rows:
        labels.add("no_rows")
    elif [r["shard"] for r in rows] != list(range(case["shards"])):
        labels.add("incomplete_shards")
    if not 1 <= case["shards"] <= MAX_KEY_USAGE_SHARDS:
        labels.add("shard_count_invalid")
    if case["shards"] > 1:
        labels.add("multi_shard")
    if case["estimate"] == 0:
        labels.add("zero_estimate")
    floors = window_floors(_at(at_us))
    for w, limit in enforced.items():
        p = PREFIX[w]
        if limit == 0:
            labels.add("zero_limit")
        if limit < 0:
            labels.add("negative_limit")
        for r in rows:
            if r[f"{p}_start_us"] is None:
                labels.add("null_start")
            elif r[f"{p}_start_us"] < _us(floors[w]):
                labels.add("stale_start")
            elif r[f"{p}_start_us"] == _us(floors[w]):
                labels.add("start_at_floor")
            if r[f"{p}_usage"] is None:
                labels.add("null_usage")
    values = [case["estimate"], *[v for v in case["limits"].values() if v is not None]]
    values += [r[f"{PREFIX[w]}_usage"] or 0 for r in rows for w in WINDOWS]
    if any(abs(v) >= 2**62 for v in values):
        labels.add("int64_edge")
    windows = facts["windows"]
    if len(windows) > 1:
        labels.add("multi_window")
    for w, f in windows.items():
        if case["estimate"] == f["remaining"]:
            labels.add("exact_headroom")
        if case["estimate"] == f["remaining"] + 1:
            labels.add("over_by_one")
        if f["used"] > enforced[w]:
            labels.add("used_over_limit")
    allowed = [f["remaining"] for f in windows.values() if case["estimate"] <= f["remaining"]]
    if len(allowed) == len(windows) > 1 and allowed.count(min(allowed)) > 1:
        labels.add("tie")
    if facts["near"]:
        labels.add("boundary")
    if facts["drift"]:
        labels.add("config_drift")
    if facts["overflow"]:
        labels.add("overflow")
    labels.add("edge_" + edge.split(":")[-1].removeprefix("key_window."))
    return sorted(labels)


def _row(shard: int, limits: Mapping[str, int | None], at_us: int, **values: Any) -> dict[str, Any]:
    """A shard row: zero usage started at the current floor, the mirror equal
    to the limits; ``values`` overrides any column (``day_usage=...``)."""
    floors = window_floors(_at(at_us))
    row: dict[str, Any] = {"shard": shard}
    for w in WINDOWS:
        p = PREFIX[w]
        row[f"{p}_usage"] = 0
        row[f"{p}_start_us"] = _us(floors[w])
        row[f"{p}_limit"] = limits.get(w)
    for name, value in values.items():
        assert name in row, name
        row[name] = value
    return row


# A Wednesday noon, far from every boundary.
AT = _utc(2026, 10, 21, 12, 0, 0, 250_000)
HOUR = 3_600_000_000
DAY = 24 * HOUR


def decide_case(
    name: str,
    limits: Mapping[str, int | None],
    rows: list[dict[str, Any]] | None = None,
    *,
    estimate: int = 100,
    at_us: int = AT,
    shards: int = 1,
    alert_only: bool = False,
    labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    if rows is None:
        rows = [_row(i, limits, at_us) for i in range(shards)]
    case: dict[str, Any] = {
        "name": name,
        "limits": {w: limits.get(w) for w in WINDOWS},
        "alert_only": alert_only,
        "shards": shards,
        "rows": rows,
        "estimate": estimate,
        "at_us": at_us,
        "extra_labels": labels,
    }
    key = _key(limits, alert_only=alert_only, shards=shards)
    python = _python(key, rows, estimate, at_us)
    edge, facts = _edge(case, python)
    if python["verdict"] in ("allow", "deny"):
        governing = facts["windows"].get(python["window"])
        assert governing is None or governing["remaining"] == python["remaining"], name
    case["labels"] = _labels(case, python, edge, facts)
    case["python"] = python
    case["edge"] = edge
    return case


def edge_decide_cases() -> list[dict[str, Any]]:
    d = {"daily": 1_000}
    cases = []

    def add(name: str, limits: Mapping[str, int | None], **kw: Any) -> None:
        cases.append(decide_case(name, limits, **kw))

    def one(limits: Mapping[str, int | None], at_us: int = AT, **values: Any) -> list[dict[str, Any]]:
        return [_row(0, limits, at_us, **values)]

    # One window, headroom arithmetic.
    add("daily_allow", d, rows=one(d, day_usage=400), estimate=500)
    add("daily_exact_headroom", d, rows=one(d, day_usage=400), estimate=600)
    add("daily_over_by_one", d, rows=one(d, day_usage=400), estimate=601)
    add("daily_spent_zero_estimate", d, rows=one(d, day_usage=1_000), estimate=0)
    add("daily_over_zero_estimate", d, rows=one(d, day_usage=1_500), estimate=0)
    add("daily_over_limit", d, rows=one(d, day_usage=1_500), estimate=1)
    add("daily_negative_estimate", d, rows=one(d, day_usage=1_500), estimate=-1)
    add("zero_limit_zero_estimate", {"daily": 0}, estimate=0)
    add("zero_limit_estimate_one", {"daily": 0}, estimate=1)
    add("negative_limit_zero_estimate", {"weekly": -5}, estimate=0)
    add("negative_limit_estimate_one", {"weekly": -5}, estimate=1)
    add("negative_usage_adds_headroom", d, rows=one(d, day_usage=-500), estimate=1_500)
    add("only_weekly", {"weekly": 7_000}, rows=one({"weekly": 7_000}, week_usage=6_000), estimate=1_000)
    add("only_weekly_deny", {"weekly": 7_000}, rows=one({"weekly": 7_000}, week_usage=6_001), estimate=1_000)
    add("only_monthly", {"monthly": 30_000}, rows=one({"monthly": 30_000}, month_usage=29_999), estimate=1)
    add("only_monthly_deny", {"monthly": 30_000}, rows=one({"monthly": 30_000}, month_usage=29_999), estimate=2)

    # Starts and NULLs.
    floors = window_floors(_at(AT))
    day_floor = _us(floors["daily"])
    add("null_start_counts_zero", d, rows=one(d, day_usage=900, day_start_us=None), estimate=1_000)
    add("null_usage_counts_zero", d, rows=one(d, day_usage=None), estimate=1_000)
    add("null_usage_null_start", d, rows=one(d, day_usage=None, day_start_us=None), estimate=1_000)
    add("stale_start_one_us", d, rows=one(d, day_usage=10**9, day_start_us=day_floor - 1), estimate=1_000)
    add("stale_start_last_week", d, rows=one(d, day_usage=10**9, day_start_us=day_floor - 7 * DAY), estimate=1)
    add("start_at_floor_counts", d, rows=one(d, day_usage=1_000, day_start_us=day_floor), estimate=1)
    add("start_after_floor_counts", d, rows=one(d, day_usage=1_000, day_start_us=day_floor + 1), estimate=1)
    add("start_in_future_counts", d, rows=one(d, day_usage=1_000, day_start_us=AT + HOUR), estimate=1)
    add("pre_ddl_row_all_null", {"daily": 1_000, "weekly": 5_000, "monthly": 20_000},
        rows=[_row(0, {"daily": 1_000, "weekly": 5_000, "monthly": 20_000}, AT,
                   day_usage=None, day_start_us=None, week_usage=None, week_start_us=None,
                   month_usage=None, month_start_us=None)], estimate=1_000)

    # Shards.
    m4 = {"daily": 10_000}
    add("multi_shard_sum_allow", m4, shards=4, estimate=2_000,
        rows=[_row(0, m4, AT, day_usage=2_000), _row(1, m4, AT, day_usage=3_000),
              _row(2, m4, AT, day_usage=3_000), _row(3, m4, AT, day_usage=0)])
    add("multi_shard_sum_deny", m4, shards=4, estimate=2_001,
        rows=[_row(0, m4, AT, day_usage=2_000), _row(1, m4, AT, day_usage=3_000),
              _row(2, m4, AT, day_usage=3_000), _row(3, m4, AT, day_usage=0)])
    add("multi_shard_mixed_starts", m4, shards=4, estimate=6_000,
        rows=[_row(0, m4, AT, day_usage=2_000), _row(1, m4, AT, day_usage=9_000, day_start_us=day_floor - 1),
              _row(2, m4, AT, day_usage=None), _row(3, m4, AT, day_usage=5_000, day_start_us=None)])
    add("no_rows", d, rows=[], estimate=1)
    add("no_rows_multi_shard", d, rows=[], shards=3, estimate=1)
    add("incomplete_missing_middle", d, shards=3, rows=[_row(0, d, AT), _row(2, d, AT)])
    add("incomplete_missing_first", d, shards=2, rows=[_row(1, d, AT)])
    add("incomplete_missing_last", d, shards=4, rows=[_row(i, d, AT) for i in range(3)])
    add("shard_count_zero", d, shards=0, rows=[])
    add("shard_count_over_max", d, shards=MAX_KEY_USAGE_SHARDS + 1, rows=[])
    add("shard_count_max", d, shards=MAX_KEY_USAGE_SHARDS, estimate=1_000 - MAX_KEY_USAGE_SHARDS,
        rows=[_row(i, d, AT, day_usage=1) for i in range(MAX_KEY_USAGE_SHARDS)])

    # Several windows: the first rejecting in daily, weekly, monthly order,
    # else the least remaining, the first on a tie.
    all3 = {"daily": 1_000, "weekly": 5_000, "monthly": 20_000}
    add("multi_window_all_allow_daily_least", all3, rows=one(all3, day_usage=500, week_usage=1_000), estimate=100)
    add("multi_window_all_allow_monthly_least", all3,
        rows=one(all3, day_usage=0, week_usage=0, month_usage=19_500), estimate=100)
    add("multi_window_weekly_rejects", all3, rows=one(all3, day_usage=0, week_usage=4_950), estimate=100)
    add("multi_window_daily_and_monthly_reject", all3,
        rows=one(all3, day_usage=950, month_usage=19_950), estimate=100)
    add("multi_window_monthly_rejects_only", all3, rows=one(all3, month_usage=19_950), estimate=100)
    add("tie_daily_weekly", all3, rows=one(all3, day_usage=500, week_usage=4_500), estimate=100)
    wm = {"weekly": 5_000, "monthly": 20_000}
    add("tie_weekly_monthly", wm, rows=one(wm, week_usage=1_000, month_usage=16_000), estimate=100)
    add("tie_all_three_zero_estimate", all3,
        rows=one(all3, day_usage=1_000, week_usage=5_000, month_usage=20_000), estimate=0)
    add("windows_with_own_starts", all3, estimate=600,
        rows=one(all3, day_usage=900, day_start_us=day_floor - 1, week_usage=4_500))
    add("daily_and_monthly_only", {"daily": 1_000, "monthly": 3_000},
        rows=one({"daily": 1_000, "monthly": 3_000}, day_usage=100, month_usage=2_500), estimate=400)

    # The mirrored limit differs from the cached limit (§4.5).
    add("drift_mirror_lower", d, rows=one(d, day_limit=500), estimate=1)
    add("drift_mirror_null", d, rows=one(d, day_limit=None), estimate=1)
    add("drift_mirror_on_unset_window", d, rows=one(d, week_limit=5_000), estimate=1)
    add("drift_one_shard_of_two", d, shards=2, rows=[_row(0, d, AT), _row(1, d, AT, day_limit=999)])
    add("drift_and_exceeded", d, rows=one(d, day_usage=1_000, day_limit=2_000), estimate=1)

    # Alert-only and absent limits: Python never checks, the edge never reads.
    add("alert_only_daily", d, rows=one(d, day_usage=10**6), alert_only=True, estimate=1)
    add("alert_only_all_windows", all3, alert_only=True, estimate=10**9)
    add("alert_only_no_rows", d, rows=[], alert_only=True, estimate=1)
    add("no_limits", {}, estimate=1)
    add("no_limits_alert_only", {}, alert_only=True, estimate=1)

    # Boundaries: the guard is inclusive at both ends, and only configured
    # windows count.
    thu = _utc(2026, 10, 22)  # a Thursday midnight
    for name, at in (
        ("boundary_at_daily_floor", thu),
        ("boundary_floor_plus_guard", thu + BOUNDARY_GUARD_US),
        ("boundary_floor_plus_guard_plus_1us", thu + BOUNDARY_GUARD_US + 1),
        ("boundary_reset_minus_guard", thu - BOUNDARY_GUARD_US),
        ("boundary_reset_minus_guard_minus_1us", thu - BOUNDARY_GUARD_US - 1),
        ("boundary_last_us_of_day", thu - 1),
    ):
        add(name, d, rows=one(d, at, day_usage=100), at_us=at, estimate=100)
    add("boundary_daily_midnight_weekly_only", {"weekly": 7_000},
        rows=one({"weekly": 7_000}, thu, week_usage=100), at_us=thu, estimate=100)
    mon = _utc(2026, 11, 2)
    add("boundary_weekly_monday", {"weekly": 7_000}, rows=one({"weekly": 7_000}, mon), at_us=mon + 1)
    add("boundary_weekly_sunday_end", {"weekly": 7_000}, rows=one({"weekly": 7_000}, mon - 1), at_us=mon - 1)
    oct31_end = _utc(2026, 11, 1) - 2_000_000
    add("boundary_monthly_end_31d", {"monthly": 30_000}, rows=one({"monthly": 30_000}, oct31_end),
        at_us=oct31_end)
    add("boundary_monthly_mid_month_daily_unset", {"monthly": 30_000},
        rows=one({"monthly": 30_000}, thu), at_us=thu)
    add("boundary_and_exceeded", d, rows=one(d, thu, day_usage=1_000), at_us=thu, estimate=1)

    # Month and year rollover, leap day, DST dates (no DST is applied).
    feb28 = _utc(2026, 2, 28, 12)
    add("month_rollover_stale_monthly", {"monthly": 30_000},
        rows=one({"monthly": 30_000}, feb28, month_usage=29_000, month_start_us=_utc(2026, 1, 31)),
        at_us=feb28, estimate=29_000)
    add("month_rollover_current_monthly", {"monthly": 30_000},
        rows=one({"monthly": 30_000}, feb28, month_usage=29_000, month_start_us=_utc(2026, 2, 1)),
        at_us=feb28, estimate=29_000)
    jan1 = _utc(2027, 1, 1, 12)
    add("year_rollover", all3, at_us=jan1, estimate=800,
        rows=one(all3, jan1, day_usage=900, day_start_us=_utc(2026, 12, 31),
                 week_usage=1_000, week_start_us=_utc(2026, 12, 28),
                 month_usage=19_000, month_start_us=_utc(2026, 12, 15)))
    add("year_rollover_weekly_counts", {"weekly": 5_000}, at_us=jan1, estimate=4_001,
        rows=one({"weekly": 5_000}, jan1, week_usage=1_000, week_start_us=_utc(2026, 12, 28)))
    leap = _utc(2028, 2, 29, 12)
    add("leap_day", all3, at_us=leap, estimate=500,
        rows=one(all3, leap, day_usage=500, day_start_us=_utc(2028, 2, 29), month_usage=10_000))
    mar1 = _utc(2028, 3, 1, 12)
    add("after_leap_day_monthly_stale", {"monthly": 20_000}, at_us=mar1, estimate=20_000,
        rows=one({"monthly": 20_000}, mar1, month_usage=19_000, month_start_us=_utc(2028, 2, 29)))
    for name, at in (("dst_us_spring", _utc(2026, 3, 8, 7)), ("dst_us_fall", _utc(2026, 11, 1, 6)),
                     ("dst_eu_spring", _utc(2026, 3, 29, 1)), ("dst_eu_fall", _utc(2026, 10, 25, 1))):
        add(name, all3, at_us=at, estimate=100, rows=one(all3, at, day_usage=200, week_usage=300),
            labels=("dst",))

    # int64: values at the bounds, sums Go cannot hold.
    big = {"daily": INT64_MAX}
    add("int64_max_limit_max_estimate", big, estimate=INT64_MAX)
    add("int64_max_limit_spent", big, rows=one(big, day_usage=INT64_MAX), estimate=0)
    add("int64_min_limit_zero_usage", {"daily": INT64_MIN}, estimate=0)
    add("int64_min_limit_usage_one", {"daily": INT64_MIN}, rows=one({"daily": INT64_MIN}, day_usage=1), estimate=0)
    add("int64_limit_minus_min_usage", {"daily": 1}, rows=one({"daily": 1}, day_usage=INT64_MIN), estimate=0)
    add("int64_negative_limit_minus_min_usage", {"daily": -1},
        rows=one({"daily": -1}, day_usage=INT64_MIN), estimate=0)
    add("int64_sum_overflows", big, shards=2, estimate=0,
        rows=[_row(0, big, AT, day_usage=INT64_MAX), _row(1, big, AT, day_usage=1)])
    add("int64_sum_overflows_then_returns", big, shards=3, estimate=0,
        rows=[_row(0, big, AT, day_usage=INT64_MAX), _row(1, big, AT, day_usage=1),
              _row(2, big, AT, day_usage=-2)])
    add("int64_sum_at_max", big, shards=2, estimate=0,
        rows=[_row(0, big, AT, day_usage=INT64_MAX - 1), _row(1, big, AT, day_usage=1)])
    add("int64_overflow_in_unset_window", {"weekly": 7_000}, shards=2, estimate=1,
        rows=[_row(0, {"weekly": 7_000}, AT, day_usage=INT64_MAX), _row(1, {"weekly": 7_000}, AT, day_usage=1)])
    add("int64_overflow_in_later_window_after_reject", {"daily": 1, "monthly": INT64_MAX}, shards=2, estimate=5,
        rows=[_row(0, {"daily": 1, "monthly": INT64_MAX}, AT, month_usage=INT64_MAX),
              _row(1, {"daily": 1, "monthly": INT64_MAX}, AT, month_usage=1)])
    # Overflow under another pass reason that takes precedence.
    add("int64_overflow_under_drift", {"daily": 1}, rows=one({"daily": 1}, day_usage=INT64_MIN, day_limit=2),
        estimate=0)
    thu0 = _utc(2026, 10, 22)
    add("int64_overflow_under_boundary", {"daily": 1},
        rows=one({"daily": 1}, thu0, day_usage=INT64_MIN), at_us=thu0, estimate=0)

    # The ends of Python's datetime: a reset in year 10000 raises there.
    for name, at, limits in (
        ("year_9999_december_monthly", _utc(9999, 12, 15, 12), {"monthly": 1_000}),
        ("year_9999_last_day_daily", _utc(9999, 12, 31, 12), {"daily": 1_000}),
        ("year_9999_last_week_weekly", _utc(9999, 12, 29, 12), {"weekly": 1_000}),
        ("year_9999_january_daily", _utc(9999, 1, 15, 12), {"daily": 1_000}),
        ("year_9998_december_monthly", _utc(9998, 12, 15, 12), {"monthly": 1_000}),
        ("year_1_daily", _utc(1, 1, 3, 12), {"daily": 1_000}),
        ("year_2_daily", _utc(2, 1, 3, 12), {"daily": 1_000}),
    ):
        add(name, limits, rows=one(limits, at), at_us=at, estimate=1, labels=("year_range",))
    add("int64_stale_huge_usage_ignored", big, shards=2, estimate=1,
        rows=[_row(0, big, AT, day_usage=INT64_MAX, day_start_us=day_floor - 1), _row(1, big, AT, day_usage=1)])
    return cases


def random_decide_cases() -> list[dict[str, Any]]:
    rng = random.Random(21900)  # noqa: S311 - reproducible test vectors, not secrets.
    cases = []
    for i in range(400):
        if rng.random() < 0.25:
            # Within a few seconds of a UTC midnight: the guard's edges.
            at = rng.randint(_utc(2020, 1, 1), _utc(2035, 1, 1)) // DAY * DAY
            at += rng.randint(-7_000_000, 7_000_000)
        else:
            at = rng.randint(_utc(2020, 1, 1), _utc(2035, 1, 1))
        floors = window_floors(_at(at))
        scale = rng.choice([10, 1_000, 10**6, 10**12])
        limits: dict[str, int | None] = {
            w: (rng.randint(-scale // 10, scale) if rng.random() < 0.6 else None) for w in WINDOWS
        }
        shards = rng.choice([1, 1, 1, 2, 3, 4, 8])
        rows = []
        for shard in range(shards):
            row: dict[str, Any] = {"shard": shard}
            for w in WINDOWS:
                p = PREFIX[w]
                fl = _us(floors[w])
                row[f"{p}_usage"] = None if rng.random() < 0.05 else rng.randint(-scale // 20, scale // shards + 1)
                row[f"{p}_start_us"] = rng.choice([None, fl - 1, fl, fl, fl, rng.randint(fl, at), fl - 40 * DAY])
                row[f"{p}_limit"] = limits[w] if rng.random() < 0.97 else rng.choice([None, scale])
            rows.append(row)
        if rng.random() < 0.03:
            rows.pop(rng.randrange(len(rows)))
        estimate = rng.choice([0, rng.randint(0, scale), rng.randint(0, scale // 10 + 1)])
        cases.append(decide_case(
            f"random_{i:03d}", limits, rows, estimate=estimate, at_us=at, shards=shards,
            alert_only=rng.random() < 0.05, labels=("random",),
        ))
    return cases


def vectors() -> dict[str, Any]:
    rule = source_rule()
    if rule != PINNED_SOURCE:
        raise SystemExit(
            "the route's window rule changed; update PINNED_SOURCE, the edge's keywindow use and "
            f"docs/design/fast-admission-e2-key-windows.md together:\n{json.dumps(rule, indent=1)}"
        )
    return {
        "generator": "scripts/fastpath/key_window_vectors.py",
        "boundary_guard_us": BOUNDARY_GUARD_US,
        "max_shards": MAX_KEY_USAGE_SHARDS,
        "windows": list(WINDOWS),
        "pinned_source": PINNED_SOURCE,
        "floors": floors_cases(),
        "decide": edge_decide_cases() + random_decide_cases(),
    }


def render() -> str:
    """The file's text: one case a line, so a change shows as the cases it changes."""
    v = vectors()

    def lines(cases: list[dict[str, Any]]) -> str:
        return ",\n  ".join(json.dumps(c, sort_keys=True, separators=(",", ":")) for c in cases)

    return (
        "{\"generator\": " + json.dumps(v["generator"]) + ",\n"
        + " \"boundary_guard_us\": " + json.dumps(v["boundary_guard_us"]) + ",\n"
        + " \"max_shards\": " + json.dumps(v["max_shards"]) + ",\n"
        + " \"windows\": " + json.dumps(v["windows"]) + ",\n"
        + " \"pinned_source\": " + json.dumps(v["pinned_source"], sort_keys=True) + ",\n"
        + " \"floors\": [\n  " + lines(v["floors"]) + "\n ],\n"
        + " \"decide\": [\n  " + lines(v["decide"]) + "\n ]}\n"
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
