"""Original paths and parsers for the applicable nested file graph."""
from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    BUDGET_MEASUREMENTS,
    RETAINED_OUTPUT,
    SHUTDOWN_SOURCE,
    LoadingPorts,
)
from trusted_router.async_settle_shadow_v2_gate_loading import LIMITS, retained_json
from trusted_router.async_settle_shadow_v2_gate_policy import closure
from trusted_router.async_settle_shadow_v2_schema import Schema, require

Parser = Callable[[bytes], Any]


@lru_cache(maxsize=128)
def python_source(raw: bytes) -> Any:
    try:
        ast.parse(raw)
        return None
    except (SyntaxError, UnicodeError) as exc:
        raise ValueError('evidence_schema_gap') from exc


def original_bytes(raw: bytes) -> bytes:
    """No declared file format: presence, length and digest are checked by file()."""
    return raw


def serving_roster(raw: bytes) -> Any:
    from scripts.async_settle.pilot_enablement import roster_coverage
    value = retained_json(raw)
    require(type(value) is list)
    try:
        roster_coverage(value)
        require(all(type(row['pins']) is dict for row in value))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('evidence_schema_gap') from exc
    return value


def deployment_roster(raw: bytes) -> Any:
    from trusted_router.detached_jws import canonical
    value = retained_json(raw)
    require(type(value) is dict and 'serving' in value)
    serving_roster(canonical(value['serving']))
    return value


def file_list(raw: bytes) -> Any:
    value = retained_json(raw)
    require(type(value) is list)
    for item in value:
        a2.FILE_REF(item)
    return value


