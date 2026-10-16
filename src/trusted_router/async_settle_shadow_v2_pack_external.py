"""Stage-scoped external originals with the complete first-window policy."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_inventory import timestamp
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS, CIPort, Pass, Query, approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_policy import (  # noqa: F401
    BUDGET,
    REQUIRED_STAGES,
    STAGES,
    TRANSPORT,
)
from trusted_router.async_settle_shadow_v2_schema import require


def policy(stages: dict[str, list[str]]) -> None:
    require(set(stages) <= set(STAGES), 'artifact_gap')
    for stage, names in REQUIRED_STAGES.items():
        require(set(names) <= set(stages.get(stage, [])), 'artifact_gap')
    for stage_names in stages.values():
        require(len(stage_names) == len(set(stage_names)), 'artifact_gap')


def capture_ci(run: Pass, port: CIPort, query: Query) -> list[dict[str, Any]]:
    require(query.page_size <= 100 and query.deadline_seconds <= 10, 'artifact_gap')
    return list(run.scan(port, query))


def ci_originals(files: PackFiles, *, run: dict[str, Any], job: dict[str, Any],
                 artifact: dict[str, Any], result: dict[str, Any], revision: str) -> int:
    """Check captured run/job/artifact metadata and the original result object.

    These are explicit downloaded JSON originals. Archive extraction and selecting
    immutable artifact members remain the CIArtifactCapture source adapter seam.
    """
    a2.revision(revision)
    r, j, a, output = (retained_json(files.read(ref)) for ref in (run, job, artifact, result))
    require(r['head_sha'] == j['head_sha'] == a['workflow_run']['head_sha'] == output['revision'] == revision,
            'artifact_gap')
    require(r['id'] == j['run_id'] == a['workflow_run']['id'] == output['run_id'], 'artifact_gap')
    require(j['id'] == output['job_id'] and a['id'] == output['artifact_id'], 'artifact_gap')
    require(r['status'] == j['status'] == 'completed' and a['expired'] is False, 'artifact_gap')
    require(r['conclusion'] == j['conclusion'] == output['conclusion'] == 'success', 'artifact_gap')
    completed = timestamp(j['completed_at'])
    require(completed == timestamp(output['completed_at']) and completed <= timestamp(r['updated_at']), 'artifact_gap')
    return completed


def proof_result(files: PackFiles, *, name: str, router_revision: str, enclave_revision: str,
                 completed_at_us: int, producer: str, approved_checks: list[str],
                 checks: list[dict[str, Any]], sources: list[dict[str, Any]],
                 stage: str = 'transport') -> dict[str, Any]:
    require(stage in STAGES, 'artifact_gap')
    prefix = 'external/' + stage + '/' + name
    require(bool(sources) and bool(approved_checks), 'artifact_gap')
    require(len(approved_checks) == len(set(approved_checks)), 'artifact_gap')
    require(sorted(check['name'] for check in checks) == sorted(approved_checks), 'artifact_gap')
    for ref in sources:
        files.read(ref)
    require(all(check['evidence'] in sources for check in checks), 'artifact_gap')
    value = dict(**TAGS, criterion=name, router_revision=router_revision, enclave_revision=enclave_revision,
        completed_at_us=completed_at_us, producer=producer, checks=sorted(checks, key=lambda c: c['name']),
        sources=files.detail(prefix + '/sources', sources, 'files'))
    files.obj(prefix + '/result.json', value, a2.PROOF_RESULT)
    return value


def external_pack(files: PackFiles, *, stage: str, required_stages: dict[str, list[str]],
                  supplied: list[dict[str, Any]], pilot: dict[str, Any] | None = None) -> dict[str, Any]:
    policy(required_stages)
    require(stage in STAGES, 'artifact_gap')
    # Transport and pre-enable are independent roots; later stages need both.
    reached = {stage}
    if stage in ('fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip'):
        reached.update(('transport', 'fleet-budgets-pre', 'fleet-budgets'))
    required = {(s, name) for s in reached for name in required_stages.get(s, [])}
    available = {(item['stage'], item['name']) for item in supplied}
    require(required <= available and len(available) == len(supplied), 'artifact_gap')
    for item in supplied:
        a2.EXTERNAL_REQUIREMENT(item)
        require(item['name'] in required_stages.get(item['stage'], []), 'artifact_gap')
        approval(files, item['approval'], item['evidence'])
        proof = a2.PROOF_RESULT.parse(files.read(item['evidence']))
        require(proof['criterion'] == item['name'] and all(proof[k] == item[k]
            for k in ('router_revision', 'enclave_revision')), 'artifact_gap')
    value = dict(**TAGS, requirements=sorted(supplied, key=lambda item: (item['stage'], item['name'])), pilot=pilot)
    files.obj('proof.external_proofs_sha256', value, a2.EXTERNAL_PROOFS)
    return value
