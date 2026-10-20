"""Original-byte boundary drivers and module-scoped caches; real tool ports."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.async_settle import pilot_enablement, shadow_report
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput
from trusted_router.detached_jws import canonical


def library(raw: bytes, ports: Any, gate: str = 'shadow-serving') -> Any:
    from trusted_router.async_settle_shadow_v2_gate_result import run
    _, rendered = run(raw, ports, gate=gate)
    if isinstance(rendered, UnavailableGateOutput):
        return rendered.diagnostic()
    return json.loads(rendered[0])


def tool(raw: bytes, ports: Any, path: Path, which: str, *, gate: str = 'shadow-serving',
         release: bytes | None = None, keys: dict[str, bytes] | None = None,
         options: dict[str, str] | None = None) -> Any:
    path.mkdir(exist_ok=True)
    bundle = path / 'bundle.json'
    bundle.write_bytes(raw)
    plan = path / 'release.json'
    plan.write_bytes(ports.release.raw if release is None else release)
    destination = path / 'out'
    # Every unavailable run must prove it emitted no artifacts, even after PASS.
    import shutil
    shutil.rmtree(destination, ignore_errors=True)
    args = dict(policy='shadow-v2', bundle=str(bundle), release_plan=str(plan),
                release_plan_sha256=roots.file_digest(plan.read_bytes()), output_dir=str(destination))
    for name in ('certificate', 'binding'):
        key = path / (name + '.json')
        key.write_bytes((keys or {}).get(name, next(a.keyset for a in ports.approvals
            if a.entry_point.endswith('.verify_' + name))))
        args[name + '_keys'] = str(key)
    args.update(options or {})
    argv = [which] + ([gate] if which == 'pilot' else ['--gate', gate])
    for key, value in args.items():
        argv.extend(['--' + key.replace('_', '-'), value])
    out = io.StringIO()
    with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out):
        try:
            (pilot_enablement.main if which == 'pilot' else shadow_report.main)()
        except SystemExit as exc:
            code = exc.code
        else:
            raise AssertionError('tool did not exit')
    data = json.loads(out.getvalue())
    assert out.getvalue().encode() == canonical(data) + b'\n'
    if data.get('error'):
        assert code == 1 and not destination.exists()
        assert set(data) == {'policy_version', 'status', 'error', 'condition'}
        assert data['error'] == 'unavailable_required_report_commitments'
    else:
        report = a2.GATE_REPORT.parse((destination / 'report.json').read_bytes())
        files = {roots.file_digest(p.read_bytes()): p.read_bytes() for p in destination.rglob('*') if p.is_file()}
        streams = {}
        for stream in ('writers', 'regions', 'failures', 'input_inventory', 'reset_history'):
            pages = []
            tail = report[stream]['tail_sha256']
            while tail is not None:
                page = roots.STREAMS[stream][1].parse(files[tail])
                pages.append(page)
                tail = page['previous_sha256']
            streams[stream] = list(reversed(pages))
        contract.validate_gate_output(data, report, failures=streams['failures'], writers=streams['writers'],
            regions=streams['regions'], inputs=streams['input_inventory'], reset_history=streams['reset_history'],
            verifiers=report['verifiers'])
        assert code == (0 if data['verdict'] == 'PASS' else 1)
    return data


def damage_cases(documents: dict[str, Any]) -> list[tuple[str, tuple[Any, ...], str, Any]]:
    """Fixed-seed native-path corpus, including every wrong JSON type at each path."""
    import random
    rng = random.Random(1101740)  # noqa: S311 - deterministic damage corpus
    deep: Any = 0
    for _ in range(24):
        deep = [deep]
    cases = []
    def visit(document: str, value: Any, path: tuple[Any, ...]) -> None:
        for replacement in (None, False, 1, 1.5, '', [], {}):
            if type(replacement) is not type(value):
                cases.append((document, path, 'replace', replacement))
        cases.extend((document, path, kind, replacement) for kind, replacement in
            (('delete', None), ('surrogate', '\ud800'), ('nonfinite', float('inf')), ('depth', deep)))
        if isinstance(value, (list, dict, str)) and value:
            cases.append((document, path, 'truncate', None))
            cases.append((document, path, 'reorder', None))
            if isinstance(value, (list, dict)):
                cases.append((document, path, 'duplicate', None))
        children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
        for key, child in children:
            visit(document, child, (*path, key))
    for document, value in documents.items():
        visit(document, value, ())
    # Shuffle path groups, keeping each group's damage variants adjacent for
    # bounded content caching. The seed fixes enumeration and pair sampling.
    groups = {}
    for case in cases:
        groups.setdefault(case[:2], []).append(case)
    values = list(groups.values())
    rng.shuffle(values)
    return [case for group in values for case in group]


def damaged_document(document: Any, path: tuple[Any, ...], kind: str, replacement: Any) -> bytes:
    """Keep embedded original strings byte-identical except at the chosen leaf."""
    import copy
    value = copy.deepcopy(document)
    parent = None
    child = value
    for key in path:
        parent, child = child, child[key]
    if kind == 'delete':
        if parent is None:
            return b'null'
        del parent[path[-1]]
    elif kind == 'duplicate' and isinstance(child, dict):
        # Duplicate the selected member in original transport bytes; the
        # decoder must reject ambiguity without erasing unrelated siblings.
        raw = json.dumps(value, separators=(',', ':'))
        fragment = json.dumps(child, separators=(',', ':'))
        key = next(iter(child))
        duplicate = json.dumps(key) + ':' + json.dumps(child[key], separators=(',', ':'))
        return raw.replace(fragment, fragment[:-1] + ',' + duplicate + '}', 1).encode()
    else:
        if kind == 'truncate':
            replacement = dict(list(child.items())[:len(child)//2]) if isinstance(child, dict) else child[:len(child)//2]
        elif kind == 'reorder':
            replacement = dict(reversed(list(child.items()))) if isinstance(child, dict) else child[::-1]
        elif kind == 'duplicate':
            replacement = child + child[:1]
        if parent is None:
            value = replacement
        else:
            parent[path[-1]] = replacement
    return json.dumps(value, separators=(',', ':')).encode()


def proof_condition(raw: bytes) -> str | None:
    """Independent oracle for the unavailable proof half of E1."""
    from trusted_router.async_settle_shadow_v2_a1_evidence import PROOF_MANIFEST
    try:
        data = json.loads(raw)
        assert isinstance(data, dict) and isinstance(data.get('proof'), str)
        PROOF_MANIFEST.parse(data['proof'].encode())
    except (AssertionError, ValueError, TypeError, UnicodeError):
        return 'proof_manifest_absent_or_undecodable'
    return None


class BoundaryCache:
    """Test-owned content cache in a fixed offline world, never contract state.

    Tools still read/parse their option and approval files on every invocation.
    The loader/evaluator execute once per exact content and trusted input; all
    cached values are copied. Live checkout reads remain in the cache key.
    """
    def __init__(self) -> None:
        from collections import OrderedDict
        self.loads: Any = OrderedDict()
        self.evaluations: Any = OrderedDict()
        self.boundaries: set[Any] = set()

    def trusted(self, ports: Any) -> bytes:
        import pickle
        from dataclasses import fields, is_dataclass

        from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
        from trusted_router.async_settle_shadow_v2_gate_release import TERMINAL_SOURCES
        def freeze(value: Any) -> Any:
            if callable(value):
                return ('callable', id(getattr(value, '__func__', value)), id(getattr(value, '__self__', None)))
            if is_dataclass(value):
                return (type(value).__name__, tuple((f.name, freeze(getattr(value, f.name))) for f in fields(value)))
            if isinstance(value, dict):
                return tuple((k, freeze(v)) for k, v in sorted(value.items()))
            if isinstance(value, (list, tuple)):
                return tuple(freeze(v) for v in value)
            return value
        if ports is None:
            return b''
        from trusted_router import async_settle_shadow_v2_gate_loader as loader
        from trusted_router import async_settle_shadow_v2_gate_release as release
        from trusted_router import async_settle_shadow_v2_gate_views as views
        from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
        boundaries = (loader.validate_original_bytes, release.release_checks,
            *(getattr(views, name) for name in ('budget_view', 'external_view', 'reset_view', 'signature_views')),
            *(getattr(PayloadGraph, name) for name in ('materialize', 'inventory', 'cohort')))
        # A changed view must never reuse an earlier view's outcome. Retain the
        # callables until teardown so Python cannot reuse a discarded identity.
        self.boundaries.update(boundaries)
        values = [('view_boundaries', tuple(id(fn) for fn in boundaries))]
        for field in fields(ports):
            value = getattr(ports, field.name)
            if field.name == 'read_release_file':
                value = tuple((path, value(path)) for path in (*RELEASE_FILES.values(), *TERMINAL_SOURCES)) if value else None
            values.append((field.name, freeze(value)))
        return pickle.dumps(values)

    def call(self, cache: Any, limit: int, function: Any, value: Any, ports: Any, kwargs: Any) -> Any:
        import copy
        import hashlib
        import pickle

        from trusted_router.async_settle_shadow_v2_gate_loader import MAX_BUNDLE_BYTES
        from trusted_router.async_settle_shadow_v2_gate_loading import transport_children
        content = value
        if cache is self.loads:
            try:
                # The native transport boundary still runs for EVERY original
                # input. Equal surviving children and equal rejection subjects
                # are precisely the loader's complete downstream inputs.
                content = transport_children(value, MAX_BUNDLE_BYTES)
            except ValueError:
                pass
        digest = hashlib.sha256(pickle.dumps(content))
        digest.update(str(id(function)).encode())
        digest.update(self.trusted(ports))
        digest.update(repr(kwargs).encode())
        key = digest.digest()
        if key not in cache:
            cache[key] = function(value, ports, **kwargs)
            if len(cache) > limit:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        value = cache[key]
        from trusted_router.async_settle_shadow_v2_gate_result import FailureSet, Result
        memo = {}
        if isinstance(value, Result) and isinstance(value.failures, FailureSet):
            memo[id(value.failures)] = FailureSet(copy.deepcopy(tuple(value.failures)), copy.deepcopy(value.failures.summaries))
        return copy.deepcopy(value, memo)

    @contextlib.contextmanager
    def installed(self) -> Any:
        from trusted_router import async_settle_shadow_v2_gate_loader as loader
        from trusted_router import async_settle_shadow_v2_gates as gates
        original_load, original_evaluate = loader.load, gates.evaluate
        with patch.object(loader, 'load', lambda value, ports=None, **kwargs:
                self.call(self.loads, 16, original_load, value, ports, kwargs)), patch.object(gates, 'evaluate',
                lambda value, ports=None, **kwargs: self.call(self.evaluations, 256, original_evaluate, value, ports, kwargs)):
            yield self
        assert loader.load is original_load and gates.evaluate is original_evaluate
        self.loads.clear()
        self.evaluations.clear()
        self.boundaries.clear()


def inert_damage(document: str, path: tuple[Any, ...], kind: str, replacement: Any,
                 documents: dict[str, Any], changed: bytes, options: Any, *, library_call: bool) -> bool:
    """Enumerate only changes that preserve the effective evidence identity."""
    if document in ('release', 'certificate', 'binding'):
        # These trust roots are byte-pinned, even when a reordered JSON object
        # would mean the same thing to an ordinary consumer.
        return changed == canonical(documents[document])
    if document == 'options':
        supplied = options or {}
        pin = supplied.get('release_plan_sha256', documents['options']['release_plan_sha256'])
        if pin != documents['options']['release_plan_sha256']:
            return False
        # Absent options use the same original descriptors as the explicit
        # valid baseline. A changed path is not assumed equivalent.
        return all(key not in supplied or supplied[key] == documents['options'][key]
                   for key in ('certificate_keys', 'binding_keys'))
    # The transport oracle deliberately leaves later-stage originals supplied.
    # Their absence is now permitted by A3. Compute the exclusive deferred
    # closure from the baseline bytes, independently of the production loader.
    originals = {entry['name']: entry['raw'] for entry in documents['bundle']['preimages']}
    decoded = {}
    digests = {}
    from trusted_router.async_settle_shadow_v2_schema import sha256
    for filename, body in originals.items():
        try:
            decoded[filename] = json.loads(body)
            digests[sha256(decoded[filename])] = filename
        except (ValueError, TypeError):
            pass
    def reachable(seeds: list[Any]) -> set[str]:
        found: set[str] = set()
        pending = list(seeds)
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                references = []
                if set(node) == {'path', 'bytes', 'sha256'}:
                    references.append(node['path'])
                for field in ('tail_sha256', 'previous_sha256'):
                    if node.get(field) in digests:
                        references.append(digests[node[field]])
                for filename in references:
                    if filename not in found:
                        found.add(filename)
                        pending.append(decoded.get(filename))
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        return found
    external = decoded['proof.external_proofs_sha256']
    current = {**external, 'requirements': [item for item in external['requirements'] if item['stage'] == 'transport'], 'pilot': None}
    retained = [value for filename, value in decoded.items()
                if (filename.startswith('proof.') or '/manifest.' in filename)
                and filename != 'proof.external_proofs_sha256']
    # Required original bytes include explicit stage inputs and the complete
    # descriptor closure, rather than just the difference from future proofs.
    seeds = retained + [current]
    required = {filename for filename in originals if filename.startswith('proof.') or '/manifest.' in filename}
    required.add('loading/release-plan.json')
    # Supplied root descriptors retain the frozen validator's direct FILE_REF
    # byte checks even when the referenced proof has no current semantic use.
    direct = [decoded[name] for name in required if name in decoded and name != 'loading/release-plan.json']
    while direct:
        node = direct.pop()
        if isinstance(node, dict):
            if set(node) == set(a2.FILE_REF.fields):
                required.add(node['path'])
            else:
                direct.extend(node.values())
        elif isinstance(node, list):
            direct.extend(node)
    by_file_digest = {roots.file_digest(body.encode()): filename for filename, body in originals.items()}
    ledger_files: set[str] = set()
    for raw in documents['bundle']['ledgers']:
        ledger = json.loads(raw)
        for item in ledger.values():
            if isinstance(item, dict) and set(item) == {'sha256', 'completed_at_us'}:
                filename = by_file_digest.get(item['sha256'])
                if filename is not None:
                    ledger_files.add(filename)
                    required.add(filename)
                    seeds.append(decoded.get(filename))
    required.update(reachable(seeds))
    # Retained output descriptors are mandatory even though the frozen
    # delegated envelope only declares its input and dependency stream.
    pending = list(seeds)
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if set(node) == set(a2.DELEGATED_PAYLOAD.fields) and not node['entry_point'].endswith(('fleet_budgets_pre', 'fleet_budgets')):
                filename = 'loading/retained-output/' + node['payload']['sha256'] + '.json'
                required.add(filename)
                required.update(reachable([decoded.get(filename)]))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    ignored = set(originals) - required
    # DETAIL pages are located by canonical content hash, not their transport
    # filename. The fixture also retains superseded source pages after adding
    # boot context; those orphan pages are not dependencies of any proof.
    named_files: set[str] = set()
    detail_hashes: set[str] = set()
    pending = [*decoded.values(), documents['bundle'].get('producer_registrations', [])]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if set(node) == {'path', 'bytes', 'sha256'}:
                named_files.add(node['path'])
            for field in ('tail_sha256', 'previous_sha256'):
                if isinstance(node.get(field), str):
                    detail_hashes.add(node[field])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    page_raw = set()
    for filename, value in decoded.items():
        if (isinstance(value, dict) and {'entries', 'page_seq', 'previous_sha256'} <= value.keys()
                and filename not in named_files):
            page_raw.add(originals[filename])
            if sha256(value) not in detail_hashes:
                ignored.add(filename)
    # R1/R4: ledger declarations identify these proofs by byte digest, not
    # filename. A valid rename retains that role unless a FILE_REF names it.
    ledger_names = ledger_files - named_files
    ledger_raw = {originals[filename] for filename in ledger_names}
    ignored_raw = {originals[filename] for filename in ignored if filename in originals}
    def valid_path(path: Any) -> bool:
        try:
            a2.FILE_REF.fields['path'](path)
        except (ValueError, TypeError, UnicodeError):
            return False
        return True

    def effective(value: Any) -> Any:
        # Explicit inert set, evaluated on the COMPLETE final document:
        # absent/null optional prior envelopes (no input bytes); absent/null or
        # baseline window (same containment); keyed collection order and exact
        # duplicate entries (same inventory); valid non-shutdown attachment labels
        # (their original body, rather than the transport id, owns identity).
        import copy
        value = copy.deepcopy(value)
        if not isinstance(value, dict):
            return value
        for field in ('serving', 'transport', 'pre', 'fleet'):
            if value.get(field) is None:
                value.pop(field, None)
        if value.get('window') is None or value.get('window') == documents['bundle']['window']:
            value.pop('window', None)
        for field in ('rows', 'attachments', 'preimages', 'ledgers'):
            entries = value.get(field)
            if not isinstance(entries, list):
                continue
            if field == 'preimages':
                entries = [entry for entry in entries if not (isinstance(entry, dict)
                    and set(entry) == {'name', 'raw'} and entry['raw'] in ignored_raw
                    and (entry['name'] in ignored or entry['name'] not in originals)
                    and valid_path(entry['name']))]
                for entry in entries:
                    if (isinstance(entry, dict) and set(entry) == {'name', 'raw'}
                            and entry['raw'] in ledger_raw and valid_path(entry['name'])
                            and entry['name'] not in named_files
                            and (entry['name'] in ledger_names and originals[entry['name']] == entry['raw']
                                 or entry['name'] not in originals)):
                        entry['name'] = '<content-addressed-ledger>'
                    if (isinstance(entry, dict) and set(entry) == {'name', 'raw'}
                            and entry['raw'] in page_raw and entry['name'] not in named_files):
                        try:
                            a2.FILE_REF.fields['path'](entry['name'])
                        except (ValueError, TypeError, UnicodeError):
                            continue
                        entry['name'] = '<content-addressed-page>'
            if field == 'attachments':
                for entry in entries:
                    if (isinstance(entry, dict) and set(entry) == {'kind', 'id', 'raw'}
                            and isinstance(entry['id'], str)
                            and entry['kind'] not in ('shutdown_entry', 'shutdown_measurement')):
                        try:
                            a2.O(entry['kind'], entry['id'])
                            a2.FILE_REF.fields['path']('rows/' + entry['kind'] + '/' + entry['id'] + '/' + '0' * 64)
                        except (ValueError, TypeError, UnicodeError):
                            continue  # Malformed labels are never legitimately inert.
                        entry['id'] = '<body-owned-label>'
            value[field] = sorted({canonical(entry) for entry in entries})
        return value
    try:
        def unique(pairs: Any) -> Any:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate member is not inert')
                result[key] = value
            return result
        final = json.loads(changed, object_pairs_hook=unique)
        declared = decoded.get('loading/release-plan.json', {}).get('artifacts', {})
        if isinstance(final, dict) and isinstance(final.get('preimages'), list):
            for entry in final['preimages']:
                if isinstance(entry, dict) and isinstance(entry.get('raw'), str) and entry.get('name') in declared:
                    if roots.file_digest(entry['raw'].encode()) != declared[entry['name']]:
                        return False
        return effective(final) == effective(documents['bundle'])
    except (ValueError, TypeError):
        return False
