"""Retained history calls and original payload/output records."""
from __future__ import annotations

from typing import Any

from scripts.async_settle.async_settle_shadow_v2_gate_tools import run_retained
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS, approval
from trusted_router.async_settle_shadow_v2_gate_inputs import RETAINED_OUTPUT
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_gate_retained import capture_path
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require


def retained_call(files: PackFiles, *, entry: str, payload: dict[str, Any],
                  dependencies: list[dict[str, Any]], source: dict[str, Any],
                  prefix: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Import the existing runner; retain its exact output including BLOCKED."""
    files.read(source)
    originals = {ref['path']: files.read(ref) for ref in dependencies}
    result = run_retained(entry, retained_json(files.read(payload)), originals)
    require(result.calls.get(entry, 0) >= 1, 'artifact_gap')
    if entry == 'shadow_report.collect_rollbacks':
        require(result.calls.get('shadow_report.report', 0) >= 1, 'artifact_gap')
    output = files.put(prefix + '/output.json', result.output)
    capture = files.obj(capture_path(payload['sha256']),
        dict(entry_point=entry, payload_sha256=payload['sha256'], output=output), RETAINED_OUTPUT)
    refs = {ref['path']: ref for ref in [*dependencies, capture, output]}
    envelope = dict(**TAGS, entry_point=entry, payload=payload, source=source,
                    dependencies=files.detail(prefix + '/dependencies', list(refs.values()), 'files'))
    a2.DELEGATED_PAYLOAD(envelope)
    return envelope, output, retained_json(result.output)


def history(files: PackFiles, *, payload: dict[str, Any], source: dict[str, Any],
            dependencies: list[dict[str, Any]], reviewed_history: dict[str, Any],
            approvals: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the whole reviewed history before any selected-day filtering."""
    data = retained_json(files.read(payload))
    reviewed = retained_json(files.read(reviewed_history))
    reviews = [item for item in approvals if item['subject_sha256'] == reviewed_history['sha256']]
    require(len(reviews) == 1, 'artifact_gap')
    approval(files, reviews[0], reviewed_history)
    require(reviewed == dict(rows=data['rows'], resets=data['resets'],
                            resolutions=data['proof'].get('resolved_mismatches', [])), 'artifact_gap')
    retained, _, result = retained_call(files, entry='shadow_report.collect_rollbacks', payload=payload,
        dependencies=[*dependencies, reviewed_history], source=source, prefix='history')
    require(not any(gap.endswith(':missing_support') for gap in result['report']['gaps']), 'artifact_gap')
    facts: dict[tuple[Any, ...], dict[str, Any]] = {}
    for reset in result['resets'] + result['report']['resets']:
        key = reset['at_us'], reset['revision'], reset['reason'], reset.get('invalidated_at_us')
        facts.setdefault(key, {}).update(reset)
    support = files.detail('history/support', [payload, reviewed_history], 'files')
    entries = [dict(at_us=fact['at_us'], revision=fact['revision'], reason=fact['reason'], support=support,
                    resolved_at_us=fact.get('resolved_at_us'), fixed_revision=fact.get('fixed_revision'),
                    invalidated_at_us=fact.get('invalidated_at_us')) for fact in facts.values()]
    for resolution in data['proof'].get('resolved_mismatches', []):
        require(any(a['subject_sha256'] == resolution['artifact_sha256'] for a in approvals), 'artifact_gap')
    value = dict(**TAGS, retained=retained, history=files.detail('history/entries', entries, 'reset_history'),
                 approvals=approvals)
    files.obj('proof.resolution_dag_sha256', value, a2.RESOLUTION_DAG)
    # No retained v1 elapsed seconds are projected into v2 observation coverage.
    return value
