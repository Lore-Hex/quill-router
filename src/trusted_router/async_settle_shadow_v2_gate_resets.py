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
    support = ctx.bundle.reset_support
    originals = support.support_files.get(ref['root']) if support is not None else None
    if not originals:
        return False
    files = [dict(path=p.name, bytes=len(p.raw), sha256=roots.file_digest(p.raw)) for p in originals]
    try:
        actual, _ = detail(files, 'files')
    except (ValueError, KeyError, TypeError):
        return False
    if actual != ref:
        return False
    # Original inventory remains authoritative if a file is removed/conflicts.
    supplied: dict[str, set[bytes]] = {}
    for source in ctx.bundle.preimages:
        supplied.setdefault(source.name, set()).add(source.raw)
    return all(supplied.get(p.name) == {p.raw} for p in originals)
