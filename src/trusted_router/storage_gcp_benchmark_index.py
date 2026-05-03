from __future__ import annotations

import json
from typing import Any

from trusted_router.storage_gcp_codec import json_body, reverse_time_key
from trusted_router.storage_models import ProviderBenchmarkSample


def write_provider_benchmark(table: Any, family: str, sample: ProviderBenchmarkSample) -> None:
    body = json_body(sample).encode("utf-8")
    day = sample.created_at[:10]
    reverse_time = reverse_time_key(sample.created_at)
    keys = [
        f"benchmark#{day}#{sample.provider}#{sample.model}#{reverse_time}#{sample.id}",
        f"benchmark_provider_day#{day}#{sample.provider}#{reverse_time}#{sample.id}",
        f"benchmark_recent#{reverse_time}#{sample.id}",
        f"benchmark_provider_recent#{sample.provider}#{reverse_time}#{sample.id}",
        f"benchmark_model_recent#{sample.provider}#{sample.model}#{reverse_time}#{sample.id}",
    ]
    for key in keys:
        row = table.direct_row(key.encode("utf-8"))
        row.set_cell(family, b"body", body)
        row.commit()


def provider_benchmark_samples(
    table: Any,
    family: str,
    *,
    date: str | None,
    provider: str | None,
    model: str | None,
    limit: int,
) -> list[ProviderBenchmarkSample]:
    prefix, precise = _benchmark_prefix(date=date, provider=provider, model=model)
    read_limit = max(limit, 1) if precise else min(max(limit * 10, limit, 1), 5000)
    rows = table.read_rows(start_key=prefix, end_key=prefix + b"~", limit=read_limit)
    samples = _samples_from_rows(rows, family)
    filtered = [
        sample
        for sample in samples
        if (date is None or sample.created_at.startswith(date))
        and (provider is None or sample.provider == provider)
        and (model is None or sample.model == model)
    ]
    filtered.sort(key=lambda item: item.created_at, reverse=True)
    return filtered[:limit]


def _benchmark_prefix(
    *,
    date: str | None,
    provider: str | None,
    model: str | None,
) -> tuple[bytes, bool]:
    if date is not None:
        if provider is not None and model is not None:
            return f"benchmark#{date}#{provider}#{model}#".encode(), True
        if provider is not None:
            return f"benchmark_provider_day#{date}#{provider}#".encode(), True
        return f"benchmark#{date}#".encode(), False
    if provider is not None and model is not None:
        return f"benchmark_model_recent#{provider}#{model}#".encode(), True
    if provider is not None:
        return f"benchmark_provider_recent#{provider}#".encode(), model is None
    return b"benchmark_recent#", False


def _samples_from_rows(rows: Any, family: str) -> list[ProviderBenchmarkSample]:
    samples: list[ProviderBenchmarkSample] = []
    for row in rows:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        samples.append(ProviderBenchmarkSample(**json.loads(cells[0].value.decode("utf-8"))))
    return samples
