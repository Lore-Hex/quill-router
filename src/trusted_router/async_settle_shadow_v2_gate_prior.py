"""Resolve §7.5 original prior graphs; §8's dependency graph is acyclic.

Transport and pre-enable have no gate predecessor. Fleet requires pre-enable;
serving/pre-flip require transport, fleet and the matching pre-enable. Boot
readiness is the committed §5.3 ledger, not a recursive daily transport gate.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    Bundle,
    Key,
    LoadingPorts,
    Preimage,
    ReleasePlan,
    Row,
)
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_result import Subject
from trusted_router.async_settle_shadow_v2_schema import require, sha256

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context

STAGES = {'transport': 'transport', 'pre': 'fleet-budgets-pre', 'fleet': 'fleet-budgets', 'serving': 'shadow-serving'}
REQUIRED = {'fleet-budgets': ('pre',), 'shadow-serving': ('transport', 'pre', 'fleet'),
            'pre-flip': ('transport', 'pre', 'fleet')}
STREAMS = ('writers', 'regions', 'failures', 'input_inventory', 'reset_history')
# Only complete graph acceptances, keyed by original content and trusted ports.
_VERIFIED: OrderedDict[tuple[Any, ...], None] = OrderedDict()


def _resolve(ctx: Context, name: str, output: dict[str, Any]) -> dict[str, Any]:
    from trusted_router.async_settle_shadow_v2_gate_bundle import ATTACHMENTS, Context
    from trusted_router.async_settle_shadow_v2_gate_preimages import (
        ledger_preimages,
        original_files,
        preimages,
        verify_release_file_preimages,
    )
    require(output.get('gate') == STAGES[name], 'artifact_gap')
    files = original_files(ctx.bundle.preimages)
    by_hash = {roots.file_digest(raw): raw for raw in files.values()}

    def original(digest: str) -> bytes:
        require(digest in by_hash, 'artifact_gap')
        return by_hash[digest]

    report = a2.GATE_REPORT.parse(original(output['coverage_sha256']))
    pages: dict[str, list[dict[str, Any]]] = {}
    for stream in STREAMS:
        desc = report[stream]
        tail = desc['tail_sha256']
        chain: list[dict[str, Any]] = []
        schema = roots.STREAMS[stream][1]
        assert schema is not None
        while tail is not None:
            require(len(chain) < desc['pages'], 'artifact_gap')
            page = schema.parse(original(tail))
            require(sha256(page) == tail, 'artifact_gap')
            chain.append(page)
            tail = page['previous_sha256']
        pages[stream] = list(reversed(chain))
    contract.validate_gate_output(output, report, failures=pages['failures'], writers=pages['writers'],
        regions=pages['regions'], inputs=pages['input_inventory'], reset_history=pages['reset_history'],
        verifiers=report['verifiers'])
    # Derive PASS from the verified report's complete tallies/rules, never the
    # envelope verdict. The validator also rejects a lying envelope.
    require(report['complete'] and not any(t['count'] for t in report['failure_counts'].values())
            and all(s in ('pass', 'not_required') for s in report['rules'].values()), 'artifact_gap')
    require(report['completed_at_us'] <= ctx.bundle.completed_at_us, 'artifact_gap')
    inventory: dict[str, bytes] = {}
    for page in pages['input_inventory']:
        for entry in page['entries']:
            ref = entry['bundle']
            raw = original(ref['sha256'])
            require(len(raw) == ref['bytes'], 'artifact_gap')
            leaf = a2.INPUT_LEAF.parse(raw)
            require(leaf['complete'] and len(leaf['keys']) == entry['objects'], 'artifact_gap')
            require(leaf['keys'][0]['path'] == entry['first_key'] and leaf['keys'][-1]['path'] == entry['last_key'], 'artifact_gap')
            for ref in leaf['keys']:
                raw = original(ref['sha256'])
                require(len(raw) == ref['bytes'] and ref['path'] not in inventory, 'artifact_gap')
                inventory[ref['path']] = raw
    proof_raw = inventory.get('proof_manifest')
    require(proof_raw is not None and roots.file_digest(proof_raw) == report['proof_manifest_sha256'], 'artifact_gap')
    assert proof_raw is not None
    proof = a1.PROOF_MANIFEST.parse(proof_raw)
    require(proof['ledger_root'] == report['prerequisite_ledger_root'], 'artifact_gap')
    require(ctx.proof is not None, 'artifact_gap')
    assert ctx.proof is not None
    for field in ('source_shas', 'cohort_seed_policy_sha256', 'pre_budget_sha256',
                  'evaluator_fixture_sha256', 'binding_fixture_sha256', 'wire_fixture_sha256'):
        require(proof[field] == ctx.proof[field], 'artifact_gap')
    # The old release plan is itself approved by the current release, by exact
    # bytes. Its measurement paths may differ from the new observation window.
    require(isinstance(ctx.ports, LoadingPorts) and ctx.ports.release is not None, 'artifact_gap')
    assert isinstance(ctx.ports, LoadingPorts) and ctx.ports.release is not None
    plan_raw = inventory.get(ctx.ports.release.path)
    require(plan_raw is not None, 'artifact_gap')
    assert plan_raw is not None
    require(plan_raw == ctx.ports.release.raw or roots.file_digest(plan_raw) in ctx.ports.release.artifacts.values(), 'artifact_gap')
    plan = retained_json(plan_raw)
    prior_release = ReleasePlan(ctx.ports.release.path, plan_raw, plan['artifacts'],
        {k: tuple(v) for k, v in plan['criteria'].items()},
        {k: tuple(v) for k, v in plan['verifiers'].items()}, tuple(plan['required']))
    prior_ports = replace(ctx.ports, release=prior_release)
    graph = PayloadGraph(inventory, report['completed_at_us'], ports=prior_ports)
    payloads = {path: contract._object_bytes(raw, contract.OBJECT_PREIMAGES[qualified])
        for path, raw in inventory.items()
        if (qualified := path.split('/', 1)[-1] if '/manifest.' in path else path) in contract.OBJECT_PREIMAGES}
    from trusted_router.async_settle_shadow_v2_gate_release import release_checks
    release_checks(graph, payloads)
    graph.detail(payloads['proof.post_budget_sha256']['v2_load']['segments'], 'segments')
    # Release files are live trusted checkout inputs even on a cache hit.
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    require(prior_ports.read_release_file is not None, 'artifact_gap')
    assert prior_ports.read_release_file is not None
    release_bytes = tuple((field, prior_ports.read_release_file(path)) for field, path in RELEASE_FILES.items())
    for field, raw in release_bytes:
        require(roots.file_digest(raw) == proof[field.split('.')[1]] == prior_ports.approved_files.get(field), 'artifact_gap')
    current_config = tuple(sorted((m['config_sha256'], m['fixture_sha256']) for m in ctx.kind(p1.KINDS[5]) if 'router_writers' in m))
    cache_key = (name, sha256(report), tuple(sorted(inventory.items())), release_bytes, current_config,
        tuple((a.entry_point, a.function, a.source, a.fixture, a.keyset) for a in prior_ports.approvals),
        prior_ports.roster_coverage, prior_ports.gate_roster, contract.validate_preimages)
    if cache_key in _VERIFIED:
        _VERIFIED.move_to_end(cache_key)
        return dict(verdict='PASS', gate=report['gate'], completed_at_us=report['completed_at_us'])
    boots = graph.inventory(payloads, prior_ports)
    rows: list[Row] = []
    attachments: list[Row] = []
    ledgers: list[bytes] = []
    for path, raw in inventory.items():
        if path.startswith('rows/') and path.rsplit('/', 1)[-1] == roots.file_digest(raw):
            kind, key = path[5:].rsplit('/', 1)[0].split('/', 1)
            row = Row(Key(kind, key), raw)
            (attachments if kind in ATTACHMENTS else rows).append(row)
        elif path.startswith('ledgers/'):
            ledgers.append(raw)
    parsed_ledgers = {roots.file_digest(raw): a1.PREREQUISITE_LEDGER.parse(raw) for raw in ledgers}
    # Reconstruct ledger order from links; inventory path order is not chain order.
    ordered: list[bytes] = []
    previous = None
    while len(ordered) < len(ledgers):
        matches = [digest for digest, value in parsed_ledgers.items() if value['previous_ledger_sha256'] == previous]
        require(len(matches) == 1, 'artifact_gap')
        previous = matches[0]
        ordered.append(original(previous))
    bundle = Bundle(rows=tuple(rows), attachments=tuple(attachments), proof=proof_raw,
        ledgers=tuple(ordered), inventory=boots, days=tuple(report['days']),
        completed_at_us=report['completed_at_us'], preimages=tuple(Preimage(k, v) for k, v in inventory.items()),
        transport=inventory.get('prior-gates/transport'), pre=inventory.get('prior-gates/pre'),
        fleet=inventory.get('prior-gates/fleet'), serving=inventory.get('prior-gates/serving'))
    prior = Context(bundle, prior_ports, report['gate'])
    prior.proof = proof
    prior.rows = {row.key: a1.validate_row(row.key.kind, row.key.id, row.raw) for row in rows}
    prior.attachments = {row.key: ATTACHMENTS[row.key.kind].parse(row.raw) for row in attachments}
    prior.ledgers = [a1.PREREQUISITE_LEDGER.parse(raw) for raw in ordered]
    verify_release_file_preimages(prior)
    ledger_preimages(prior)
    require(not prior.issues, 'artifact_gap')
    manifests = prior.kind(p1.KINDS[5])
    require({m['day'] for m in manifests} == set(report['days']), 'artifact_gap')
    for manifest in manifests:
        artifacts, inputs = preimages(prior, manifest)
        contract.validate_preimages(manifest, proof, artifacts=artifacts, roots=inputs)
        # Configuration/policy identity is independent of lifetime/roster churn.
        current = [m for m in ctx.kind(p1.KINDS[5]) if 'router_writers' in m]
        require(bool(current) and all(m['config_sha256'] == manifest['config_sha256']
            and m['fixture_sha256'] == manifest['fixture_sha256'] for m in current), 'artifact_gap')
    expected = {a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us)) for b in boots.boots}
    actual = {a2.L(*(str(v[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us'))) for v in prior.ledgers}
    require(expected == actual, 'artifact_gap')
    for key in REQUIRED.get(report['gate'], ()):
        raw = getattr(bundle, key)
        require(raw is not None, 'artifact_gap')
        assert raw is not None
        _resolve(prior, key, a1.GATE_OUTPUT.parse(raw))
    if name == 'fleet':
        pre = payloads['proof.pre_budget_sha256']
        post = payloads['proof.post_budget_sha256']
        require(post['pre_envelope_sha256'] == sha256(pre) == ctx.proof['pre_budget_sha256'], 'artifact_gap')
    for record in [report['verifiers']['certificate'], report['verifiers']['binding'], *report['verifiers']['retained_tools']]:
        require(record is not None, 'artifact_gap')
        require(prior_release.verifiers.get(record['entry_point']) ==
            (record['source_sha256'], record['fixture_sha256'], record['keyset_sha256']), 'artifact_gap')
    _VERIFIED[cache_key] = None
    if len(_VERIFIED) > 32:
        _VERIFIED.popitem(last=False)
    return dict(verdict='PASS', gate=report['gate'], completed_at_us=report['completed_at_us'])


def verified_prior_gate(ctx: Context, name: str, value: dict[str, Any]) -> None:
    if name not in REQUIRED.get(ctx.gate, ()):
        return
    try:
        ctx.gates[name] = _resolve(ctx, name, value)
    except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
        subject = Subject(wire=a2.O('prior_gate', name))
        ctx.issue('artifact_gap', 'prior_gate_original_graph: ' + STAGES[name] + ': ' + str(exc), subject)
        ctx.issue('pre_enable_budget' if name == 'pre' else 'boot_prerequisite_gap',
                  'required prior graph did not verify')
