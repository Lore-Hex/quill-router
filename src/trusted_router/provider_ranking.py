"""Bounded, reviewed routing evidence; not a claim about public leaderboard rank."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

UNKNOWN_RANK = 1_000
MAX_AGE = timedelta(days=14)
MIN_SAMPLES = 25
MIN_THROUGHPUT_SAMPLES = 5
MIN_COMPLETION_RATE = 0.95


def build_ranks(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Use measured completion first, then TTFT for the default preference.

    Provider aggregates span different model mixes. They are conservative
    fallback preferences, not per-model speed predictions or health overrides.
    """
    sampled = [r for r in rows if r["samples"] >= MIN_SAMPLES]
    healthy = [r for r in sampled if r["completion_rate"] >= MIN_COMPLETION_RATE]
    latency = sorted(
        (r for r in healthy if r["p50_ttft_ms"] is not None),
        key=lambda r: (r["p50_ttft_ms"], r["provider"]),
    )
    throughput = sorted(
        (r for r in healthy if r["throughput_samples"] >= MIN_THROUGHPUT_SAMPLES
         and r["tokens_per_second"] is not None and r["tokens_per_second"] > 0),
        key=lambda r: (-r["tokens_per_second"], r["provider"]),
    )
    defaults = {r["provider"]: i for i, r in enumerate(latency)}
    degraded = sorted(
        (r for r in sampled if r["completion_rate"] < MIN_COMPLETION_RATE),
        key=lambda r: (-r["completion_rate"], r["provider"]),
    )
    defaults.update({r["provider"]: 2_000 + i for i, r in enumerate(degraded)})
    return {
        "default": defaults,
        "latency": {r["provider"]: i for i, r in enumerate(latency)},
        "throughput": {r["provider"]: i for i, r in enumerate(throughput)},
    }


def validate_snapshot(payload: Any) -> tuple[datetime, dict[str, dict[str, int]]]:
    generated_at = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    if generated_at.tzinfo is None or payload["window"] != "7d":
        raise ValueError("ranking evidence must be timestamped seven-day data")
    seen: set[str] = set()
    for row in payload["providers"]:
        provider = row["provider"]
        if not isinstance(provider, str) or not provider or provider in seen:
            raise ValueError("invalid or duplicate ranking provider")
        seen.add(provider)
        for name in ("samples", "throughput_samples"):
            if type(row[name]) is not int or row[name] < 0:
                raise ValueError("invalid ranking sample count")
        for name in ("completion_rate", "p50_ttft_ms", "tokens_per_second"):
            value = row[name]
            if value is None and name != "completion_rate":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid ranking measurement")
        if row["completion_rate"] > 1:
            raise ValueError("invalid completion rate")
    if not seen:
        raise ValueError("empty ranking evidence")
    return generated_at, build_ranks(payload["providers"])


def _load() -> tuple[datetime, dict[str, dict[str, int]]]:
    path = Path(__file__).with_name("data") / "provider_routing_evidence.json"
    return validate_snapshot(json.loads(path.read_text()))


_GENERATED_AT, _RANKS = _load()


def measured_provider_rank(provider: str, sort: str | None, *, now: datetime | None = None) -> int:
    age = (now or datetime.now(UTC)) - _GENERATED_AT
    if not timedelta(0) <= age <= MAX_AGE:
        return UNKNOWN_RANK
    return _RANKS.get(sort or "default", {}).get(provider, UNKNOWN_RANK)
