"""Compose exact archive members, procedure records and external results."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_external import (
    REQUIRED_STAGES as EARLIER_STAGES,
)
from trusted_router.async_settle_shadow_v2_pack_external import (
    ci_originals,
    proof_result,
)
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_scope import check_scope as earlier_checks
from trusted_router.async_settle_shadow_v2_pack_sources import archive_members
from trusted_router.async_settle_shadow_v2_schema import require

REQUIRED_STAGES = {stage: tuple(names) for stage, names in EARLIER_STAGES.items()}
REQUIRED_STAGES['pre-flip'] = (*REQUIRED_STAGES['pre-flip'], 'live', 'rare_cases')


def check_scope(name: str, stage: str) -> tuple[str, ...]:
    checks = earlier_checks(name, stage)
    if stage == 'pre-flip' and name in ('live', 'rare_cases'):
        checks = (*checks, 'drain_handover')
    return tuple(sorted(checks))


def procedure_result(files: PackFiles, plan: dict[str, Any], *, capture_root: Path) -> dict[str, Any]:
    """Only original completed check records supply conclusions.

    Each check points to a captured procedure output. Native procedures also
    retain their resource, interval, operation records and released settings.
    A title, supplied list or archive filename supplies no conclusion.
    """
    name, stage = plan['name'], plan['stage']
    scope = check_scope(name, stage)
    prefix = 'external/' + stage + '/' + name
    archive = (capture_root / plan['archive']).resolve()
    require(archive.is_relative_to(capture_root.resolve()), 'artifact_gap')
    selected = archive_members(files, archive, archive_sha256=plan['archive_sha256'],
                               members=[plan['member']], prefix=prefix + '/ci')
    result_ref = selected[0]
    completed = ci_originals(files, run=plan['run'], job=plan['job'], artifact=plan['artifact'],
                            result=result_ref, revision=plan['router_revision'])
    result = retained_json(files.read(result_ref))
    require(result['enclave_revision'] == plan['enclave_revision'], 'artifact_gap')
    supplied = result['procedures']
    require(tuple(sorted(supplied)) == scope, 'artifact_gap')
    require(tuple(sorted(plan['checks'])) == scope, 'artifact_gap')
    sources = [plan['run'], plan['job'], plan['artifact'], result_ref,
               files.refs[prefix + '/ci/selection.json']]
    checks = []
    for check in scope:
        ref = supplied[check]
        original = retained_json(files.read(ref))
        require(original['check'] == check and original['criterion'] == name, 'artifact_gap')
        require(original['router_revision'] == plan['router_revision'] and
                original['enclave_revision'] == plan['enclave_revision'], 'artifact_gap')
        require(original['completed_at_us'] <= completed and original['complete'] is True, 'artifact_gap')
        require(original['conclusion'] in ('pass', 'fail'), 'artifact_gap')
        require(original['kind'] in ('ci_procedure', 'native_trial'), 'artifact_gap')
        dependencies = original['sources']
        require(bool(dependencies), 'artifact_gap')
        for dependency in dependencies:
            files.read(dependency)
        if original['kind'] == 'native_trial':
            require(bool(original['resource']) and
                0 < original['completed_at_us'] - original['started_at_us'] <= 3600000000, 'artifact_gap')
            config = retained_json(files.read(original['configuration']))
            require(config['router_revision'] == plan['router_revision'] and
                    config['enclave_revision'] == plan['enclave_revision'], 'artifact_gap')
            require(bool(original['operations']), 'artifact_gap')
            for operation in original['operations']:
                measured = retained_json(files.read(operation))
                require(measured['resource'] == original['resource'] and measured['complete'] is True,
                        'artifact_gap')
                require(original['started_at_us'] <= measured['start_us'] < measured['end_us']
                        <= original['completed_at_us'], 'artifact_gap')
                require(type(measured['native_rpcs']) is int and measured['native_rpcs'] > 0, 'artifact_gap')
            dependencies = [*dependencies, original['configuration'], *original['operations']]
        sources.extend([ref, *dependencies])
        checks.append(dict(name=check, result=original['conclusion'], evidence=ref))
    return proof_result(files, name=name, stage=stage, approved_checks=list(scope),
        router_revision=plan['router_revision'], enclave_revision=plan['enclave_revision'],
        completed_at_us=completed, producer='captured-procedures', checks=checks,
        sources=list({r['path']: r for r in sources}.values()))
