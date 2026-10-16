"""Finite next-pass file reservations and following-pass measured records.

Local staging precedes output planning. A reservation names staged bytes without
including a digest of the plan or its own receipts. The caller carries the final
receipt record to the following pass; it is never inserted into its source graph.
"""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_schema import require

FIELDS = ('export_operations', 'export_bytes', 'export_write_bytes', 'native_rpcs')


def totals(receipts: list[dict[str, Any]]) -> dict[str, int]:
    return dict(export_operations=len(receipts),
        export_bytes=sum(r['bytes'] for r in receipts),
        export_write_bytes=sum(r['bytes'] for r in receipts if r['operation'] == 'put'),
        native_rpcs=sum(r['native_rpcs'] for r in receipts))


def reserve_files(references: list[dict[str, Any]], *, source_pass: int,
                  ceiling: dict[str, int]) -> dict[str, Any]:
    """Reserve one local output operation per staged file before comparing it."""
    require(type(source_pass) is int and source_pass >= 1, 'LaterOutputOrder')
    require(len({r['path'] for r in references}) == len(references), 'LaterOutputPopulation')
    reservations = [dict(path=r['path'], operation='put', bytes=r['bytes'],
                         operations=1, native_rpcs=0) for r in references]
    expected = dict(export_operations=len(reservations),
        export_bytes=sum(r['bytes'] for r in reservations),
        export_write_bytes=sum(r['bytes'] for r in reservations), native_rpcs=0)
    require(all(type(ceiling[k]) is int and 0 <= expected[k] <= ceiling[k] for k in FIELDS),
            'evidence_export_budget_gap')
    return dict(source_pass=source_pass, output_pass=source_pass + 1,
                totals_pass=source_pass + 2, reservations=reservations, ceiling=ceiling)


def compare_files(files: Files, plan: dict[str, Any],
                  references: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the complete emitted population with individual reservations."""
    require(plan['output_pass'] == plan['source_pass'] + 1 and
            plan['totals_pass'] == plan['output_pass'] + 1, 'LaterOutputOrder')
    planned = plan['reservations']
    expected = {r['path']: r for r in planned}
    actual = {r['path']: r for r in references}
    require(len(expected) == len(planned) and len(actual) == len(references)
            and set(actual) == set(expected), 'LaterOutputPopulation')
    reserved = dict(export_operations=sum(r['operations'] for r in planned),
        export_bytes=sum(r['bytes'] for r in planned),
        export_write_bytes=sum(r['bytes'] for r in planned),
        native_rpcs=sum(r['native_rpcs'] for r in planned))
    require(all(type(plan['ceiling'][k]) is int and 0 <= reserved[k] <= plan['ceiling'][k]
                for k in FIELDS), 'evidence_export_budget_gap')
    receipts = []
    for path, ref in sorted(actual.items()):
        reservation = expected[path]
        size = file_size(files, ref)
        require(reservation['operation'] == 'put' and reservation['operations'] == 1
                and reservation['native_rpcs'] == 0, 'LaterOutputOperation')
        require(type(reservation['bytes']) is int and size <= reservation['bytes'],
                'evidence_export_budget_gap')
        receipts.append(dict(object=ref, operation='put', bytes=size, native_rpcs=0,
                             **{'pass': plan['output_pass']}))
    return dict(source_pass=plan['source_pass'], output_pass=plan['output_pass'],
                totals_pass=plan['totals_pass'], receipts=receipts, measured=totals(receipts), reserved=reserved)


def following_totals(files: Files, records: list[dict[str, Any]], *,
                     current_pass: int) -> dict[str, int]:
    """Only the immediately following pass consumes the measured receipts."""
    receipts = []
    seen = set()
    for record in records:
        require(record['source_pass'] + 1 == record['output_pass'] and
                record['output_pass'] + 1 == record['totals_pass'] == current_pass, 'LaterOutputOrder')
        require(record['measured'] == totals(record['receipts']), 'LaterOutputTotals')
        for receipt in record['receipts']:
            key = receipt['pass'], receipt['operation'], receipt['object']['path']
            require(key not in seen and receipt['pass'] == record['output_pass'], 'LaterOutputPopulation')
            seen.add(key)
            require(file_size(files, receipt['object']) == receipt['bytes'], 'LaterOutputBytes')
            receipts.append(receipt)
    return totals(receipts)


def directory_references(root: Any) -> list[dict[str, Any]]:
    """Measure every emitted file, including transport, pages and rendered reports."""
    import hashlib
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result.append(dict(path=path.relative_to(root).as_posix(), bytes=path.stat().st_size, sha256=digest))
    return result


def file_size(files: Files, ref: dict[str, Any]) -> int:
    import hashlib
    path = (files.root / ref['path']).resolve()
    require(path.is_relative_to(files.root.resolve()), 'LaterOutputPath')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    size = path.stat().st_size
    require(digest == ref['sha256'] and size == ref['bytes'], 'LaterOutputBytes')
    return size
