"""Reviewed model/host/workload evidence. No IO on the inference path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MIN_SAMPLES = 25
MAX_AGE = timedelta(days=7)
INPUT_LIMITS = (4_096, 32_768, 131_072)
OUTPUT_LIMITS = (256, 2_048, 8_192)
OUTPUT_ESTIMATES = (128, 1_024, 4_096, 16_384)
MAX_ROWS = 20_000
EVIDENCE_PATH = Path(__file__).with_name("data") / "model_routing_evidence.json"


def bucket(tokens: int, limits: tuple[int, ...]) -> int:
    return next((i for i, limit in enumerate(limits) if tokens <= limit), len(limits))


def _micros(at: datetime) -> int:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def validate_evidence(payload: Any) -> tuple[datetime, list[dict[str, Any]]]:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload.get("version") != 1 or payload.get("window") != "7d":
        raise ValueError("unsupported model routing evidence")
    if set(payload) - {"version", "window", "generated_at", "source", "routes"}:
        raise ValueError("unexpected model evidence metadata")
    generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    rows = payload["routes"]
    if generated.tzinfo is None or not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("unbounded model routing evidence")
    seen = set()
    for row in rows:
        if set(row) != {"model", "provider", "input_bucket", "ttft_samples", "ttft_ms",
                        "throughput_samples", "tps_milli"}:
            raise ValueError("unexpected model evidence fields")
        for field in ("model", "provider"):
            if not isinstance(row[field], str) or not row[field] or len(row[field]) > 256:
                raise ValueError("invalid model evidence identity")
            if any(ord(char) < 33 or ord(char) > 126 or char == "|" for char in row[field]):
                raise ValueError("invalid model evidence identity")
        for field in ("input_bucket", "ttft_samples", "throughput_samples"):
            if type(row[field]) is not int or not 0 <= row[field] <= 1_000_000_000:
                raise ValueError("invalid model evidence count")
        if row["input_bucket"] > len(INPUT_LIMITS) or row["ttft_samples"] < MIN_SAMPLES:
            raise ValueError("insufficient or invalid workload evidence")
        for field, maximum in (("ttft_ms", 3_600_000), ("tps_milli", 1_000_000_000)):
            value = row[field]
            if value is not None and (type(value) is not int or not 0 < value <= maximum):
                raise ValueError("invalid model evidence timing")
        if row["ttft_ms"] is None:
            raise ValueError("TTFT evidence is required")
        if row["tps_milli"] is not None and row["throughput_samples"] < MIN_SAMPLES:
            raise ValueError("insufficient throughput evidence")
        identity = (row["model"], row["provider"], row["input_bucket"])
        if identity in seen:
            raise ValueError("duplicate model evidence")
        seen.add(identity)
    return generated, rows


def stabilize(payload: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Retain the published anchor until cumulative movement is material.

    A TTFT change must exceed both 250ms and 20%; throughput must exceed 20%.
    Comparing against the published anchor, not the last observation, permits
    gradual genuine improvement/regression to cross the threshold eventually.
    """
    generated, rows = validate_evidence(payload)
    old: dict[tuple[str, str, int], dict[str, Any]] = {}
    if previous is not None:
        before, old_rows = validate_evidence(previous)
        if timedelta(0) <= generated - before <= MAX_AGE:
            old = {(r["model"], r["provider"], r["input_bucket"]): r for r in old_rows}
    stabilized = []
    for row in rows:
        result = dict(row)
        prior = old.get((row["model"], row["provider"], row["input_bucket"]))
        if prior:
            for field, absolute in (("ttft_ms", 250), ("tps_milli", 0)):
                value, anchor = row[field], prior[field]
                if value is not None and anchor is not None:
                    difference = abs(value - anchor)
                    if difference <= absolute or difference * 5 <= anchor:
                        result[field] = anchor
        stabilized.append(result)
    return {**payload, "routes": stabilized}


def compile_ranks(payload: dict[str, Any]) -> dict[str, Any]:
    generated, rows = validate_evidence(payload)
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["model"], row["input_bucket"]), []).append(row)
    workloads: dict[str, dict[str, dict[str, int]]] = {}
    for (model, input_bucket), group in sorted(groups.items()):
        latency = {r["provider"]: i for i, r in enumerate(sorted(
            group, key=lambda r: (r["ttft_ms"], r["provider"]),
        ))}
        throughput = {r["provider"]: i for i, r in enumerate(sorted(
            (r for r in group if r["tps_milli"] is not None),
            key=lambda r: (-r["tps_milli"], r["provider"]),
        ))}
        for output_bucket, output in enumerate(OUTPUT_ESTIMATES):
            # Compare total estimated duration only when all observed hosts
            # have sustained-throughput evidence; otherwise compare TTFT.
            decode = all(r["tps_milli"] is not None for r in group)
            scores = sorted((
                r["ttft_ms"] + (output * 1_000_000 // r["tps_milli"] if decode else 0),
                r["provider"],
            ) for r in group)
            bands: dict[str, int] = {}
            band, anchor = 0, scores[0][0]
            for score, provider in scores:
                if score - anchor > 250 and (score - anchor) * 5 > anchor:
                    band += 1
                    anchor = score
                bands[provider] = band
            workloads[f"{model}|{input_bucket}|{output_bucket}"] = {
                "default": bands, "latency": latency, "throughput": throughput,
            }
    return {
        "version": 1,
        "measured_from_us": _micros(generated),
        "measured_through_us": _micros(generated + MAX_AGE),
        "workloads": workloads,
    }


def load_ranks() -> dict[str, Any]:
    return compile_ranks(json.loads(EVIDENCE_PATH.read_text()))


_RANKS = load_ranks()


def workload_ranks(
    model: str, input_tokens: int | None, output_tokens: int,
    *, now: datetime | None = None,
) -> dict[str, dict[str, int]]:
    now_us = _micros(now or datetime.now(UTC))
    if input_tokens is None or not _RANKS["measured_from_us"] <= now_us <= _RANKS["measured_through_us"]:
        return {}
    key = f"{model}|{bucket(input_tokens, INPUT_LIMITS)}|{bucket(output_tokens, OUTPUT_LIMITS)}"
    workloads: dict[str, dict[str, dict[str, int]]] = _RANKS["workloads"]
    return workloads.get(key, {})
