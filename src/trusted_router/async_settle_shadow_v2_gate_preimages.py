"""A2 original-byte preimages and independently keyed F/R/C constructions."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
from trusted_router.async_settle_shadow_v2_gate_result import Subject

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


# These three F inputs are repository release files, not exported FILE_REFs.
# Design §7.5.4 and the A2-S8 ledger fix their paths and existing code pins.
RELEASE_FILES = {
    'proof.evaluator_fixture_sha256': 'tests/fixtures/async_settlement/billing_v1.json',
    'proof.binding_fixture_sha256': 'tests/fixtures/async_settlement/shadow_v1.json',
    'proof.wire_fixture_sha256': 'tests/fixtures/async_settlement/shadow_v2/manifest.json',
}


def object_preimage_schemas(days: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """The A2 payload slots are exact fields, optionally qualified by a declared day."""
    return {**contract.OBJECT_PREIMAGES,
            **{day + '/' + field: schema for day in days
               for field, schema in contract.OBJECT_PREIMAGES.items() if field.startswith('manifest.')}}


def original_files(sources: tuple[Preimage, ...]) -> dict[str, bytes]:
    """Retain conflicting file candidates without selecting a logical body."""
    grouped: dict[str, set[bytes]] = defaultdict(set)
    for source in sources:
        grouped[source.name].add(source.raw)
    result = {}
    for path, candidates in grouped.items():
        if len(candidates) == 1:
            result[path] = next(iter(candidates))
        else:
            for raw in candidates:
                result['file-candidates/' + roots.file_digest(path.encode()) + '/' + roots.file_digest(raw)] = raw
    return result


def preimages(ctx: Context, manifest: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Derive roots from complete logical identities, retaining every candidate."""
    artifacts = original_files(ctx.bundle.preimages)
    artifacts.update(ctx.release_files)
    for qualified in (*contract.OBJECT_PREIMAGES, *contract.FILE_PREIMAGES):
        dated = manifest['day'] + '/' + qualified
        if dated in artifacts:
            artifacts[qualified] = artifacts[dated]
    if ctx.bundle.proof is not None:
        artifacts['proof_manifest'] = ctx.bundle.proof
    grouped: dict[tuple[str, str], list[bytes]] = defaultdict(list)
    for row in (*ctx.bundle.rows, *ctx.bundle.attachments):
        grouped[row.key.kind, row.key.id].append(row.raw)
        artifacts['candidates/' + roots.file_digest(row.raw) + '.json'] = row.raw
    ledgers: dict[str, list[bytes]] = defaultdict(list)
    for raw in ctx.bundle.ledgers:
        try:
            ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        except ValueError:
            continue
        key = a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
        ledgers[key].append(raw)
        artifacts['candidates/' + roots.file_digest(raw) + '.json'] = raw
    result: dict[str, Any] = {}
    day = manifest['day']
    from trusted_router.async_settle_shadow_v2_gate_expected import (
        receipt_keys,
        trace_keys,
        writers,
    )
    expected = writers(ctx, manifest)
    for field, kind in (('counter_root', p1.KINDS[3]), ('closure_root', p1.KINDS[4])):
        result['manifest.' + field] = [dict(key=a2.L(day, boot), bodies=grouped[kind, day + '/' + boot]) for boot in sorted(expected)]
    # A missing page is still a root entry. Enumerate the first committed page
    # and each successor from the counter's HWM and verified predecessor, not
    # merely from the files that happened to arrive in the export.
    result['manifest.receipts_root'] = [dict(key=a2.L(*key.split('/')), bodies=grouped[p1.KINDS[2], key]) for key in sorted(receipt_keys(ctx, manifest))]
    expected_ledgers = {a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us))
                        for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else set()
    result['proof.ledger_root'] = [dict(key=key, bodies=ledgers[key]) for key in sorted(expected_ledgers)]
    day_boots = set(manifest['router_writers']) | set(manifest['enclave_writers'])
    result['manifest.prerequisite_ledger_root'] = [entry for entry in result['proof.ledger_root'] if a2.decode_key(entry['key'])[1] in day_boots]
    result['proof.publisher_slot_trace_sha256'] = [dict(key=a2.L(*key.split('/')[1:]), bodies=grouped['publisher_trace', key]) for key in sorted(trace_keys(ctx))]
    for field, stream in contract.COVERAGE_PREIMAGES.items():
        result[field] = sorted([p for p in ctx.attached('coverage') if p['day'] == day and p['stream'] == stream], key=lambda p: p['page_seq'])
    for field, stream in contract.CROSS_DAY_PREIMAGES.items():
        result[field] = [dict(day=d, pages=sorted([p for p in ctx.attached('coverage') if p['day'] == d and p['stream'] == stream], key=lambda p: p['page_seq'])) for d in ctx.bundle.days]
    # A2 checks each candidate against this finite inventory by iteration. Put
    # digest-qualified candidates first; preserve every key and original byte.
    artifacts = {**{k: v for k, v in artifacts.items() if k.startswith('candidates/')}, **artifacts}
    return artifacts, result


