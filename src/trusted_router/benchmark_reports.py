"""Checked-in monthly benchmark evidence for stable public report URLs."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPORTS_PATH = Path(__file__).parent / "data" / "monthly_benchmark_reports.json"


@lru_cache(maxsize=1)
def monthly_benchmark_reports() -> tuple[dict[str, Any], ...]:
    payload = json.loads(_REPORTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("monthly benchmark reports must be a JSON list")
    reports = tuple(dict(row) for row in payload if isinstance(row, dict))
    if len({str(row.get("period")) for row in reports}) != len(reports):
        raise ValueError("monthly benchmark report periods must be unique")
    return tuple(sorted(reports, key=lambda row: str(row.get("period")), reverse=True))


def monthly_benchmark_report(period: str) -> dict[str, Any] | None:
    return next(
        (row for row in monthly_benchmark_reports() if str(row.get("period")) == period),
        None,
    )


def monthly_benchmark_report_view(report: dict[str, Any]) -> dict[str, Any]:
    period = str(report["period"])
    year, month = period.split("-", 1)
    month_name = (
        "January February March April May June July August September October November December"
    ).split()[int(month) - 1]
    view = dict(report)
    view["period_label"] = f"{month_name} {year}"
    view["row_count_label"] = f"{int(report.get('row_count') or 0):,}"
    view["provider_count_label"] = f"{int(report.get('provider_count') or 0):,}"
    view["model_count_label"] = f"{int(report.get('model_count') or 0):,}"
    view["overall"] = _metrics_view(dict(report.get("overall") or {}))
    view["top_providers"] = [
        {**row, **_metrics_view(row)} for row in report.get("top_providers", [])
    ]
    view["top_model_routes"] = [
        {**row, **_metrics_view(row)} for row in report.get("top_model_routes", [])
    ]
    return view


def _metrics_view(metrics: dict[str, Any]) -> dict[str, Any]:
    availability = metrics.get("provider_availability")
    completion = metrics.get("completion_rate")
    throughput = metrics.get("p50_tokens_per_second")
    return {
        "observation_count_label": f"{int(metrics.get('observation_count') or 0):,}",
        "availability_label": (
            f"{float(availability) * 100:.2f}%" if availability is not None else "not measured"
        ),
        "completion_rate_label": (
            f"{float(completion) * 100:.2f}%" if completion is not None else "not measured"
        ),
        "p50_ttft_label": _milliseconds_label(metrics.get("p50_ttft_ms")),
        "p95_ttft_label": _milliseconds_label(metrics.get("p95_ttft_ms")),
        "throughput_label": (
            f"{float(throughput):,.1f} tok/s" if throughput is not None else "not measured"
        ),
    }


def _milliseconds_label(value: Any) -> str:
    return f"{int(value):,} ms" if value is not None else "not measured"
