"""Gate consumption of the bundle package's original export accounting."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def select_registrations(files: Mapping[str, bytes], declared_paths: Iterable[str],
                         registrations: tuple[dict[str, Any], ...] | None) -> tuple[dict[str, Any], ...]:
    """Only an absent declaration permits the conventional-path fallback."""
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    if registrations is not None:
        return registrations
    declared = set(declared_paths)
    conventional = 'stage/producer-captures.json'
    if conventional in files and conventional not in declared:
        raw = files[conventional]
        return (dict(path=conventional, bytes=len(raw), sha256=file_digest(raw)),)
    return tuple(dict(path=path, bytes=len(raw), sha256=file_digest(raw))
                 for path, raw in files.items() if path not in declared
                 and (path == 'stage/producer-captures.json' or path.endswith('/stage/producer-captures.json')))


def accounting_prefixes(files: Mapping[str, bytes], paths: Iterable[str],
                        registrations: Iterable[dict[str, Any]] = ()) -> dict[str, str]:
    """Check each selected registration and capture before resolving scope."""
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_loading import PayloadError

    applicable = set(paths)

    def read(ref: dict[str, Any], owner: str) -> Any:
        path = ref['path'] if isinstance(ref, dict) and isinstance(ref.get('path'), str) else owner
        try:
            a2.FILE_REF(ref)
            raw = files[path]
            require(len(raw) == ref['bytes'] and file_digest(raw) == ref['sha256'])
            return retained_json(raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise PayloadError(path, 'evidence_schema_gap') from exc

    prefixes: dict[str, str] = {}
    for registration in registrations:
        path = registration['path']
        try:
            declared = read(registration, path)
            require(type(declared) is dict, 'evidence_schema_gap')
            if 'NativeExport' not in declared:
                continue
            ref = declared['NativeExport']
            capture = read(ref, path)
            path = ref['path']
            require(type(capture) is dict and type(capture.get('passes')) is list and bool(capture['passes']))
            for passed in capture['passes']:
                require(type(passed) is dict and all(key in passed for key in ('bundle', 'accounting', 'prefix', 'object_plan')))
                a2.FILE_REF(passed['accounting'])
                require(type(passed['bundle']) is str and type(passed['prefix']) is str and type(passed['object_plan']) is dict)
                target, prefix = passed['accounting']['path'], passed['prefix']
                if target in applicable:
                    require(target not in prefixes or prefixes[target] == prefix)
                    prefixes[target] = prefix
        except PayloadError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise PayloadError(path, 'evidence_schema_gap') from exc
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

    if graph.producer_registrations is None:
        from trusted_router.async_settle_shadow_v2_gate_dependencies import dependencies
        roles = dependencies(graph.files, payloads, graph.ports, 'post-flip', graph.ledgers,
                             producer_registrations=(), _roles_only=True) if select_registrations(graph.files, (), None) else {}
        graph.producer_registrations = select_registrations(graph.files, roles, None)
    totals = []
    for key, envelope in payloads.items():
        if key != 'manifest.independent_inventory_sha256' and not key.endswith('/manifest.independent_inventory_sha256'):
            continue
        deployment = retained_json(graph.file(envelope['deployment']))
        if 'export_accounting' not in deployment:
            continue
        refs = graph.detail(deployment['export_accounting'], 'files')
        require(bool(refs), 'artifact_gap')
        prefixes = accounting_prefixes(graph.files, (ref['path'] for ref in refs), graph.producer_registrations)
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
