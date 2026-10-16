"""Gate consumption of the bundle package's original export accounting."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files as Files
from trusted_router.async_settle_shadow_v2_export import DAY_KINDS as DAY_KINDS
from trusted_router.async_settle_shadow_v2_export import OBJECT_KINDS as OBJECT_KINDS
from trusted_router.async_settle_shadow_v2_export import bounds as bounds
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
    compare_export_accounting_streamed,
)
from trusted_router.async_settle_shadow_v2_pack_daypack import AccountingResult as AccountingResult
from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON as CapturedJSON
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def accounting_prefixes(captures: Iterable[bytes]) -> dict[str, str]:
    """Read the declared pass scopes; captures without one keep the empty scope."""
    prefixes: dict[str, str] = {}
    for raw in captures:
        try:
            capture = retained_json(raw)
        except ValueError:
            continue
        if not isinstance(capture, dict) or not isinstance(capture.get('passes'), list):
            continue
        for passed in capture['passes']:
            if not isinstance(passed, dict) or not all(key in passed for key in ('bundle', 'accounting', 'prefix', 'object_plan')):
                continue
            ref = passed['accounting']
            require(isinstance(ref, dict) and isinstance(ref.get('path'), str), 'evidence_schema_gap')
            prefix = passed['prefix']
            require(isinstance(prefix, str), 'evidence_schema_gap')
            require(ref['path'] not in prefixes or prefixes[ref['path']] == prefix, 'evidence_schema_gap')
            prefixes[ref['path']] = prefix
    return prefixes


def accounting_root(raw: bytes) -> dict[str, Any]:
    """The same root format check used before reference-byte comparisons."""
    root = retained_json(raw)
    require(type(root) is dict and type(root.get('details')) is dict and type(root.get('operations')) is dict, 'evidence_schema_gap')
    require(root['format'] == 'shadow-v2-live-export-a3-n', 'evidence_schema_gap')
    require(type(root['native']) is bool and type(root['complete']) is bool, 'evidence_schema_gap')
    for field in ('passes', 'source_body_bytes', 'source_key_bytes', 'source_operations', 'gap_count', 'entry_count'):
        require(type(root[field]) is int and root[field] >= 0, 'evidence_schema_gap')
    require(set(root['details']) <= {'plan', 'entries', 'reads', 'gaps', 'final-gaps'}, 'evidence_schema_gap')
    require(all(type(value) is int and value >= 0 for value in root['operations'].values()), 'evidence_schema_gap')
    return root


def accounting_page(raw: bytes) -> dict[str, Any]:
    value = retained_json(raw)
    require(type(value) is dict and type(value.get('entries')) is list, 'evidence_schema_gap')
    require(all(key in value for key in ('stream', 'sequence', 'previous_sha256')), 'evidence_schema_gap')
    return value


def consume(graph: Any, payloads: dict[str, Any], rows: tuple[Any, ...], attachments: tuple[Any, ...]) -> None:
    """Compare each declared pass with the day's original native records."""
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_gate_loading import PayloadError, retained_json

    class Originals(Files):
        def read(self, ref: dict[str, Any]) -> bytes:
            # Preserve the original-path checks, then use the supplied bounded reader.
            self.put(ref['path'], graph.file(ref))
            return super().read(ref)

    prefixes = None
    totals = []
    for key, envelope in payloads.items():
        if key != 'manifest.independent_inventory_sha256' and not key.endswith('/manifest.independent_inventory_sha256'):
            continue
        deployment = retained_json(graph.file(envelope['deployment']))
        if 'export_accounting' not in deployment:
            continue
        refs = graph.detail(deployment['export_accounting'], 'files')
        require(bool(refs), 'artifact_gap')
        if prefixes is None:
            prefixes = accounting_prefixes(graph.files.values())
        date = envelope['day']
        collector_plan = deployment.get('collector_plan', {})
        require(type(collector_plan) is dict, 'evidence_schema_gap')
        selected_rows = [row for row in rows if row.key.kind in DAY_KINDS
                         and row.key.id.startswith(date + '/') and not row.key.id.endswith('/manifest-v2')]
        selected_attachments = [row for row in attachments
                                if (row.key.kind in OBJECT_KINDS.values() and '/' + date + '/' in row.key.id)
                                or row.key.id in collector_plan]
        object_plan = {row.key.id: None for row in selected_attachments
                       if row.key.kind in OBJECT_KINDS.values() and row.key.id not in collector_plan}
        with TemporaryDirectory(prefix='shadow-v2-accounting-') as folder:
            originals = Originals(Path(folder) / 'originals')
            path = Path(folder) / 'native.json'
            with path.open('wb') as target:
                target.write(b'{')
                for group_index, (group, selected) in enumerate((('rows', selected_rows), ('attachments', selected_attachments))):
                    if group_index:
                        target.write(b',')
                    target.write(canonical(group) + b':[')
                    for index, row in enumerate(selected):
                        if index:
                            target.write(b',')
                        target.write(canonical(dict(kind=row.key.kind, id=row.key.id, raw=row.raw.decode())))
                    target.write(b']')
                target.write(b'}')
            for ref in refs:
                try:
                    result = compare_export_accounting_streamed(originals, ref, path, day=date,
                                                                object_plan=object_plan, collector_plan=collector_plan,
                                                                prefix=prefixes.get(ref['path'], ''))
                except PayloadError:
                    raise
                except (ValueError, KeyError, TypeError) as exc:
                    raise PayloadError(ref['path'], 'evidence_schema_gap') from exc
                require(result.complete, 'evidence_schema_gap')
                totals.append(dict(day=date, path=ref['path'], source_records=len(selected_rows) + len(selected_attachments), native_rpcs=result.native_rpcs,
                                   body_bytes=result.body_bytes, operations=result.operations))
    if totals:
        graph.streams['export_accounting'] = totals