@lru_cache(maxsize=128)
def _entry_digest(stream: str, entries: tuple[tuple[str, tuple[bytes, ...]], ...]) -> str:
    """Memoize only immutable original-byte roots using the contract functions."""
    return roots.entry_root([roots._root_entry(key, bodies) for key, bodies in entries], stream=stream)


def audit(ctx: Context) -> None:
    for issue in ctx.bundle.a2_streams.get('loading_issues', []):
        ctx.issue(issue['gap'], issue['detail'], Subject(wire=a2.O('a2_file', issue['path'])))
    if 'loading_issues' in ctx.bundle.a2_streams:
        ledger_preimages(ctx)
        from trusted_router.async_settle_shadow_v2_gate_expected import audit_expected
        audit_expected(ctx)
    if ctx.proof is None:
        return
    expected_ledgers = {a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us))
                        for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else set()
    actual_ledgers = {a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
                      for ledger in ctx.ledgers}
    for key in sorted(expected_ledgers - actual_ledgers):
        ctx.issue('artifact_gap', 'expected prerequisite ledger absent', Subject(wire=a2.O('prerequisite_ledger', key)))
    for key in sorted(actual_ledgers - expected_ledgers):
        ctx.issue('evidence_schema_gap', 'prerequisite ledger outside independent inventory', Subject(wire=a2.O('prerequisite_ledger', key)))
    for qualified in contract.FILE_PREIMAGES:
        if not qualified.startswith('proof.'):
            continue
        expected = ctx.ports.approved_files.get(qualified) if ctx.ports else None
        if expected is None:
            ctx.issue('artifact_gap', 'approved release file pin absent', Subject(wire=a2.O('a2_file', qualified)))
        elif ctx.proof[qualified.split('.')[1]] != expected:
            ctx.issue('evidence_schema_gap', 'unapproved release fixture pin', Subject(wire=a2.O('a2_file', qualified)))
    for manifest in ctx.manifests():
        if 'router_writers' not in manifest:
            continue
        artifacts, root_inputs = preimages(ctx, manifest)
        for qualified in (*contract.FILE_PREIMAGES, *contract.OBJECT_PREIMAGES):
            if qualified not in artifacts and not any(
                    issue['path'] == qualified for issue in ctx.bundle.a2_streams.get('loading_issues', [])):
                ctx.issue('artifact_gap', 'A2 required original preimage absent',
                    Subject(day=manifest['day'], wire=a2.O('a2_file', qualified)))
        try:
            # Fail the exact original proof commitment before traversing its
            # unchanged global source roots. The contract repeats this check.
            if ctx.bundle.proof is None or roots.file_digest(ctx.bundle.proof) != manifest['proof_manifest_sha256']:
                raise ValueError('artifact_gap')
            inventory = contract._object_bytes(artifacts['manifest.independent_inventory_sha256'], a2.INDEPENDENT_INVENTORY)
            if inventory['day'] != manifest['day']:
                raise ValueError('evidence_schema_gap')
            # Check the same contract R/F construction before its quadratic
            # candidate-presence scan. This is only an early rejection; a new
            # successful input still goes through the complete contract check.
            for qualified, stream in contract.ENTRY_PREIMAGES.items():
                owner, field = qualified.split('.')
                entries = tuple((entry['key'], tuple(entry['bodies'])) for entry in root_inputs[qualified])
                actual = _entry_digest(stream, entries)
                if actual != (manifest if owner == 'manifest' else ctx.proof)[field]:
                    raise ValueError('artifact_gap')
            contract.validate_preimages(manifest, ctx.proof, artifacts=artifacts, roots=root_inputs)
        except KeyError:
            ctx.issue('artifact_gap', 'independent inventory preimage absent',
                Subject(day=manifest['day'], wire=a2.O(p1.KINDS[5], manifest['day'] + '/manifest-v2')))
        except ValueError as exc:
            gap = str(exc) if str(exc) in ('artifact_gap', 'evidence_schema_gap') else 'evidence_schema_gap'
            ctx.issue(gap, 'A2 manifest/proof preimage validation failed',
                Subject(day=manifest['day'], wire=a2.O(p1.KINDS[5], manifest['day'] + '/manifest-v2')))


