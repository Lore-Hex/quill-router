"""Typed views derived from verified A2 envelopes and retained outputs.

BUDGET_MEASUREMENTS is a named supplemental producer format: frozen A2 PEAKS
has no units/fields for the Budget RPC, metadata, export-plan and grace inputs.
It is a separately approved original dependency, never an implicit projection
of a nullable peak or a fabricated zero.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    ALLOWANCES,
    BUDGET_MEASUREMENTS,
    TOTALS,
    Budget,
    External,
    ExternalProof,
    Inventory,
    LoadingPorts,
    Preimage,
    Rate,
    ResetSupport,
    Row,
)
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_retained import delegate
from trusted_router.async_settle_shadow_v2_schema import integer, require, sha256
from trusted_router.detached_jws import canonical

MEASUREMENTS_PATH = 'loading/budget-measurements.json'

# Retained eleven plus the required first-window v2/A1/A2 contract and fail-closed
# integration criteria. Further release-specific criteria may only add to this.
EXTERNAL_REQUIRED = ('deployment_inventory', 'authorization_transport_measurements', 'maximum_header_hops',
    'fleet_load_budget', 'publisher_poll_freshness', 'lifecycle_cpu', 'shared_fixtures',
    'frozen_pricing_crash_mutations', 'd2_d3_capacity_slo', 'policy_rollback_status', 'rare_cases',
    'v2_wire', 'a1_contract', 'a2_contract', 'p4a_fail_closed',
    'a1_time_domain', 'a1_accounting_dormancy', 'a1_report_obligations', 'a1_completion_context',
    'a1_close_handoff', 'a1_provenance_export_load', 'health', 'partition_wire', 'candidate_backing',
    'original_receipt_locator', 'money_echo', 'demand', 'budget_boot', 'durability', 'serialization',
    'bootstrap', 'window', 'live', 'a2_report_commitment', 'a2_failure_counts', 'a2_roots_preimages',
    'a2_verifier_purity', 'a2_packaging_delegation_cost')


def rational(value: dict[str, int]) -> float:
    a2.RATIONAL(value)
    return float(Fraction(value['numerator'], value['denominator']))


def budget_view(graph: PayloadGraph, payloads: dict[str, dict[str, Any]],
                inventory: Inventory, rows: tuple[Row, ...], attachments: tuple[Row, ...] = ()) -> Budget:
    ports = graph.ports
    require(isinstance(ports, LoadingPorts), 'artifact_gap')
    assert isinstance(ports, LoadingPorts)
    adapters = [a for a in ports.approvals if a.function is budget_view]
    require(len(adapters) == 1, 'artifact_gap')
    adapter = adapters[0]
    graph.streams.setdefault('retained_calls', []).append(dict(entry_point=adapter.entry_point, calls=1,
        source_sha256=adapter.source_pin, fixture_sha256=adapter.fixture_pin, keyset_sha256=None))
    pre, post = (payloads['proof.' + name + '_budget_sha256'] for name in ('pre', 'post'))
    require(post['pre_envelope_sha256'] == sha256(pre), 'artifact_gap')
    for envelope in (pre, post):
        require(envelope['v2_load']['interval']['end_us'] <= envelope['v2_load']['approval']['reviewed_at_us']
                <= envelope['approval']['reviewed_at_us'], 'evidence_schema_gap')
    pre_input, pre_result = delegate(graph, pre['retained'], pre['output'])
    post_input, post_result = delegate(graph, post['retained'], post['output'])
    # CLI completion is not part of the Python return. Verify every other byte
    # of this original completed artifact against the recomputed pre result.
    completed_ref = post_input['pre_enable']
    raw = graph.files.get(completed_ref['path'])
    require(raw is not None, 'artifact_gap')
    assert raw is not None
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    require(file_digest(raw) == completed_ref['sha256'], 'artifact_gap')
    completed = retained_json(raw)
    before = completed.pop('completed_at_us')
    integer(before)
    require(canonical(completed) == canonical(pre_result), 'artifact_gap')
    require(before <= pre['approval']['reviewed_at_us'] <= post['approval']['reviewed_at_us'], 'evidence_schema_gap')
    require(post_input['workspace_count'] == pre_result['workspace_count']
        and post_input['maximum_router_instances_by_region'] == pre_result['maximum_router_instances_by_region']
        and post_input['regional_budgets'] == pre_result['regional_budgets']
        and post_input['approved_fleet_reads_per_second'] == pre_result['approved_fleet_reads_per_second'], 'evidence_schema_gap')
    dependencies = graph.detail(post['retained']['dependencies'], 'files')
    extra = next((ref for ref in dependencies if ref['path'] == MEASUREMENTS_PATH), None)
    require(extra is not None, 'artifact_gap')
    assert extra is not None
    measured = graph.object(extra, BUDGET_MEASUREMENTS)
    monitor = retained_json(graph.files[pre_input['monitoring']['path']])
    regions = monitor['regions']
    rates = []
    counters: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.key.kind == p1.KINDS[3]:
            decoded_counter = a1.validate_row(row.key.kind, row.key.id, row.raw)
            key = (decoded_counter['day'], decoded_counter['boot'])
            require(key not in counters or counters[key] == decoded_counter, 'artifact_gap')
            counters[key] = decoded_counter
    segments = graph.detail(post['v2_load']['segments'], 'segments')
    require(bool(segments), 'artifact_gap')
    require({(s['boot'], s['role'], s['region']) for s in segments}
            == {(b.boot, b.role, b.region) for b in inventory.boots}, 'evidence_schema_gap')
    for boot in inventory.boots:
        spans = sorted((s for s in segments if s['boot'] == boot.boot), key=lambda s: s['start_us'])
        require(spans[0]['start_us'] == boot.start_us and spans[-1]['end_us'] == boot.end_us
            and all(a['end_us'] == b['start_us'] for a, b in zip(spans, spans[1:], strict=False)), 'evidence_schema_gap')
    for segment in segments:
        require(segment['workspace_count'] == pre_result['workspace_count'], 'evidence_schema_gap')
        # Equal endpoints are a legal empty half-open load interval.
        if segment['role'] != 'router' or segment['start_us'] == segment['end_us']:
            continue
        counter = counters.get((segment['day'], segment['boot']))
        require(counter is not None, 'artifact_gap')
        assert counter is not None
        require((counter['started_at_us'], counter['flushed_at_us']) ==
                (segment['start_us'], segment['end_us']), 'evidence_schema_gap')
        observer = counter['admission_observer']
        publisher = counter['publisher']
        from trusted_router import async_settle_shadow_v2_a1_health as health
        pages = [health.PUBLISHER_TRACE_PAGE.parse(row.raw) for row in attachments if row.key.kind == 'publisher_trace']
        traces = [entry for page in pages if (page['day'], page['boot']) == (counter['day'], counter['boot']) for entry in page['entries']]
        first, last = (segment['start_us'] + 1999999) // 2000000, (segment['end_us'] - 1) // 2000000
        health.validate_publisher_traces(publisher, sorted(traces, key=lambda t: t['slot']),
            day=counter['day'], boot=counter['boot'], first_slot=first, last_slot=last)
        duration = Fraction(segment['end_us'] - segment['start_us'], 1000000)
        counts = dict(reads_per_second=observer['workspace_reads'] + observer['health_reads']
            + publisher['claim_attempts'] + 3 * publisher['scans'],
            pending_rows_per_second=observer['workspace_reads'] * 1001,
            trust_rows_per_second=observer['workspace_reads'], health_rows_per_second=observer['health_reads'],
            claim_operations_per_second=publisher['claim_attempts'], scan_operations_per_second=publisher['scans'],
            publication_operations_per_second=publisher['publications'])
        for field, count in counts.items():
            value = segment['peaks'][field]
            require(value is not None and Fraction(value['numerator'], value['denominator']) == count / duration,
                    'evidence_schema_gap')
        rates.append(Rate(segment['boot'], segment['start_us'], segment['end_us'],
            observer['workspace_reads'], observer['health_reads'],
            publisher['claim_attempts'] + 3 * publisher['scans'], 2 * publisher['publications']))
    require(graph.shutdown is not None, 'artifact_gap')
    assert graph.shutdown is not None
    shutdown = graph.shutdown.measurements
    require(bool(shutdown), 'artifact_gap')
    caps = pre_result['maximum_router_instances_by_region']
    limits = graph.limits['post']
    require(graph.limits['pre'] == limits, 'evidence_schema_gap')
    require({r['region'] for r in limits['regions']} == set(caps), 'evidence_schema_gap')
    policy = payloads['proof.cohort_seed_policy_sha256']
    require(set(policy['required_regions']) == {b.region for b in inventory.boots if b.role == 'router'}, 'evidence_schema_gap')
    extra_fields: dict[str, Any] = {k: measured[k] for k in TOTALS}
    extra_fields.update({k: rational(measured[k]) for k in ALLOWANCES})
    return Budget(pre_input=pre_input, regional_caps=caps, fleet_cap=sum(caps.values()),
        publisher_cap=policy['maximum_publishers'], regional_reads={r: v['reads_per_second'] for r, v in pre_result['regional_budgets'].items()},
        regional_pending={r: v['pending_rows_per_second'] for r, v in pre_result['regional_budgets'].items()},
        fleet_reads=pre_result['approved_fleet_reads_per_second'],
        cpu_peak=max(v['cpu_peak_percent'] for v in regions.values()),
        cpu_increment=max(v['approved_incremental_cpu_percent'] for v in regions.values()),
        component_seconds={k: v / 1000000 for k, v in measured['component_us'].items()},
        rates=tuple(rates), pre_completed_us=before, post_completed_us=post['approval']['reviewed_at_us'],
        pre_digest=sha256(pre), post_pre_digest=post['pre_envelope_sha256'],
        post_workspace_count=post_input['workspace_count'], post_regional_caps=post_input['maximum_router_instances_by_region'],
        close_elapsed_us=max(s['final_pass_us'] for s in shutdown),
        handoff_elapsed_us=max(s['result_us'] + s['export_wait_us'] + s['preparation_us'] for s in shutdown),
        grace_us=min(s['grace_us'] for s in shutdown),
        load_segments=tuple(segments), peak_limits=dict(graph.limits),
        projected_load_segments=tuple(graph.detail(pre['v2_load']['segments'], 'segments')), **extra_fields)


def external_view(graph: PayloadGraph, value: dict[str, Any]) -> External:
    ports = graph.ports
    require(isinstance(ports, LoadingPorts) and ports.release is not None, 'artifact_gap')
    assert isinstance(ports, LoadingPorts) and ports.release is not None
    required = tuple(sorted(set(EXTERNAL_REQUIRED) | set(ports.release.required)))
    proofs = []
    for item in value['requirements']:
        raw = graph.file(item['evidence'])
        proof = graph.object(item['evidence'], a2.PROOF_RESULT)
        from trusted_router.async_settle_shadow_v2_gate_release import validate_proof
        validate_proof(graph, proof)
        proofs.append(ExternalProof(item['name'], item['router_revision'], item['enclave_revision'],
            raw, item['evidence']['sha256'], all(c['result'] == 'pass' for c in proof['checks']), item['stage']))
    require(set(required) <= {p.name for p in proofs}, 'artifact_gap')
    scoped = ports.release.required_stages
    require(not scoped or set(required) <= {name for names in scoped.values() for name in names}, 'artifact_gap')
    pilot_input = None
    pilot_result: dict[str, Any] = {}
    if value['pilot'] is not None:
        pilot_input, pilot_result = delegate(graph, value['pilot'])
    checks = pilot_result.get('checklist', {})
    return External(tuple(proofs), required, checks.get('measured_drain') == 'PASS',
        checks.get('signer_epoch') == checks.get('signer_public_key') == 'PASS',
        any(p.name == 'attestation' and p.passed for p in proofs),
        any(p.name == 'exposure_acceptance' and p.passed for p in proofs),
        pilot_input if value['pilot'] and value['pilot']['entry_point'].endswith('.post_flip') else None,
        ports.release.required_stages or {'shadow-serving': required})


def reset_view(graph: PayloadGraph, value: dict[str, Any], inventory: Inventory | None = None,
               rows: tuple[Row, ...] = ()) -> ResetSupport:
    payload, result = delegate(graph, value['retained'])
    require(inventory is not None, 'artifact_gap')
    assert inventory is not None
    serving = sorted([[b.start_us, b.end_us or graph.completed_at_us, b.revision] for b in inventory.boots if b.role == 'router'])
    manifests = {m['day']: {'router_revisions': m['revisions']['router']} for m in
        (a1.MANIFEST.parse(row.raw) for row in rows if row.key.kind == p1.KINDS[5])}
    require(sorted(payload['serving']) == serving and payload['manifests'] == manifests
            and sorted(payload['days']) == sorted(manifests), 'evidence_schema_gap')
    history = graph.detail(value['history'], 'reset_history')
    def identity(r: dict[str, Any]) -> tuple[Any, ...]:
        return r['at_us'], r['revision'], r['reason'], r.get('invalidated_at_us')
    require({identity(r) for r in history} ==
        {identity(r) for r in result['resets'] + result['report']['resets']}, 'evidence_schema_gap')
    facts: dict[tuple[Any, ...], dict[str, Any]] = {}
    for reset in result['resets'] + result['report']['resets']:
        facts.setdefault(identity(reset), {}).update(reset)
    originals = [canonical(v) for v in (payload, payload['proof'], payload['serving'], payload['manifests'], *payload['rows'])]
    for reset in history:
        expected = facts[identity(reset)]
        require(all(reset[k] == expected.get(k) for k in ('resolved_at_us', 'fixed_revision', 'invalidated_at_us')), 'evidence_schema_gap')
        support = graph.detail(reset['support'], 'files')
        require(bool(support), 'artifact_gap')
        require(any(canonical(retained_json(graph.file(ref))) in originals for ref in support), 'evidence_schema_gap')
    for resolution in payload['proof'].get('resolved_mismatches', []):
        require(any(a['subject_sha256'] == resolution['artifact_sha256'] for a in value['approvals']), 'artifact_gap')
    return ResetSupport(tuple(payload['rows']), payload['proof'], tuple(history),
        {reset['support']['root']: tuple(Preimage(ref['path'], graph.file(ref))
            for ref in graph.detail(reset['support'], 'files')) for reset in history})



def signature_views(graph: PayloadGraph, rows: tuple[Row, ...]) -> tuple[Any, ...]:
    """Ephemeral original tokens, with keys selected only from approved ports.

    Frozen Bundle transport has no signature field. This named companion file
    supplies material for the existing verifier; it creates no second verifier.
    """
    from trusted_router.async_settle_shadow_v2_gate_inputs import Signature
    path = 'loading/signatures.json'
    raw = graph.files.get(path)
    require(raw is not None, 'artifact_gap')
    assert raw is not None
    values = retained_json(raw)
    require(type(values) is list and len(values) <= 10000, 'evidence_schema_gap')
    ports = graph.ports
    require(ports is not None, 'artifact_gap')
    assert ports is not None
    result = []
    seen = set()
    for value in values:
        require(type(value) is dict and set(value) == {'object_id', 'verifier', 'token', 'now',
            'expected', 'attempt', 'server_attempt', 'claims'}, 'evidence_schema_gap')
        require(value['verifier'] in ('binding', 'certificate') and type(value['token']) is str
            and len(value['token']) <= 32768 and value['object_id'] not in seen, 'evidence_schema_gap')
        integer(value['now'])
        for field in ('expected', 'attempt', 'server_attempt', 'claims'):
            require(type(value[field]) is dict, 'evidence_schema_gap')
        approvals = [a for a in ports.approvals if a.entry_point.endswith('.verify_' + value['verifier'])]
        require(len(approvals) == 1, 'artifact_gap')
        result.append(Signature(value['object_id'], value['token'], approvals[0].keys, value['now'],
            value['expected'], value['attempt'], value['server_attempt'], value['claims']))
        seen.add(value['object_id'])
    expected = {r.key.id for r in rows if r.key.kind == p1.KINDS[0]}
    for row in rows:
        if row.key.kind == p1.KINDS[2]:
            page = a1.validate_row(row.key.kind, row.key.id, row.raw)
            expected.update(e['authorization_day'] + '/' + e['authorization_id'] for e in page['events']
                if e.get('proof_kind') == 'certificate')
    require(expected == seen, 'artifact_gap')
    return tuple(result)
