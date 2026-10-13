"""Six A3 boot prerequisites from pinned CI/trials and retained startup checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_collect_inventory import InventoryResult
from trusted_router.async_settle_shadow_v2_collect_ports import (
    CHECKS,
    SOURCES,
    TAGS,
    CIPort,
    LifecyclePort,
    Pass,
    Query,
    classified_records,
    fields,
    identifier,
    integer,
)
from trusted_router.async_settle_shadow_v2_gate_inputs import PREREQUISITE_CONTEXT
from trusted_router.async_settle_shadow_v2_schema import decode, require


@dataclass(frozen=True)
class ReadinessResult:
    ledgers: tuple[dict[str, Any], ...]
    proofs: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    complete: bool



def local_measurements(run: Pass, boot: dict[str, Any], criterion: str,
                       record: dict[str, Any], pins: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the frozen freshness predicate; no borrowed observer or empty role PASS."""
    refs: list[dict[str, Any]] = []
    config = decode(run.files.read(pins['config']), 262144, retained_strings=True)
    require(type(config['descriptor_ids']) is list and 0 < len(config['descriptor_ids']) <= 16)
    require(len(config['descriptor_ids']) == len(set(config['descriptor_ids'])))
    for descriptor_id in config['descriptor_ids']:
        identifier(descriptor_id)
    if criterion in ('boot_transport', 'observer_freshness'):
        basis = record['clock_basis']
        require(basis['boot_at_us'] == boot['boot_at_us'])
        clock = decode(run.files.read(basis['original']), 262144, retained_strings=True)
        require(clock == {k: basis[k] for k in ('clock', 'boot', 'boot_at_us')})
        refs.append(basis['original'])
    def during(at_ns: int) -> None:
        integer(at_ns)
        require(boot['boot_at_us'] * 1000 <= at_ns <= record['completed_at_us'] * 1000)

    if criterion == 'boot_transport':
        reads = record['dependencies']
        require(type(reads) is list and 0 < len(reads) <= 16)
        require({r['operation'] for r in reads} >= {'transport', 'read'})
        expected = config['dependencies'][boot['role']]
        targets = [(r['operation'], r['resource'], r['region']) for r in reads]
        require(len(targets) == len(set(targets)))
        require(set(targets) == {(r['operation'], r['resource'], r['region']) for r in expected})
        for read in reads:
            during(read['observed_at_us'] * 1000)
            during((read['observed_at_us'] + read['elapsed_us']) * 1000)
            require(read['boot'] == boot['boot'] and read['region'] == boot['region'])
            require(read['config_sha256'] == pins['config']['sha256'])
            require(read['attempts'] == 1 and read['marker_matches'] is True)
            require(type(read['elapsed_us']) is int and 0 <= read['elapsed_us'] <= read['deadline_us'] <= 10_000_000)
            require(bool(read['resource']))
    elif criterion == 'observer_freshness':
        if boot['role'] == 'router':
            ref = record['health_basis']
            basis = health.HEALTH_BASIS.parse(run.files.read(ref))
            require(basis['reader_boot'] == boot['boot'] and basis['source_state'] == 'installed')
            during(basis['read_at_ns'])
            during(record['installed_at_ns'])
            require(basis['read_at_ns'] + basis['read_elapsed_ns'] <= record['installed_at_ns'])
            require(0 <= record['install_elapsed_us'] <= 250_000)
            require(record['installed_at_ns'] - basis['read_at_ns'] == record['install_elapsed_us'] * 1000)
            require(type(record['contender']) is bool)
            refs.append(ref)
            if record['contender']:
                claim_ref, body_ref = record['claim'], record['publication']
                claim = health.HEALTH_CLAIM.parse(run.files.read(claim_ref))
                body = health.HEALTH.parse(run.files.read(body_ref))
                health.validate_publication(claim, body)
                require(claim['owner_boot_id'] == boot['boot'])
                during(record['claim_at_ns'])
                during(record['published_at_ns'])
                require(record['claim_at_ns'] == claim['claim_at_ns'])
                require(record['published_at_ns'] == body['observed_at_ns'])
                require(record['claim_at_ns'] <= record['published_at_ns'] <= record['installed_at_ns'])
                require(0 <= record['publication_elapsed_us'] <= 500_000)
                require(record['published_at_ns'] - record['claim_at_ns'] == record['publication_elapsed_us'] * 1000)
                require(body == basis['body'])
                refs.extend((claim_ref, body_ref))
        else:
            path = record['router_path']
            require(path['ready'] is True and bool(path['resource']) and bool(path['router_boot']))
            require(path['region'] == record['configured_router_region'])
            require({k: path[k] for k in ('resource', 'region')} == config['router_path'])
            during(path['observed_at_us'] * 1000)
            require(record['local_transport_ready'] is True and record['echo_ready'] is True)
            require('health_basis' not in record)
    elif criterion == 'marker_keys':
        require(record['effective_pins'] == boot['pins'])
        require(all(record[k + '_sha256'] == pins[k]['sha256'] for k in ('policy', 'fixture', 'config')))
        require(record['source_revision'] == boot['revision'])
        require(type(record['authority_epoch']) is int and record['authority_epoch'] > 0)
        descriptor = decode(run.files.read(record['public_descriptors']), 16384, retained_strings=True)
        fields(descriptor, 'authority_epoch config_sha256 source_revision descriptor_ids')
        require(descriptor == dict(authority_epoch=record['authority_epoch'], config_sha256=pins['config']['sha256'],
            source_revision=boot['revision'], descriptor_ids=config['descriptor_ids']))
        refs.append(record['public_descriptors'])
        if boot['role'] == 'enclave':
            image = decode(run.files.read(record['attested_image']), 16384, retained_strings=True)
            fields(image, 'revision image_sha256')
            require(image == dict(revision=boot['revision'], image_sha256=config['enclave_image_sha256']))
            require(record['attested_revision'] == boot['revision'])
            refs.append(record['attested_image'])
    return refs


