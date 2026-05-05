from __future__ import annotations

import json
from typing import Any

from trusted_router.storage_gcp_codec import json_body, reverse_time_key
from trusted_router.storage_models import SyntheticProbeSample


def write_synthetic_probe_sample(table: Any, family: str, sample: SyntheticProbeSample) -> None:
    body = json_body(sample).encode("utf-8")
    day = sample.created_at[:10]
    reverse_time = reverse_time_key(sample.created_at)
    keys = [
        f"synthetic_recent#{reverse_time}#{sample.id}",
        f"synthetic_target_recent#{sample.target}#{reverse_time}#{sample.id}",
        f"synthetic_probe_target_recent#{sample.probe_type}#{sample.target}#{reverse_time}#{sample.id}",
        f"synthetic_monitor_recent#{sample.monitor_region}#{reverse_time}#{sample.id}",
        f"synthetic_day#{day}#{sample.target}#{sample.probe_type}#{reverse_time}#{sample.id}",
        f"synthetic_day_recent#{day}#{reverse_time}#{sample.id}",
    ]
    for key in keys:
        row = table.direct_row(key.encode("utf-8"))
        row.set_cell(family, b"body", body)
        row.commit()


def synthetic_probe_samples(
    table: Any,
    family: str,
    *,
    date: str | None,
    target: str | None,
    probe_type: str | None,
    monitor_region: str | None,
    limit: int,
) -> list[SyntheticProbeSample]:
    prefix, precise = _synthetic_prefix(
        date=date,
        target=target,
        probe_type=probe_type,
        monitor_region=monitor_region,
    )
    read_limit = max(limit, 1) if precise else min(max(limit * 10, limit, 1), 50_000)
    rows = table.read_rows(start_key=prefix, end_key=prefix + b"~", limit=read_limit)
    samples = _samples_from_rows(rows, family)
    filtered = [
        sample
        for sample in samples
        if (date is None or sample.created_at.startswith(date))
        and (target is None or sample.target == target)
        and (probe_type is None or sample.probe_type == probe_type)
        and (monitor_region is None or sample.monitor_region == monitor_region)
    ]
    filtered.sort(key=lambda sample: sample.created_at, reverse=True)
    return filtered[:limit]


def _synthetic_prefix(
    *,
    date: str | None,
    target: str | None,
    probe_type: str | None,
    monitor_region: str | None,
) -> tuple[bytes, bool]:
    if date is not None and target is not None and probe_type is not None:
        return f"synthetic_day#{date}#{target}#{probe_type}#".encode(), True
    if date is not None:
        return f"synthetic_day_recent#{date}#".encode(), target is None and probe_type is None
    if probe_type is not None and target is not None:
        return f"synthetic_probe_target_recent#{probe_type}#{target}#".encode(), True
    if target is not None:
        return f"synthetic_target_recent#{target}#".encode(), True
    if monitor_region is not None:
        return f"synthetic_monitor_recent#{monitor_region}#".encode(), True
    return b"synthetic_recent#", False


def _samples_from_rows(rows: Any, family: str) -> list[SyntheticProbeSample]:
    samples: list[SyntheticProbeSample] = []
    for row in rows:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        samples.append(SyntheticProbeSample(**json.loads(cells[0].value.decode("utf-8"))))
    return samples
