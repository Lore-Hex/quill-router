"""Captured packaging completion, separate from the native export horizon."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require


def completion(files: PackFiles, ref: dict[str, Any], *, previous_us: int, command: str) -> int:
    record = retained_json(files.read(ref))
    require(record['command'] == command and record['complete'] is True and
        type(record['completed_at_us']) is int, 'ComposedCompletion')
    latest = previous_us
    def visit(value: Any) -> None:
        nonlocal latest
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('completed_at_us', 'reviewed_at_us') and type(child) is int:
                    latest = max(latest, child)
                elif isinstance(child, (list, dict)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for path, original in files.refs.items():
        if path == ref['path']:
            continue
        try:
            value = retained_json(files.read(original))
        except ValueError:
            continue
        visit(value)
    require(record['completed_at_us'] >= latest, 'ComposedCompletion')
    return int(record['completed_at_us'])
