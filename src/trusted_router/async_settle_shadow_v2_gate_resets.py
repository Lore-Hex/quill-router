"""Normalize retained reset identities without inventing missing provenance."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_bundle import Context
from trusted_router.async_settle_shadow_v2_gate_failures import detail


def normalized(value: dict[str, Any]) -> dict[str, Any]:
    empty, _ = detail([], 'files')
    entry = {key: value.get(key) for key in a2.RESET_ENTRY.fields}
    entry['support'] = value.get('support', empty)
    a2.RESET_ENTRY(entry)
    return entry


def supported(ctx: Context, entry: dict[str, Any]) -> bool:
    ref = entry['support']
    files = ctx.bundle.a2_streams.get('files/' + ref['root'])
    if files is None or not files:
        return False
    try:
        actual, _ = detail(files, 'files')
    except (ValueError, KeyError, TypeError):
        return False
    if actual != ref:
        return False
    supplied = {p.name: p.raw for p in ctx.bundle.preimages}
    return all(f['path'] in supplied and len(supplied[f['path']]) == f['bytes']
               and roots.file_digest(supplied[f['path']]) == f['sha256'] for f in files)