def ledger_preimages(ctx: Context) -> None:
    """Resolve ledger F commitments from named original files, never hex alone.

    The descriptor has no path. All candidates must therefore be in the finite
    supplied inventory; no filesystem search is permitted. Unknown prerequisite
    evidence formats have an explicit parser seam, not a completion-time guess.
    """
    files: dict[str, set[bytes]] = defaultdict(set)
    for source in ctx.bundle.preimages:
        if len(source.raw) <= 262144:
            files[roots.file_digest(source.raw)].add(source.raw)
    from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    graph = PayloadGraph({p.name: p.raw for p in ctx.bundle.preimages}, ctx.bundle.completed_at_us, ports=ctx.ports)
    previous_files: dict[str, set[bytes]] = defaultdict(set)
    for raw in ctx.bundle.ledgers:
        previous_files[roots.file_digest(raw)].add(raw)
    for ledger in ctx.ledgers:
        key = a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
        subject = Subject(wire=a2.O('prerequisite_ledger', key))
        for field in ('policy_sha256', 'config_sha256', 'fixture_sha256'):
            if len(files[ledger[field]]) != 1:
                ctx.issue('artifact_gap', 'ledger file preimage absent or ambiguous: ' + field, subject)
        previous = ledger['previous_ledger_sha256']
        if previous is not None:
            candidates = previous_files[previous]
            if len(candidates) != 1:
                ctx.issue('artifact_gap', 'previous exact ledger preimage absent or ambiguous', subject)
            else:
                try:
                    contract._object_bytes(next(iter(candidates)), a1.PREREQUISITE_LEDGER)
                except ValueError:
                    ctx.issue('evidence_schema_gap', 'invalid previous ledger preimage', subject)
        for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
            artifact = ledger[field]
            candidates = files[artifact['sha256']]
            if len(candidates) != 1:
                ctx.issue('artifact_gap', 'prerequisite evidence preimage absent or ambiguous: ' + field, subject)
                continue
            raw = next(iter(candidates))
            # These are the only contract-owned completed-evidence wrappers.
            # Retained/opaque tool outputs require the named retained parser.
            evidence = None
            for schema in (a1.GATE_OUTPUT, a2.PROOF_RESULT):
                try:
                    evidence = contract._object_bytes(raw, schema)
                except ValueError:
                    continue
                break
            if evidence is None:
                ctx.issue('evidence_schema_gap', 'ledger_artifact_parser_unbound:' + field, subject)
            elif evidence['completed_at_us'] != artifact['completed_at_us']:
                ctx.issue('artifact_gap', 'prerequisite completion differs from original evidence: ' + field, subject)
            elif 'criterion' in evidence:
                if evidence['criterion'] != field or evidence[ledger['role'] + '_revision'] != ledger['revision']:
                    ctx.issue('evidence_schema_gap', 'prerequisite criterion or producer revision differs: ' + field, subject)
                if isinstance(ctx.ports, LoadingPorts):
                    from trusted_router.async_settle_shadow_v2_gate_provenance import prerequisite
                    try:
                        prerequisite(graph, evidence, ledger, field)
                    except ValueError as exc:
                        ctx.issue(str(exc) if str(exc) == 'artifact_gap' else 'evidence_schema_gap', 'prerequisite provenance: ' + field, subject)
            else:
                # A generic gate output does not establish a particular boot's
                # readiness (§5.3); don't invent a mapping to daily fleet gates.
                ctx.issue('evidence_schema_gap', 'ledger_artifact_context_unbound:' + field, subject)


