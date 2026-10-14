"""One source-derived day graph over captured producer results."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_coverage import (
    ExactPages,
    coverage,
    handoff,
    obligations,
    populations,
)
from trusted_router.async_settle_shadow_v2_pack_daypack import assemble_day
from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
from trusted_router.async_settle_shadow_v2_pack_readers import native_reader_index
from trusted_router.async_settle_shadow_v2_pack_references import TerminalReconciliation
from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def compose_day(files: DayFiles, *, day: str, producers: dict[str, Any],
                fixtures: dict[str, dict[str, Any]], cohort: dict[str, Any],
                source_shas: dict[str, str], complete_through_us: int) -> dict[str, Any]:
    """Producer outputs select all roots; caller-supplied assembly is not read."""
    for name in PRODUCERS:
        require(name in producers and bool(producers[name]), 'MissingProducer:' + name)
    native = producers['NativeExport']
    complete_through_us = min(complete_through_us, native['completed_at_us'])
    inventory = producers['Inventory']
    ready = producers['Readiness']
    shutdown = producers['Shutdown']
    budget = producers['Budget']
    for name, result in (('Inventory', inventory), ('Readiness', ready), ('Shutdown', shutdown)):
        require(result.get('complete') is True, 'MissingProducer:' + name)
    rows = native['rows']
    attachments = native.get('attachments', [])
    data = defaultdict(list)
    by_kind = defaultdict(list)
    for row in rows + attachments:
        raw = row['raw'].encode()
        value = retained_json(raw)
        by_kind[row['kind']].append((row['id'], value, raw))
        data[row['id']].append(raw)
    receipts = [v for _, v, _ in by_kind[p1.KINDS[2]]]
    samples = [v for _, v, _ in by_kind[p1.KINDS[0]]]
    terminal = TerminalReconciliation(receipts, samples)
    batches = [(key, v) for key, v, _ in by_kind['health_basis']]
    # Export attachment kind names retain the native object spelling.
    if not batches:
        batches = [(row['id'], retained_json(row['raw'].encode())) for row in attachments
                   if row['id'].startswith('health-basis-batches/')]
    reader_records = {}
    for ref in ready['proofs']:
        check = a2.PROOF_RESULT.parse(files.read(ref))
        if check['criterion'] == 'observer_freshness':
            for result in check['checks']:
                original = result['evidence']
                reader_records[original['path']] = original
    require(bool(reader_records), 'health_source_gap')
    basis = native_reader_index(files, records=reader_records.values(), receipts=receipts,
                               samples=samples, batches=batches)
    require(not basis.missing, 'health_source_gap')
    obligation_keys = []
    horizon_ends: list[int] = []
    missing = []
    for _key, closure, _ in by_kind[p1.KINDS[4]]:
        pages = [page for page in receipts if page['day'] == day and page['boot'] == closure['boot']]
        made, _, gaps = obligations(files, closure, pages, original=terminal.original,
            terminal=terminal.terminal, complete_through_us=complete_through_us)
        missing.extend(gaps)
        for page in made:
            name = day + '/' + closure['boot'] + '/' + page['page_seq']
            obligation_keys.append(name)
            data['authorization_obligation/' + name].append(canonical(page))
            horizon_ends.extend(e['horizon_at_us'] for e in page['entries'])
    for boot in inventory['boots']:
        if boot['role'] != 'router':
            continue
        key = day + '/' + boot['boot']
        result = [v for row_key, v, _ in by_kind[a1.KINDS[-1]] if row_key == key]
        data['handoff_accounting/' + key].append(canonical(handoff(day, boot['boot'], result[0] if len(result) == 1 else None)))
    for key, _, raw in by_kind[p1.KINDS[3]]:
        data['counter/' + key].append(raw)
    for key, _, raw in by_kind[a1.KINDS[-1]]:
        data['close_result/' + key].append(raw)
    for row in attachments:
        prefix = 'health_basis/' if row['id'].startswith('health-basis-batches/') else 'publisher_trace/'
        data[prefix + row['id']].append(row['raw'].encode())
    source = ExactPages(lambda key: tuple(data.get(key, [])))
    population = populations(source, day=day, boots=tuple(inventory['boots']),
        contender_segments=budget['segments'], basis_keys=list(basis.keys), obligation_keys=obligation_keys)
    covered = coverage(files, source, day=day, expected=population.expected)
    rosters: dict[str, dict[str, Any]] = {'router': {}, 'enclave': {}}
    for boot in inventory['boots']:
        rosters[boot['role']][boot['boot']] = dict(revision=boot['revision'], region=boot['region'],
            lifetime=dict(start_us=boot['start_us'], end_us=boot['end_us']))
    manifest: dict[str, Any] = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=day,
        router_writers=rosters['router'], enclave_writers=rosters['enclave'],
        revisions={role: sorted({b['revision'] for b in roster.values()}) for role, roster in rosters.items()},
        first_evidence_at_us=min((e['event_at_us'] for p in receipts for e in p['events']), default=None),
        disabled_flag_intervals=[], gaps=[], complete=False)
    proof = dict(v=2, policy_version='shadow-v2', contract_revision='a1', source_shas=source_shas,
        rosters_by_day={day: rosters}, instance_regions_by_day={day: {b['boot']: b['region'] for b in inventory['boots']}},
        attachments=[], explicit_keys=[], complete=False)
    preimages: dict[str, Any] = {}
    for qualified, kind in (('manifest.counter_root', p1.KINDS[3]), ('manifest.closure_root', p1.KINDS[4]),
                            ('manifest.receipts_root', p1.KINDS[2])):
        preimages[qualified] = [dict(key=a2.L(*key.split('/')), bodies=[raw]) for key, _, raw in by_kind[kind]]
    ledger_entries = []
    for ref in ready['ledgers']:
        raw = files.read(ref)
        ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        key = a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
        ledger_entries.append(dict(key=key, bodies=[raw]))
    preimages['manifest.prerequisite_ledger_root'] = ledger_entries
    preimages['proof.ledger_root'] = ledger_entries
    preimages['proof.publisher_slot_trace_sha256'] = [dict(
        key=a2.L(v['day'], v['boot'], str(v['first_slot'])), bodies=[row['raw'].encode()])
        for row in attachments if row['id'].startswith('publisher-traces/')
        for v in [retained_json(row['raw'].encode())]]
    for qualified, stream in contract.COVERAGE_PREIMAGES.items():
        preimages[qualified] = covered[stream]
    for qualified, stream in contract.CROSS_DAY_PREIMAGES.items():
        preimages[qualified] = [dict(day=day, pages=covered[stream])]
    aliases = {**fixtures, 'manifest.independent_inventory_sha256': inventory['envelope'],
        'proof.shutdown_inventory_sha256': shutdown['envelope'], 'proof.cohort_seed_policy_sha256': cohort,
        'proof.pre_budget_sha256': budget['pre'], 'proof.post_budget_sha256': budget['post'],
        'proof.external_proofs_sha256': producers['ExternalPack'],
        'proof.resolution_dag_sha256': producers['History']}
    for path, ref in aliases.items():
        files.put(path, files.original(ref))
    # This is the frozen whole-day call. Its dict is released on return.
    artifacts = files.originals()
    manifest, proof = assemble_day(files, manifest, proof, artifacts=artifacts, preimages=preimages,
        expected_closed={name: not missing and not population.missing and not population.tails for name in PRODUCERS},
        horizon_ends=horizon_ends, complete_through_us=complete_through_us)
    del artifacts, preimages
    data.clear()
    subject = files.obj('days/' + day + '/subject.json', dict(day=day,
        manifest=files.refs[day + '/manifest-v2.json'], producers={name: producers[name] for name in ('ExternalPack', 'History')}, coverage=covered))
    transport = {**native, 'proof': a1.PROOF_MANIFEST.serialize(proof).decode(),
        'ledgers': [files.read(ref).decode() for ref in ready['ledgers']],
        'rows': [*rows, dict(kind=p1.KINDS[5], id=day + '/manifest-v2', raw=a1.MANIFEST.serialize(manifest).decode())]}
    return dict(subject=subject, transport=transport, manifest=manifest)
