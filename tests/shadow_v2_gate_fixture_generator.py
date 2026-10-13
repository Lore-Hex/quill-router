"""Contract-constructed P5 evidence; keys/tokens are read only from release files."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from functools import cache
from pathlib import Path
from typing import Any

from scripts.async_settle import pilot_enablement as pilot
from scripts.async_settle import shadow_report as legacy
from trusted_router import async_settle_shadow_binding as binding
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_certificate as cert
from trusted_router import async_settle_shadow_v2_contract as contract
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    Boot,
    Budget,
    Bundle,
    Cohort,
    External,
    ExternalProof,
    Inventory,
    Key,
    Ports,
    Preimage,
    Rate,
    ResetSupport,
    Row,
    Signature,
    VerifierApproval,
)
from trusted_router.async_settle_shadow_v2_gate_load import COMPONENT_BOUNDS
from trusted_router.async_settle_shadow_v2_schema import day_at, sha256
from trusted_router.detached_jws import TrustedKey, b64decode, canonical

ROOT = Path(__file__).parent / 'fixtures/async_settlement'
START = 1791244800000000
DAY = 86400000000
TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a1')
REV = 'a' * 40
REGION = 'us-central1'


@cache
def _variants(file: str, a1_release: bool) -> Any:
    return json.loads((ROOT / ('shadow_v2_a1' if a1_release else 'shadow_v2') / (file + '.json')).read_bytes())['variants']


def fixture(file: str, *, a1_release: bool = True, name: str | None = None) -> dict[str, Any]:
    variants = _variants(file, a1_release)
    return copy.deepcopy(next(v['object'] for v in variants if name is None or v['name'] == name))


@cache
def ports() -> Ports:
    import inspect
    wire = json.loads((ROOT / 'shadow_v1.json').read_bytes())
    token = wire['billing_shadow_binding']
    claims = json.loads(b64decode(token.split('.')[1]))
    old = json.loads((ROOT / 'authorize_v1.json').read_bytes())
    header = json.loads(b64decode(token.split('.')[0]))
    binding_keys = (TrustedKey(header['kid'], binding.PURPOSE, old['public_key'], claims['iss'], binding.AUDIENCE),)
    vector = json.loads((ROOT / 'shadow_v2/exclusion_certificate.json').read_bytes())['variants'][0]
    approvals = []
    for fn, keys, file in ((cert.verify_certificate, (TrustedKey(**vector['key']),), 'shadow_v2/exclusion_certificate.json'),
                           (binding.verify_binding, binding_keys, 'shadow_v1.json')):
        approvals.append(VerifierApproval(fn.__module__ + '.' + fn.__name__, fn,
            Path(inspect.getfile(fn)).read_bytes(), (ROOT / file).read_bytes(), canonical([asdict(k) for k in keys]), keys,
            hashlib.sha256(Path(inspect.getfile(fn)).read_bytes()).hexdigest(), hashlib.sha256((ROOT / file).read_bytes()).hexdigest()))
    for fn in (pilot.fleet_budgets_pre, legacy.collect_rollbacks, legacy.report, pilot.post_flip):
        source = Path(inspect.getfile(fn)).read_bytes()
        release = (ROOT / 'shadow_v2_a2/manifest.json').read_bytes()
        approvals.append(VerifierApproval(fn.__module__ + '.' + fn.__name__, fn, source, release, b'[]', (),
            hashlib.sha256(source).hexdigest(), hashlib.sha256(release).hexdigest()))
    return Ports(cert.verify_certificate, cert.verify_original_receipt, binding.verify_binding,
        legacy.comparator_positive, legacy.positive_sample, legacy.known_exclusion,
        pilot.fleet_budgets_pre, legacy.collect_rollbacks, legacy.report, pilot.post_flip, tuple(approvals),
        {field: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for field, file in (
            ('proof.evaluator_fixture_sha256', 'billing_v1.json'), ('proof.binding_fixture_sha256', 'shadow_v1.json'),
            ('proof.wire_fixture_sha256', 'shadow_v2/manifest.json'))})


def pre_enable_bundle(tmp_path: Path) -> dict[str, Any]:
    """Small dormant v1 input; the actual retained function decides approval."""
    start, end = START, START + 60000000
    interval = dict(day=day_at(START), started_at_us=start, flushed_at_us=end)
    serving = [dict(instance='router', revision=REV, role='router', region=REGION,
        intervals=[interval], pins=dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
            TR_ASYNC_SETTLE_SHADOW_WORKSPACES='', TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false')),
        dict(instance='enclave', revision=REV, role='enclave', region=REGION,
            intervals=[interval], pins=dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='off'))]
    def artifact(name: str, value: Any) -> dict[str, str]:
        path = tmp_path / name
        raw = canonical(value)
        path.write_bytes(raw)
        return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    return dict(serving=serving, serving_instance_ids=['router', 'enclave'], rows=[],
        workspace_count=1, started_at_us=start, flushed_at_us=end,
        deployment=artifact('deployment.json', dict(serving=serving, maximum_router_instances_by_region={REGION: 1})),
        monitoring=artifact('monitoring.json', dict(started_at_us=start, flushed_at_us=end,
            approved_fleet_reads_per_second=10, regions={REGION: dict(cpu_peak_percent=20,
                approved_incremental_cpu_percent=5, approved_reads_per_second=10,
                read_headroom_per_second=10, approved_pending_rows_per_second=300, pending_row_headroom_per_second=700)})),
        read_health=artifact('read-health.json', dict(started_at_us=start, flushed_at_us=end, regions={REGION: [.01, .02]})))


def build(tmp_path: Path, *, seconds: int = 20, churn: bool = True, recovered_checkpoint: bool = False) -> Bundle:
    """Use seconds=604804 for a seven-day window with midnight/restart/idle days."""
    end = START + seconds * 1000000
    # Long-lived first boot crosses midnight; subsequent independent boots restart
    # mid-day. Most days have zero terminal traffic but complete timers/closure.
    splits = [START, end]
    if churn:
        splits = [START, START + (DAY + DAY // 2 if seconds > 172800 else 10000000), end]
    routers = tuple(Boot(f'boot{i}', 'router', REV, REGION, left, right, True, True)
        for i, (left, right) in enumerate(zip(splits, splits[1:], strict=False)))
    enclave = Boot('enclave', 'enclave', REV, REGION, START, end)
    inventory = Inventory((*routers, enclave))
    rows: list[Row] = []
    attachments: list[Row] = []
    ledgers = []
    previous = None
    for boot in inventory.boots:
        ledger = fixture('prerequisite_ledger')
        ledger.update(role=boot.role, boot=boot.boot, revision=boot.revision, region=boot.region,
            boot_at_us=boot.start_us, traffic_ready_at_us=boot.start_us,
            collection_interval=dict(start_us=boot.start_us, end_us=boot.end_us),
            previous_ledger_sha256=previous)
        for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
            ledger[field] = dict(sha256=sha256({'boot': boot.boot, 'artifact': field}), completed_at_us=boot.start_us)
        for field in ('policy_sha256', 'fixture_sha256', 'config_sha256'):
            ledger[field] = sha256({'field': field})
        ledgers.append(a1.PREREQUISITE_LEDGER.serialize(ledger))
        previous = sha256(ledger)
    ledger_root = sha256([hashlib.sha256(raw).hexdigest() for raw in ledgers])
    days = tuple(day_at(at * DAY) for at in range(START // DAY, (end - 1) // DAY + 1))
    # Retained binding is checked at original observation time, not report time.
    wire = json.loads((ROOT / 'shadow_v1.json').read_bytes())
    token = wire['billing_shadow_binding']
    claims = json.loads(b64decode(token.split('.')[1]))
    old = json.loads((ROOT / 'authorize_v1.json').read_bytes())
    header = json.loads(b64decode(token.split('.')[0]))
    key = TrustedKey(header['kid'], binding.PURPOSE, old['public_key'], claims['iss'], binding.AUDIENCE)
    sample = fixture('sample', a1_release=False)
    sample.update(authorization_day=days[0], observed_at_us=START + 2000000,
        authorize_at_us=START + 1000000, booking_observed_at_us=START + 2000000,
        original_terminal_receipt=dict(day=days[0], boot='boot0', event_seq=f'{2:020}'),
        health_slot=(START + 2000000) // 2000000)
    sample['deployment'].update(instance='boot0', region=REGION)
    sample_key = days[0] + '/' + sample['authorization_id']
    rows.append(Row(Key(p1.KINDS[0], sample_key), p1.SAMPLE.serialize(sample)))
    body = fixture('health')
    ns = (START + 2000000) * 1000
    body.update(publisher_boot_id='boot0', slot=ns // 2000000000, observed_at_ns=ns,
                publisher_heartbeat_ns=ns, backlog_age_at_seconds=ns // 1000000000)
    basis = fixture('health_basis')
    basis.update(reader_boot='boot0', body=body, committed_at_ns=ns + 100000000,
                 read_at_ns=ns + 200000000, read_elapsed_ns=100000000)
    basis_batch = dict(**TAG, day=days[0], boot='boot0', sequence=f'{1:020}', previous_sha256=None, bases=[basis])
    attachments.append(Row(Key('health_basis', f'health-basis-batches/{days[0]}/boot0/{1:020}'), health.HEALTH_BASIS_BATCH.serialize(basis_batch)))
    auth = fixture('receipt_pages', a1_release=False)['events'][0]
    facts = copy.deepcopy(auth['facts'])
    facts['receipt_requested'] = False
    candidates = [dict(endpoint_id=sample['endpoint_id'], adapter='openai', features=[])]
    decision = contract.decide(contract.RequestedFacts.from_object(facts), None, canonical(candidates), complete_candidates=True)
    auth.update(event_at_us=START + 1000000, receipt_at_us=START + 1000000, authorization_day=days[0],
        receipt_locator=dict(day=days[0], boot='boot0', first_seq=f'{1:020}', event_seq=f'{1:020}'),
        facts=facts, candidates=candidates, eligibility='eligible',
        dimensions=dict(adapter='openai', route_type='chat.completions', streamed=False),
        decision=dict(reason=decision.reason, facts_sha256=decision.facts_sha256, candidate_set_sha256=decision.candidate_set_sha256))
    terminal = fixture('receipt_pages', a1_release=False, name='terminal-event')
    terminal.update(seq=f'{2:020}', event_at_us=START + 2000000, receipt_at_us=START + 2000000,
        authorization_day=days[0], decision=auth['decision'], eligibility='eligible', dimensions=auth['dimensions'],
        original_receipt=None, certificate_sha256=None, payload_sha256=sample['payload_hash'], proof_kind='compared', partition='A', comparison='exact',
        prediction=dict(prediction='yes', reason='eligible', health_source='shadow_observation', health_slot=body['slot'],
                        prediction_basis='counterfactual_backlog_v2', basis_sha256=sha256(basis)))
    page = dict(v=2, policy_version='shadow-v2', day=days[0], boot='boot0', first_seq=f'{1:020}', last_seq=f'{2:020}', previous_page_sha256=None, events=[auth, terminal])
    rows.append(Row(Key(p1.KINDS[2], f'{days[0]}/boot0/{1:020}'), p1.PAGE.serialize(page)))
    obligation = joins.authorization_obligation(page, auth['seq'])
    locator = joins.event_locator(page, terminal['seq'])
    joins.validate_terminal_join(obligation, page, page, locator)
    obligation.update(status='terminal_reconciled', terminal_reference=locator)
    op = dict(**TAG, day=days[0], boot='boot0', page_seq=f'{1:020}', previous_sha256=None, entries=[obligation])
    attachments.append(Row(Key('authorization_obligation', f'obligations/{days[0]}/boot0/{1:020}'), joins.AUTHORIZATION_OBLIGATION_PAGE.serialize(op)))
    rate_segments = []
    for boot in routers:
        assert boot.end_us is not None
        for day in days:
            day_start = next(at * DAY for at in range(START // DAY, end // DAY + 1) if day_at(at * DAY) == day)
            left, right = max(day_start, boot.start_us), min(day_start + DAY, boot.end_us)
            if left >= right:
                continue
            duration = (right - left) // 1000000
            counter = fixture('counter')
            counter.update(day=day, boot=boot.boot, started_at_us=left, flushed_at_us=right)
            counter['admission_observer'].update(workspace_reads=max(1, duration // 4), health_reads=max(1, duration))
            owns = boot.boot == 'boot0' and day == days[0]
            if owns:
                if recovered_checkpoint:
                    counter.update(checkpoint_attempts=2, checkpoint_failures=1, checkpoint_recoveries=1)
                counter.update(authorize_attempts=1, authorize_fresh=1, settle_attempts=1, A=1,
                    proof_compared=1, requested_eligible=2, eligible_demand=1, event_seq=2, durable_event_hwm=2,
                    receipt_count=2, receipt_chain_sha256=sha256(page), comparison_attempts=1, samples_inserted=1)
                counter['admission_observer']['prediction_yes'] = 1
                count = next(b for b in counter['counts'] if (b['adapter'], b['route_type'], b['streamed']) == ('openai', 'chat.completions', False))
                count.update(authorize_attempts=1, authorize_fresh=1, requested_eligible=1, snapshot_sent=1,
                    settle_attempts=1, envelope_present=1, observed_attempts=1, observed_eligible=1, evaluable=1, exact=1)
                for phase in ('authorize', 'settle'):
                    bucket = dict(phase=phase, dimensions=auth['dimensions'], **dict.fromkeys(p1.COUNTS, 0), eligible_demand=int(phase == 'settle'))
                    if phase == 'authorize':
                        bucket.update(authorize_attempts=1, authorize_fresh=1, requested_eligible=1)
                    else:
                        bucket.update(settle_attempts=1, A=1, proof_compared=1, requested_eligible=1)
                    counter['buckets'].append(bucket)
            first, last = (left + 1999999) // 2000000, (right - 1) // 2000000
            previous_trace = None
            trace_template = fixture('publisher_slot_trace')
            for offset in range(first, last + 1, 60):
                entries = []
                for slot in range(offset, min(last + 1, offset + 60)):
                    trace = trace_template.copy()
                    stamp = slot * 2000000
                    trace.update(day=day, boot=boot.boot, slot=slot, claim_start_us=stamp,
                        claim_at_ns=stamp * 1000, observed_at_ns=stamp * 1000, publisher_heartbeat_ns=stamp * 1000,
                        ack_at_us=stamp + 400000, body_sha256=sha256(body) if slot == body['slot'] else sha256({'slot': slot, 'boot': boot.boot}))
                    entries.append(trace)
                tp = dict(**TAG, day=day, boot=boot.boot, first_slot=offset, last_slot=entries[-1]['slot'], previous_sha256=previous_trace, entries=entries)
                attachments.append(Row(Key('publisher_trace', f'publisher-traces/{day}/{boot.boot}/{offset}'), health.PUBLISHER_TRACE_PAGE.serialize(tp)))
                previous_trace = sha256(tp)
            count_slots = last - first + 1
            counter['publisher'].update(scheduled_slots=count_slots, claim_attempts=count_slots,
                                       claims=count_slots, scans=count_slots, publications=count_slots)
            counter['publisher']['cycle_us']['counts'] = [0] * 9
            counter['publisher']['cycle_us']['counts'][sum(trace_template['cycle_us'] > edge for edge in counter['publisher']['cycle_us']['bounds_us'])] = count_slots
            counter['publisher']['rpc_us']['counts'] = [0] * 9
            counter['publisher']['rpc_us']['counts'][0] = count_slots * trace_template['rpc_count']
            raw_counter = a1.COUNTER.serialize(counter)
            rows.append(Row(Key(p1.KINDS[3], day + '/' + boot.boot), raw_counter))
            closure = fixture('closure')
            closure.update(day=day, boot=boot.boot, interval_end_us=right, observer_fold_through_us=right,
                final_event_seq=counter['event_seq'], durable_event_hwm=counter['event_seq'], counter_sha256=sha256(counter),
                receipt_chain_sha256=sha256(page) if owns else sha256([]), reason='shutdown' if right == boot.end_us else 'utc_boundary')
            rows.append(Row(Key(p1.KINDS[4], day + '/' + boot.boot), a1.CLOSURE.serialize(closure)))
            result = fixture('close_result')
            result.update(day=day, boot=boot.boot, closure_sha256=sha256(closure), attempt_started_at_us=right,
                          observed_at_us=right + 1000000, physical_commit_ack_at_us=right + 1000000)
            a1.sealed_closure(result, closure, counter, lifecycle={boot.boot: dict(revision=boot.revision, region=boot.region,
                lifetime=dict(start_us=boot.start_us, end_us=boot.end_us))}, termination_applies=True)
            rows.append(Row(Key(a1.KINDS[-1], day + '/' + boot.boot), a1.CLOSE_RESULT.serialize(result)))
            handoff = fixture('handoff_accounting', name='landed-True')
            handoff.update(day=day, boot=boot.boot, result_sha256=sha256(result))
            attachments.append(Row(Key('handoff_accounting', f'handoffs/{day}/{boot.boot}'), a1.HANDOFF_ACCOUNTING.serialize(handoff)))
            a2tag = dict(v=2, policy_version='shadow-v2', contract_revision='a2')
            result_raw = a1.CLOSE_RESULT.serialize(result)
            close_ref = dict(path='rows/close/' + day + '/' + boot.boot, sha256=hashlib.sha256(result_raw).hexdigest(), bytes=len(result_raw))
            measurement = dict(**a2tag, day=day, boot=boot.boot, grace_us=4000000,
                final_pass_us=2000000, close_us=1000000, result_us=1000000, export_wait_us=0, preparation_us=0,
                outcome='graceful', source=close_ref)
            measured_raw = a2.SHUTDOWN_MEASUREMENT.serialize(measurement)
            measured_key = 'measurements/' + day + '/' + boot.boot
            attachments.append(Row(Key('shutdown_measurement', measured_key), measured_raw))
            entry = dict(day=day, boot=boot.boot, termination_applies=right == boot.end_us,
                reason=closure['reason'], interval_end_us=right, grace_us=4000000,
                original_close=dict(path='rows/closure/' + day + '/' + boot.boot,
                    sha256=hashlib.sha256(a1.CLOSURE.serialize(closure)).hexdigest(), bytes=len(a1.CLOSURE.serialize(closure))),
                close_result=close_ref, measurement=dict(path=measured_key, sha256=hashlib.sha256(measured_raw).hexdigest(), bytes=len(measured_raw)))
            attachments.append(Row(Key('shutdown_entry', 'shutdown/' + day + '/' + boot.boot), a2.SHUTDOWN_ENTRY.serialize(entry)))
            joins.obligation_projection(closure, [obligation] if owns else [])
            rate_segments.append(Rate(boot.boot, left, right, max(1, duration // 4), max(1, duration)))
    preimages = []
    proof = fixture('proof_manifest')
    proof.update(rosters_by_day={}, instance_regions_by_day={}, ledger_root=ledger_root, attachments=[], explicit_keys=[])
    for field in proof:
        if field.endswith('_sha256') or field.endswith('_root'):
            if field != 'ledger_root':
                payload = canonical({'field': field, 'days': list(days)})
                proof[field] = hashlib.sha256(payload).hexdigest()
                preimages.append(Preimage('proof.' + field, payload))
    manifests = []
    for day in days:
        manifest = fixture('controls')
        roster: dict[str, dict[str, Any]] = {'router': {}, 'enclave': {}}
        day_start = next(at * DAY for at in range(START // DAY, end // DAY + 1) if day_at(at * DAY) == day)
        for boot in inventory.boots:
            assert boot.end_us is not None
            left, right = max(day_start, boot.start_us), min(day_start + DAY, boot.end_us)
            if left < right:
                roster[boot.role][boot.boot] = dict(revision=boot.revision, region=boot.region, lifetime=dict(start_us=left, end_us=right))
        manifest.update(day=day, router_writers=roster['router'], enclave_writers=roster['enclave'],
            revisions=dict(router=[REV], enclave=[REV]), prerequisite_ledger_root=ledger_root,
            disabled_flag_intervals=[dict(interval=dict(start_us=max(day_start, START), end_us=min(day_start + DAY, end)),
                                          admission=False, protection=False, negotiation=False, fast_drain=False)])
        for stream in a1.COVERAGE_STREAMS:
            candidates_rows = [r for r in (*rows, *attachments) if day in r.key.id and r.key.kind == (a1.KINDS[-1] if stream == 'close_result' else stream)]
            entries = sorted([dict(key=r.key.id, sha256=hashlib.sha256(r.raw).hexdigest(), bytes=len(r.raw), state='present') for r in candidates_rows], key=lambda e: e['key'])
            cps = []
            for index in range(0, max(1, len(entries)), 1000):
                cp = dict(**TAG, day=day, stream=stream, page_seq=f'{index // 1000 + 1:020}', previous_sha256=sha256(cps[-1]) if cps else None, entries=entries[index:index + 1000])
                attachments.append(Row(Key('coverage', f'coverage/{day}/{stream}/{index // 1000 + 1:020}'), a1.COVERAGE_PAGE.serialize(cp)))
                cps.append(cp)
            root = a1.coverage_root(cps)
            if stream + '_root' in manifest:
                manifest[stream + '_root'] = root
        for field, kind in (('counter_root', p1.KINDS[3]), ('closure_root', p1.KINDS[4]), ('receipts_root', p1.KINDS[2])):
            manifest[field] = sha256([hashlib.sha256(r.raw).hexdigest() for r in rows if r.key.kind == kind and r.key.id.startswith(day + '/')])
        proof['rosters_by_day'][day] = roster
        proof['instance_regions_by_day'][day] = {k: v['region'] for k, v in roster['router'].items()}
        manifests.append(manifest)
    preimages.append(Preimage('proof.ledger_root', canonical([hashlib.sha256(raw).hexdigest() for raw in ledgers])))
    proof['attachments'] = [dict(sha256=hashlib.sha256(r.raw).hexdigest(), bytes=len(r.raw)) for r in attachments if r.key.kind == 'coverage']
    proof['explicit_keys'] = sorted({r.key.id for r in rows})
    proof_raw = a1.PROOF_MANIFEST.serialize(proof)
    for manifest in manifests:
        manifest['proof_manifest_sha256'] = sha256(proof)
        rows.append(Row(Key(p1.KINDS[5], manifest['day'] + '/manifest-v2'), a1.MANIFEST.serialize(manifest)))
    pre_input = pre_enable_bundle(tmp_path)
    monitoring_path = Path(pre_input['monitoring']['path'])
    monitoring = json.loads(monitoring_path.read_bytes())
    monitoring['approved_fleet_reads_per_second'] = 10
    monitoring['regions'][REGION].update(approved_reads_per_second=10, read_headroom_per_second=10)
    monitoring_path.write_bytes(canonical(monitoring))
    pre_input['monitoring']['sha256'] = hashlib.sha256(monitoring_path.read_bytes()).hexdigest()
    pre_result = pilot.fleet_budgets_pre(pre_input)
    caps = pre_result['maximum_router_instances_by_region']
    budget = Budget(pre_input, caps, sum(caps.values()), 0, 1,
        {REGION: 10.}, {REGION: 300.}, 10., 2., 1., 20., 1., COMPONENT_BOUNDS.copy(),
        tuple(rate_segments), START - 1, START, sha256(pre_result), sha256(pre_result), 1, caps,
        100000, 1000000000, 100, 1000, 1000000, 1000000, 4000000,
        publisher_unresolved_rows_per_second=5000.5, publisher_control_rows_per_second=1.5,
        publisher_transactions_per_second=1., publisher_rpc_per_second=3., metadata_bytes_per_second=20.,
        export_passes=1, export_root_writes=1, export_write_bytes=262144)
    def gate(name: str, at: int) -> bytes:
        value = fixture('gate_outputs')
        value.update(gate=name, verdict='PASS', gaps=[], completed_at_us=at, proof_manifest_sha256=sha256(proof), prerequisite_ledger_root=ledger_root)
        return a1.GATE_OUTPUT.serialize(value)
    evidence = canonical({'fixture': 'offline external proof'})
    external = External((ExternalProof('fixture-ci', REV, REV, evidence, hashlib.sha256(evidence).hexdigest(), True),),
                        ('fixture-ci',), True, True, True, True)
    # Undefined payloads are supplied directly as typed inputs. Their original
    # bytes are retained for hash checking, without defining a production codec.
    material = {
        'shutdown_inventory_sha256': canonical([asdict(b) for b in inventory.boots]),
        'cohort_seed_policy_sha256': canonical(asdict(Cohort(1, True, REGION))),
        'pre_budget_sha256': canonical(pre_result),
        'post_budget_sha256': canonical(asdict(budget)),
        'external_proofs_sha256': evidence,
        'resolution_dag_sha256': canonical([]),
        'evaluator_fixture_sha256': (ROOT / 'shadow_v1.json').read_bytes(),
        'binding_fixture_sha256': (ROOT / 'shadow_v1.json').read_bytes(),
        'wire_fixture_sha256': (ROOT / 'shadow_v2_a1/manifest.json').read_bytes(),
        'ledger_root': canonical([hashlib.sha256(raw).hexdigest() for raw in ledgers]),
    }
    for field in proof:
        if field.endswith(('_root', '_sha256')) and field not in material:
            stream = field.removesuffix('_root').removesuffix('_sha256').replace('publisher_slot_trace', 'publisher_trace')
            material[field] = canonical([hashlib.sha256(r.raw).hexdigest() for r in attachments
                if r.key.kind == 'coverage' and json.loads(r.raw)['stream'] == stream])
    preimages = [Preimage('proof.' + field, raw) for field, raw in material.items()]
    for field, raw in material.items():
        proof[field] = hashlib.sha256(raw).hexdigest()
    proof_raw = a1.PROOF_MANIFEST.serialize(proof)
    for index, row in enumerate(rows):
        if row.key.kind == p1.KINDS[5]:
            manifest = json.loads(row.raw)
            manifest['proof_manifest_sha256'] = sha256(proof)
            rows[index] = Row(row.key, a1.MANIFEST.serialize(manifest))
    from tests.shadow_v2_gate_a2_fixture import seal
    return seal(Bundle(tuple(rows), proof_raw, tuple(ledgers), tuple(attachments), inventory, Cohort(1, True, REGION), budget,
        external, ResetSupport((), {}), (Signature(sample_key, token, (key,), START // 1000000 + 2),),
        gate('transport', START), gate('fleet-budgets', START), gate('fleet-budgets-pre', START - 1),
        gate('shadow-serving', end), end, days, preimages=tuple(preimages)))


def change_row(bundle: Bundle, kind: str, change: Any, index: int = 0) -> Bundle:
    rows = list(bundle.rows)
    positions = [i for i, row in enumerate(rows) if row.key.kind == kind]
    pos = positions[index]
    row = rows[pos]
    value = json.loads(row.raw)
    change(value)
    rows[pos] = Row(row.key, canonical(value))
    return replace(bundle, rows=tuple(rows))