def _inventory_originals(ctx: Context) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Keep originals and transport declarations in separate collections."""
    artifacts = original_files(ctx.bundle.preimages)
    declarations = []

    def add(member: str, path: str, raw: bytes, kind: str | None = None, key: str | None = None) -> None:
        # A generated transport name cannot replace a supplied original. The
        # report carries the selected reference, so replay needs no name rule.
        if path in artifacts and artifacts[path] != raw:
            path = 'original-roles/' + roots.file_digest(raw)
            while path in artifacts and artifacts[path] != raw:
                path = 'original-roles/' + roots.file_digest(path.encode() + raw)
        artifacts[path] = raw
        declarations.append(dict(member=member, kind=kind, id=key,
            file=dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw))))

    for member in ('rows', 'attachments'):
        for row in getattr(ctx.bundle, member):
            add(member, 'rows/' + row.key.kind + '/' + row.key.id + '/' + roots.file_digest(row.raw),
                row.raw, row.key.kind, row.key.id)
    for raw in ctx.bundle.ledgers:
        add('ledgers', 'ledgers/' + roots.file_digest(raw), raw)
    if ctx.bundle.proof is not None:
        add('proof', 'proof_manifest', ctx.bundle.proof)
    for name in ('transport', 'fleet', 'pre', 'serving'):
        prior = getattr(ctx.bundle, name)
        if prior is not None:
            add(name, 'prior-gates/' + name, prior)
    return artifacts, declarations


def original_role_pages(ctx: Context) -> tuple[dict[str, Any] | None, dict[str, bytes]]:
    from trusted_router.async_settle_shadow_v2_gate_failures import TAG
    _, declarations = _inventory_originals(ctx)
    declarations.sort(key=lambda value: (value['member'], value['file']['path']))
    # Invalid originals already have diagnostics; only bounded references can
    # be carried by a report. Such a report cannot be a passing prior input.
    valid = []
    for declaration in declarations:
        try:
            a2.ORIGINAL_ROLE(declaration)
        except ValueError:
            continue
        valid.append(declaration)
    previous = None
    leaves = {}
    for at in range(0, len(valid), 128):
        raw = a2.ORIGINAL_ROLES.serialize(dict(**TAG, entries=valid[at:at + 128], previous=previous))
        path = 'original-roles/' + roots.file_digest(raw)
        leaves[path] = raw
        previous = dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw))
    return previous, leaves



def input_inventory(ctx: Context) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Inventory exact candidate files, including conflicts, in bounded leaves."""
    from trusted_router.async_settle_shadow_v2_gate_failures import TAG
    from trusted_router.async_settle_shadow_v2_schema import sha256
    artifacts, _ = _inventory_originals(ctx)
    references = [dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw)) for path, raw in sorted(artifacts.items()) if len(raw) <= 262144]
    entries, leaves = [], {}
    for at in range(0, len(references), 512):
        keys = references[at:at + 512]
        raw = a2.INPUT_LEAF.serialize(dict(**TAG, keys=keys, complete=True))
        path = 'input-leaves/' + sha256(dict(index=at // 512)) + '.json'
        leaves[path] = raw
        entries.append(dict(bundle=dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw)),
            first_key=keys[0]['path'], last_key=keys[-1]['path'], objects=len(keys)))
    # Leaves are output artifacts of the finite export, not members of themselves.
    roots.entry_root(entries, stream='input_inventory')
    return entries, leaves


def verify_release_file_preimages(ctx: Context) -> None:
    """Read fixed checkout inputs through the tool's I/O port, checking both pins.

    Exported bytes cannot substitute for a release input. The reader is bound
    to the tool checkout, never to a caller-supplied bundle path. All other
    supplied evidence remains subject to FILE_REF's original-byte bound.
    """
    for qualified, path in RELEASE_FILES.items():
        subject = Subject(wire=a2.O('a2_file', path))
        if ctx.ports is None or ctx.ports.read_release_file is None or ctx.proof is None:
            ctx.issue('artifact_gap', 'release checkout reader absent', subject)
            continue
        try:
            raw = ctx.ports.read_release_file(path)
            actual = roots.file_digest(raw)
        except (OSError, ValueError):
            ctx.issue('artifact_gap', 'release checkout file unavailable', subject)
            continue
        pin = ctx.ports.approved_files.get(qualified)
        if pin is None or actual != pin or actual != ctx.proof[qualified.split('.')[1]]:
            ctx.issue('artifact_gap', 'release checkout file differs from repository or manifest pin', subject)
            continue
        ctx.release_files[qualified] = raw
    for source in ctx.bundle.preimages:
        if len(source.raw) > 262144:
            ctx.issue('artifact_gap', 'exported_file_ref_bound: ' + source.name,
                      Subject(wire=a2.O('a2_file', source.name)))
