"""Read C1 reader originals before selecting any retained basis batch."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router.async_settle_shadow_v2_collect_ports import Files, resolve_emitted_reference
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_references import BasisReferenceIndex
from trusted_router.async_settle_shadow_v2_schema import day_at, require, sha256


def native_reader_index(files: Files, *, records: Iterable[dict[str, Any]],
                        receipts: Iterable[dict[str, Any]], samples: Iterable[dict[str, Any]],
                        batches: Iterable[tuple[str, dict[str, Any]]],
                        originals: dict[tuple[str, str, int], dict[str, Any]] | None = None) -> BasisReferenceIndex:
    """C1 router checks name a basis original; enclave checks name a router path.

    C1's basis FILE_REF is not a batch reference. Read its digest and reader
    coordinates first, then resolve that demand against batch contents. Missing
    batches retain the demand. Optional explicit page coordinates are exact.
    """
    demands = []
    for ref in records:
        value = retained_json(files.read(ref))
        rows = value.get('records', [value])
        for wrapped in rows:
            record = wrapped.get('jsonPayload', wrapped)
            if record.get('kind') != 'a3_boot_check' or record.get('criterion') != 'observer_freshness':
                continue
            if record['role'] == 'enclave':
                require('health_basis' not in record, 'health_source_gap')
                continue
            require(record['role'] == 'router', 'health_source_gap')
            basis_ref = record['health_basis']
            if 'kind' in basis_ref:
                basis_ref = resolve_emitted_reference(files, 'health_basis', basis_ref, originals or {})
            basis = health.HEALTH_BASIS.parse(files.read(basis_ref))
            require(basis['reader_boot'] == record['boot'], 'health_source_gap')
            demands.append(dict(basis_sha256=sha256(basis), boot=basis['reader_boot'],
                day=day_at(basis['read_at_ns'] // 1000),
                health_slot=None if basis['body'] is None else basis['body']['slot'],
                page=record.get('page')))
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_pack_records import RecordSpool
    scratch = TemporaryDirectory(prefix='basis-readers-')
    available = RecordSpool(Path(scratch.name) / 'batches')
    for batch in batches:
        available.append(batch)
    normalized = []
    missing = []
    for demand in demands:
        matches = [key for key, page in available if any(sha256(b) == demand['basis_sha256'] for b in page['bases'])]
        if demand['page'] is not None:
            matches = [demand['page']]
        if not matches:
            missing.append('basis:' + demand['basis_sha256'])
        for key in matches:
            normalized.append({**demand, 'page': key})
    indexed = BasisReferenceIndex.build(receipts=receipts, samples=samples, readers=normalized, batches=available)
    return BasisReferenceIndex(indexed.keys, tuple(sorted(set(indexed.digests) |
        {d['basis_sha256'] for d in demands})), tuple(sorted(set(indexed.missing) | set(missing))))