def dependencies(files: Mapping[str, bytes], payloads: dict[str, Any], ports: Any, stage: str, ledgers: tuple[bytes, ...] = (), *,
                 references: dict[str, list[dict[str, Any]]] | None = None,
                 producer_registrations: tuple[dict[str, Any], ...] | None = None,
                 _roles_only: bool = False, parsers: dict[str, Parser] | None = None) -> dict[str, Parser]:
    """Follow descriptors, including page links, without checking references.

    The independently approved file inventory resolves missing canonical pages
    to their original paths. Present pages are also indexed by decoded digest,
    including the permitted trailing LF. Parser failures stop only that branch.
    """
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import (
        accounting_prefixes,
        accounting_root,
    )

    if parsers is None:
        parsers = {}
    decoded: dict[str, Any] = {}
    by_digest: dict[str, str] = {}
    if isinstance(ports, LoadingPorts) and ports.release is not None:
        by_digest.update((digest, path) for path, digest in ports.release.artifacts.items())
    # DETAIL declares pages by digest. Index bytes without inspecting content.
    for path, raw in files.items():
        by_digest[roots.file_digest(raw)] = path
        if raw.endswith(b'\n'):
            by_digest[roots.file_digest(raw[:-1])] = path
    pending: list[tuple[str, Any]] = []
    schemas: dict[str, Schema] = {}
    expanded: set[str] = set()

    def add(reference: Any, schema: Schema | None = None, *, source: bool = False, parser: Parser | None = None, declared: bool = True) -> None:
        if not isinstance(reference, dict) or set(reference) != set(a2.FILE_REF.fields):
            return
        path = reference['path']
        if declared and references is not None:
            found = references.setdefault(path, [])
            if reference not in found:
                found.append(reference)
        if schema is not None:
            schemas[path] = schema
        if path not in parsers:
            parsers[path] = original_bytes
        if path not in expanded and (schema is not None or parser is not None and parser not in (original_bytes, python_source)):
            expanded.add(path)
            try:
                if path not in decoded:
                    decoded[path] = retained_json(files[path])
                pending.append((path, decoded[path]))
            except (ValueError, KeyError):
                pass
        if source or parser is not None:
            parsers[path] = python_source if source else parser or original_bytes


    def pages(reference: Any, stream: str, entry_parser: Parser | None = None) -> None:
        if not isinstance(reference, dict) or set(reference) != set(a2.DETAIL.fields):
            return
        tail = reference['tail_sha256']
        seen = set()
        while tail is not None and tail not in seen:
            seen.add(tail)
            path = by_digest.get(tail)
            if path is None:
                break
            add(dict(path=path, bytes=0, sha256=tail), roots.STREAMS[stream][1], declared=False)
            page = decoded.get(path)
            if not isinstance(page, dict):
                break
            if entry_parser is not None:
                for entry in page.get('entries', []):
                    add(entry, parser=entry_parser)
            tail = page.get('previous_sha256')

    if isinstance(ports, LoadingPorts) and ports.release is not None:
        add(dict(path=ports.release.path, bytes=0, sha256='0' * 64), parser=retained_json, declared=False)
        if stage == 'fleet-budgets-pre':
            from trusted_router.async_settle_shadow_v2_gate_scopes import (
                QUALIFICATION_SCOPE,
                qualification_record,
            )
            add(dict(path=QUALIFICATION_SCOPE, bytes=0, sha256='0' * 64), parser=qualification_record, declared=False)
    for raw in ledgers:
        try:
            ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        except ValueError:
            continue
        for item in ledger.values():
            if isinstance(item, dict) and set(item) == {'sha256', 'completed_at_us'}:
                ledger_path = by_digest.get(item['sha256'])
                if ledger_path is not None:
                    add(dict(path=ledger_path, bytes=0, sha256=item['sha256']), a2.PROOF_RESULT, declared=False)
    accounting: dict[str, Any] = {}
    for path, value in payloads.items():
        if path == 'proof.external_proofs_sha256':
            value = {**value, 'requirements': [r for r in value['requirements'] if r['stage'] in closure(stage)],
                     'pilot': value['pilot'] if stage in ('pre-flip', 'post-flip') else None}
        pending.append((path, value))
    while pending:
        origin, node = pending.pop()
        if isinstance(node, list):
            pending.extend((origin, value) for value in node)
            continue
        if not isinstance(node, dict):
            continue
        fields = set(node)
        if fields == set(a2.FILE_REF.fields):
            add(node)
            continue
        if fields == set(a2.APPROVAL.fields):
            add(node['record'], a2.REVIEW_RECORD)
        if fields == set(a2.DELEGATED_PAYLOAD.fields):
            add(node['source'], source=True)
            add(node['payload'], parser=retained_json)
            pages(node['dependencies'], 'files')
            path = 'loading/retained-output/' + node['payload']['sha256'] + '.json'
            # Budget envelopes supply their output explicitly. Other retained
            # entry points require their original completed-output descriptor.
            if (not node['entry_point'].endswith(('fleet_budgets_pre', 'fleet_budgets'))
                    or node['entry_point'].endswith('fleet_budgets_pre') and 'G7' in contract.REQUIRED_RULES[stage]):
                add(dict(path=path, bytes=0, sha256='0' * 64), RETAINED_OUTPUT, declared=False)
        if fields == set(a2.INDEPENDENT_INVENTORY.fields):
            add(node['serving'], parser=serving_roster)
            add(node['deployment'], parser=deployment_roster)
        if parsers.get(origin) is accounting_root and node.get('format') == 'shadow-v2-live-export-a3-n' and isinstance(node.get('details'), dict):
            accounting[origin] = node
            # Finish the role walk before selecting a historical registration.
            continue
        if 'export_accounting' in node:
            pages(node['export_accounting'], 'files', accounting_root)
        if fields == set(a2.SHUTDOWN_INVENTORY.fields):
            pages(node['writers'], 'shutdown_writers')
            add(node['measurements'], parser=file_list)
        if fields == set(a2.SHUTDOWN_ENTRY.fields):
            add(node['measurement'], a2.SHUTDOWN_MEASUREMENT)
            add(node['original_close'], a1.CLOSURE)
            add(node['close_result'], a1.CLOSE_RESULT)
        if fields == set(a2.SHUTDOWN_MEASUREMENT.fields):
            parser = ports.measurements.get(node['source']['path']) if isinstance(ports, LoadingPorts) else None
            add(node['source'], None if parser is not None else SHUTDOWN_SOURCE, parser=parser)
        if fields == set(a2.BUDGET_ENVELOPE.fields):
            add(node['output'], parser=retained_json)
            add(node['v2_load']['limits'], LIMITS)
            pages(node['v2_load']['segments'], 'segments')
            pages(node['v2_load']['operations'], 'operations')
        if fields == set(a2.PROOF_RESULT.fields):
            pages(node['sources'], 'files')
        if 'evidence' in node and 'name' in node and 'stage' in node:
            add(node['evidence'], a2.PROOF_RESULT)
        if fields == set(RETAINED_OUTPUT.fields):
            add(node['output'], parser=retained_json)
        if fields == set(a2.COHORT_SEED_POLICY.fields):
            add(node['configuration'], parser=retained_json)
        if fields == set(a2.RESOLUTION_DAG.fields):
            pages(node['history'], 'reset_history')
        pending.extend((origin, value) for value in node.values())
    for path in parsers:
        qualified = path.split('/', 1)[-1] if '/manifest.' in path else path
        if path in payloads and qualified in contract.OBJECT_PREIMAGES:
            schemas[path] = contract.OBJECT_PREIMAGES[qualified]
    if 'loading/budget-measurements.json' in parsers:
        schemas['loading/budget-measurements.json'] = BUDGET_MEASUREMENTS
    for path, schema in schemas.items():
        def parse_schema(raw: bytes, schema: Schema = schema) -> Any:
            return contract._object_bytes(raw, schema)
        parsers[path] = parse_schema
    if isinstance(ports, LoadingPorts) and ports.release is not None:
        def release_parser(raw: bytes) -> Any:
            from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
            return decode_release_plan(raw, roots.file_digest(raw))
        parsers[ports.release.path] = release_parser
    if _roles_only:
        from trusted_router.async_settle_shadow_v2_gate_native_accounting import accounting_page
        for node in accounting.values():
            for refs in node['details'].values():
                for ref in refs if isinstance(refs, list) else []:
                    if isinstance(ref, dict) and set(ref) == set(a2.FILE_REF.fields):
                        parsers.setdefault(ref['path'], accounting_page)
    else:
        from trusted_router.async_settle_shadow_v2_gate_native_accounting import (
            accounting_page,
            select_registrations,
        )
        roles = (dependencies(files, payloads, ports, 'post-flip', ledgers,
                              producer_registrations=(), _roles_only=True)
                 if producer_registrations is None and select_registrations(files, (), None) else parsers)
        registrations = select_registrations(files, roles, producer_registrations)
        prefixes = accounting_prefixes(files, accounting, registrations, parsers=parsers)
        for origin, node in accounting.items():
            prefix = prefixes.get(origin, '')
            for refs in node['details'].values():
                for ref in refs if isinstance(refs, list) else []:
                    if isinstance(ref, dict) and set(ref) == set(a2.FILE_REF.fields):
                        add({**ref, 'path': prefix + ref['path']}, parser=accounting_page)
    return parsers