def collect(run: Pass, *, inventory: InventoryResult, ci: CIPort, logs: LifecyclePort,
            artifact_query: Query, startup_query: Query, pins: dict[str, Any],
            previous: dict[str, dict[str, Any]] | None = None) -> ReadinessResult:
    """Preserve original trial time; boot adoption time comes only from this boot."""
    require(artifact_query.page_size <= 100)
    artifacts = list(run.scan(ci, artifact_query))
    startup = list(run.scan(logs, startup_query))
    attempts = tuple(r for r in startup if r.get('kind') == 'a3_opted_attempt')
    attempt_ref = run.files.obj('readiness/attempts.json', list(attempts))
    startup = classified_records(run, startup, 'readiness')
    for attempt in attempts:
        try:
            require(attempt in startup)
            joined = [b for b in inventory.boots if b['boot'] == attempt['boot']
                      and b['start_us'] <= attempt['at_us'] < b['end_us']]
            require(len(joined) == 1)
            receipt = attempt['receipt']
            require(receipt['boot'] == attempt['boot'] and receipt['day'] == startup_query.day)
            page = p1.PAGE.parse(run.files.read(receipt['original']))
            require(page['boot'] == receipt['boot'])
            require(page['day'] == receipt['day'])
            selected = [event for event in page['events'] if int(event['seq']) == receipt['sequence']]
            require(len(selected) == 1)
            event = selected[0]
            require(event['receipt_locator'] is not None)
            require(event['event_at_us'] == attempt['at_us'])
        except (KeyError, ValueError, TypeError, OSError):
            run.gap('readiness', 'attempt_without_inventory_interval', 'boot_prerequisite_gap')
    if not inventory.complete:
        run.gap('inventory', 'independent_inventory_incomplete', 'boot_prerequisite_gap')
    for key in ('policy', 'fixture', 'config'):
        try:
            run.files.read(pins[key])
        except (OSError, ValueError, KeyError):
            run.gap(key, 'release_pin_missing', 'boot_prerequisite_gap')
    if run.missing:
        run.files.obj('readiness/status.json', dict(complete=False, missing=run.missing, attempts=attempt_ref,
                                                  originals=run.sources))
        return ReadinessResult((), (), attempts, False)
    ledgers, proofs = [], []
    for boot in inventory.boots:
        descriptors = {}
        completions = []
        prefix = f'readiness/{boot["boot"]}'
        for criterion, checks in CHECKS.items():
            records = [r for r in startup if r.get('kind') == 'a3_boot_check'
                       and r.get('boot') == boot['boot'] and r.get('criterion') == criterion]
            try:
                require(len(records) == 1)
                record = records[0]
                require(all(record[k] == boot[k] for k in ('boot', 'role', 'revision', 'region', 'boot_at_us')))
                require(record['config_sha256'] == pins['config']['sha256'])
                require(boot['boot_at_us'] <= record['completed_at_us'])
                require(record['completed_at_us'] < boot['end_us'])
                require(tuple(sorted(record['checks'])) == checks)
                require(all(v in ('pass', 'fail') for v in record['checks'].values()))
                originals = local_measurements(run, boot, criterion, record, pins)
                source_passed = True
                for source in SOURCES[criterion]:
                    selected = [r for r in artifacts if r.get('id') == record['inputs'].get(source)]
                    require(len(selected) == 1)
                    artifact = selected[0]
                    require(artifact['source'] == source and artifact['criterion'] == criterion)
                    require(all(artifact[k] == pins[k] for k in ('router_revision', 'enclave_revision')))
                    require(artifact['config_sha256'] == pins['config']['sha256'])
                    require(artifact['region'] == boot['region'])
                    integer(artifact['started_at_us'])
                    integer(artifact['completed_at_us'])
                    require(artifact['started_at_us'] < artifact['completed_at_us'] <= record['completed_at_us'])
                    if source != 'ci':
                        require(artifact['completed_at_us'] - artifact['started_at_us'] <= 3_600_000_000)
                    require(artifact['conclusion'] in ('pass', 'fail'))
                    if source == 'budget_trial':
                        require(0 < artifact['completed_at_us'] - artifact['started_at_us'] <= 3_600_000_000)
                        require(artifact['workspace_count'] == 1 and artifact['within_envelope'] is True)
                    run.files.read(artifact['original'])
                    originals.append(artifact['original'])
                    originals.append(run.files.obj(f'{prefix}/{criterion}-{source}.json', artifact))
                    if artifact['conclusion'] != 'pass':
                        source_passed = False
                        run.gap(boot['boot'], criterion + ':failed_source', 'boot_prerequisite_gap')
                require(boot['revision'] == pins[boot['role'] + '_revision'])
                # Record and every failed check survive unchanged. No PASS checklist is invented.
                raw_check = run.files.obj(f'{prefix}/{criterion}-check.json', record)
                context = {k: boot[k] for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')}
                context.update(criterion=criterion, completed_at_us=record['completed_at_us'])
                context_ref = run.files.obj(f'{prefix}/{criterion}-context.json', context, PREREQUISITE_CONTEXT)
                sources = run.files.detail(f'{prefix}/{criterion}-sources',
                    [raw_check, context_ref, *originals], 'files')
                proof = dict(**TAGS, criterion=criterion, router_revision=pins['router_revision'],
                    enclave_revision=pins['enclave_revision'], completed_at_us=record['completed_at_us'],
                    producer='a3-readiness', checks=[dict(name=c, result=record['checks'][c] if source_passed else 'fail', evidence=raw_check)
                                                   for c in checks], sources=sources)
                proof_ref = run.files.obj(f'{prefix}/{criterion}.json', proof, a2.PROOF_RESULT)
                proofs.append(proof_ref)
                descriptor = dict(sha256=proof_ref['sha256'], completed_at_us=record['completed_at_us'])
                p1.ARTIFACT(descriptor)
                descriptors[criterion] = descriptor
                completions.append(record['completed_at_us'])
                if any(v != 'pass' for v in record['checks'].values()):
                    run.gap(boot['boot'], criterion + ':failed_check', 'boot_prerequisite_gap')
            except (KeyError, ValueError, OSError, TypeError):
                run.gap(boot['boot'], criterion + ':missing_or_invalid', 'boot_prerequisite_gap')
        ready_records = [r for r in startup if r.get('kind') == 'a3_traffic_ready' and r.get('boot') == boot['boot']]
        if len(descriptors) != 6 or len(ready_records) != 1:
            run.gap(boot['boot'], 'readiness_incomplete', 'boot_prerequisite_gap')
            continue
        ready = ready_records[0]['at_us']
        if type(ready) is not int or ready <= max(completions) or ready >= boot['end_us']:
            run.gap(boot['boot'], 'ready_before_checks', 'boot_prerequisite_gap')
            continue
        prior = (previous or {}).get(boot['boot'])
        if prior is not None:
            old = a1.PREREQUISITE_LEDGER.parse(run.files.read(prior))
            require(all(old[k] == boot[k] for k in ('boot', 'role', 'revision', 'region', 'boot_at_us')))
        ledger = dict(v=2, policy_version='shadow-v2', contract_revision='a1',
            **{k: boot[k] for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')},
            **{k + '_sha256': pins[k]['sha256'] for k in ('policy', 'fixture', 'config')},
            collection_interval=dict(start_us=boot['start_us'], end_us=boot['end_us']),
            traffic_ready_at_us=ready, previous_ledger_sha256=None if prior is None else prior['sha256'],
            **descriptors)
        ledgers.append(run.files.obj(prefix + '/ledger.json', ledger, a1.PREREQUISITE_LEDGER))
        if any(a['boot'] == boot['boot'] and a['at_us'] <= ready for a in attempts):
            run.gap(boot['boot'], 'early_opted_attempt', 'boot_prerequisite_gap')
    run.files.obj('readiness/status.json', dict(complete=not run.missing, missing=run.missing, attempts=attempt_ref,
                                              originals=run.sources))
    return ReadinessResult(tuple(ledgers), tuple(proofs), attempts, not run.missing)
