"""Reconstruct continuous selected coverage from original platform lifetimes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json


def continuous_coverage(source: Path, plans: dict[str, Any], interval: dict[str, Any]) -> list[str]:
    regions: dict[str, list[tuple[int, int]]] = {}
    for spec in plans.values():
        if 'Inventory' not in spec['captures']:
            return ['WindowWriterCoverage:missing-platform']
        files = Files(source / spec.get('source_root', '.'))
        inventory = retained_json(files.read(spec['captures']['Inventory']))
        cohort = retained_json(files.read(spec['cohort']))
        for region in cohort['required_regions']:
            regions.setdefault(region, [])
        for boot in inventory['boots']:
            if boot['role'] == 'router':
                regions.setdefault(boot['region'], []).append((boot['start_us'], boot['end_us']))
    missing = []
    for region, lives in regions.items():
        cursor = interval['start_us']
        for left, right in sorted(lives):
            if left > cursor:
                break
            cursor = max(cursor, right)
        if cursor < interval['end_us']:
            missing.append('WindowWriterCoverage:' + region)
    return sorted(missing or ([] if regions else ['WindowWriterCoverage:missing-platform']))
