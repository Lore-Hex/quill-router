from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, TypeAlias

from trusted_router.storage_gcp_codec import json_body, reverse_time_key
from trusted_router.storage_gcp_synthetic_rollups import write_synthetic_rollups
from trusted_router.storage_models import SyntheticProbeSample, utcnow
from trusted_router.synthetic.rollups import raw_sample_is_within_retention

FamilyNames: TypeAlias = str | tuple[str, ...]
RETENTION_FAMILY_MAX_AGES = {
    "activity": 30,
    "benchmark": 30,
    "synthetic": 14,
    "rollup": 730,
}


def write_synthetic_probe_sample(
    table: Any,
    family: str,
    sample: SyntheticProbeSample,
    *,
    rollup_family: str | None = None,
    legacy_family: str | None = None,
) -> None:
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
    resolved_rollup_family = rollup_family or family
    rollup_read_families = _ordered_families(
        resolved_rollup_family,
        legacy_family,
    )
    write_synthetic_rollups(
        table,
        resolved_rollup_family,
        sample,
        read_families=rollup_read_families,
    )


def synthetic_probe_samples(
    table: Any,
    family: FamilyNames,
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
        and raw_sample_is_within_retention(sample, now=utcnow())
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


def _samples_from_rows(
    rows: Any,
    family: FamilyNames,
) -> list[SyntheticProbeSample]:
    samples: list[SyntheticProbeSample] = []
    for row in rows:
        cells = _body_cells(row, family)
        if not cells:
            continue
        samples.append(SyntheticProbeSample(**json.loads(cells[0].value.decode("utf-8"))))
    return samples


def _ordered_families(primary: str, legacy: str | None) -> tuple[str, ...]:
    if not legacy or primary == legacy:
        return (primary,)
    return (primary, legacy)


def _body_cells(row: Any, families: FamilyNames) -> list[Any]:
    ordered = (families,) if isinstance(families, str) else families
    for family in ordered:
        cells = row.cells.get(family, {}).get(b"body", [])
        if cells:
            return cells
    return []


def open_generation_table(settings: Any) -> Any:
    """Open the Bigtable generation table for admin/backfill tooling.

    Lives here, in the GCP adapter layer, so that operational scripts do not
    import a cloud SDK themselves. That keeps one architectural rule true
    without exception: `google.*` is imported only by the storage adapter and
    the two explicit cloud ports (`key_management`, `storage_errors`), which
    is what `tests/test_cloud_sdk_boundary.py` enforces.

    This tool is GCP-specific by nature — it rewrites Bigtable rollup rows in
    place — so the goal is not to make it portable, only to keep the SDK
    dependency where it belongs.
    """
    if not settings.gcp_project_id or not settings.bigtable_instance_id:
        raise SystemExit("TR_GCP_PROJECT_ID and TR_BIGTABLE_INSTANCE_ID are required")
    try:
        from google.cloud import bigtable
    except ImportError as exc:  # pragma: no cover - dependency exists in prod image.
        raise SystemExit("google-cloud-bigtable is required") from exc
    return (
        bigtable.Client(project=settings.gcp_project_id, admin=True)
        .instance(settings.bigtable_instance_id)
        .table(settings.bigtable_generation_table)
    )


def configure_retention_families(
    table: Any,
    *,
    apply: bool,
) -> list[dict[str, Any]]:
    """Create/update only the bounded Bigtable families.

    The legacy ``m`` family is intentionally absent from
    ``RETENTION_FAMILY_MAX_AGES`` and is never mutated here. That makes the
    expand migration non-destructive even when old rows share the same table.
    """
    try:
        from google.cloud.bigtable.column_family import (
            GCRuleUnion,
            MaxAgeGCRule,
            MaxVersionsGCRule,
        )
    except ImportError as exc:  # pragma: no cover - dependency exists in prod.
        raise RuntimeError("google-cloud-bigtable is required") from exc

    existing = table.list_column_families()
    actions: list[dict[str, Any]] = []
    for name, days in RETENTION_FAMILY_MAX_AGES.items():
        action = "update" if name in existing else "create"
        actions.append({"family": name, "max_age_days": days, "action": action})
        if not apply:
            continue
        rule = GCRuleUnion(
            [
                MaxAgeGCRule(timedelta(days=days)),
                MaxVersionsGCRule(1),
            ]
        )
        family = table.column_family(name, gc_rule=rule)
        if action == "create":
            family.create()
        else:
            family.update()
    return actions
