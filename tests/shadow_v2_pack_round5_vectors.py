"""Expected corrected outputs retained beside the current base-result checks."""
from __future__ import annotations

import json
from pathlib import Path


def corrected_result(name, case=0):
    catalog = json.loads(Path('tests/shadow_v2_pack_round5_corrected_results.json').read_bytes())
    value = dict(catalog['cases'][name + ':' + str(case)])
    for field in ('loading_issues', 'loading_validation', 'failures', 'output'):
        value[field] = catalog['vectors'][value[field]['vector']]
    paths = ['proof.shutdown_inventory_sha256', 'proof.cohort_seed_policy_sha256',
        'proof.pre_budget_sha256', 'proof.post_budget_sha256', 'proof.external_proofs_sha256',
        'proof.resolution_dag_sha256', 'manifest.independent_inventory_sha256',
        'loading/release-plan.json', 'loading/signatures.json']
    assert value['loading_issues'] == [dict(gap='artifact_gap', path=path,
        detail='required payload absent' if index < 6 else 'artifact_gap') for index, path in enumerate(paths)]
    assert value['verdict'] == 'BLOCKED' and value['proof_present'] is False
    assert value['output'] == dict(error='unavailable_required_report_commitments',
        condition='proof_manifest_absent_or_undecodable', status='BLOCKED', policy_version='shadow-v2')
    return value
