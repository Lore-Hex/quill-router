"""Offline capacity arithmetic, not a throughput benchmark or an enablement gate.

JSONL accepts timing records (service_seconds, observed_at), or outbox exports
(created_at, completed_at, service_seconds). arrival_at/created_at are arrivals;
completion timestamps are NEVER substituted for arrivals. Text timing log lines
from async_drain.timing are also accepted. Supply the export observation window,
including idle time, to avoid inventing a rate from the spacing of a few samples.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any


def timestamp(value: Any) -> float:
    result = float(value) if isinstance(value, (int, float)) else dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    if not math.isfinite(result):
        raise ValueError("nonfinite timestamp")
    return result


def parse_lines(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith('{'):
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSONL record must be an object")
        else:
            if "async_drain.timing " not in line:
                raise ValueError("unrecognized timing line")
            row = dict(re.findall(r'(\w+)=([^\s]+)', line))
            for name in ('service_seconds', 'observed_at'):
                row[name] = float(row[name])
        rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]], *, start: float, end: float,
              bucket_seconds: float, margin: float, backlog: int,
              target_seconds: float) -> dict[str, Any]:
    if (not all(math.isfinite(n) for n in (start, end, bucket_seconds, margin, target_seconds))
            or end <= start or bucket_seconds <= 0 or margin < 0 or backlog < 0 or target_seconds <= 0):
        raise ValueError("invalid observation/recovery inputs")
    services = sorted(float(row['service_seconds']) for row in rows if 'service_seconds' in row)
    if not services or any(not math.isfinite(s) or s <= 0 for s in services):
        raise ValueError("positive per-row service_seconds required; completion latency is not service time")
    arrivals = [timestamp(row.get('arrival_at', row.get('created_at'))) for row in rows
                if 'arrival_at' in row or 'created_at' in row]
    if any(not start <= a < end for a in arrivals):
        raise ValueError("arrival outside export observation window")
    def percentile(p: float) -> float:
        return services[max(0, math.ceil(p * len(services)) - 1)]
    mean = sum(services) / len(services)
    result: dict[str, Any] = dict(service_samples=len(services), service_seconds=dict(
        mean=mean, p50=percentile(.5), p95=percentile(.95), maximum=max(services)),
        arrival_samples=len(arrivals), arrival_rate=None, peak_arrival_rate=None,
        concurrency=None, burst_clear_seconds=None,
        assumption="Ideal independent workers: mu(c)=c/mean(service). Validate contention and lease loss with measured concurrency sweeps.")
    if not arrivals:
        result['missing'] = 'No arrivals exported; lambda and required concurrency are unknown.'
        return result
    # Divide the last partial bucket by its actual observed width.
    buckets: dict[int, int] = {}
    for arrival in arrivals:
        index = int((arrival - start) // bucket_seconds)
        buckets[index] = buckets.get(index, 0) + 1
    peak = max(count / min(bucket_seconds, end - (start + index * bucket_seconds))
               for index, count in buckets.items())
    required_rate = max(peak * (1 + margin), peak + backlog / target_seconds)
    concurrency = math.floor(required_rate * mean) + 1  # strict mu > required rate
    mu = concurrency / mean
    clear = backlog / (mu - peak)
    result.update(arrival_rate=len(arrivals) / (end - start), peak_arrival_rate=peak,
                  recovery_margin=margin, concurrency=concurrency, ideal_service_rate=mu,
                  burst_backlog=backlog, target_seconds=target_seconds,
                  burst_clear_seconds=clear, clears_within_target=clear <= target_seconds,
                  calculation="T_clear=B/(c/mean(service)-peak_lambda)")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    for name in ('start', 'end', 'bucket-seconds', 'margin', 'target-seconds'):
        parser.add_argument('--' + name, type=float, required=True)
    parser.add_argument('--backlog', type=int, required=True)
    args = parser.parse_args()
    result = summarize(parse_lines(args.input.read_text()), start=args.start, end=args.end,
                       bucket_seconds=args.bucket_seconds, margin=args.margin,
                       backlog=args.backlog, target_seconds=args.target_seconds)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
