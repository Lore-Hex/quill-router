"""Bind loaded evidence to independently approved release inputs."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts, ReleasePlan
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_schema import require


def plan(graph: PayloadGraph) -> ReleasePlan:
    ports = graph.ports
    require(isinstance(ports, LoadingPorts) and ports.release is not None, 'artifact_gap')
    assert isinstance(ports, LoadingPorts) and ports.release is not None
    release = ports.release
    require(graph.files.get(release.path) == release.raw, 'artifact_gap')
    return release


def approved_file(graph: PayloadGraph, reference: dict[str, Any]) -> None:
    release = plan(graph)
    raw = graph.file(reference)
    require(release.artifacts.get(reference['path']) == file_digest(raw), 'artifact_gap')


def validate_proof(graph: PayloadGraph, proof: dict[str, Any]) -> None:
    release = plan(graph)
    expected = release.criteria.get(proof['criterion'])
    require(expected is not None and bool(expected), 'evidence_schema_gap')
    assert expected is not None
    require(tuple(sorted(c['name'] for c in proof['checks'])) == tuple(sorted(expected)), 'evidence_schema_gap')
    require(graph.completed_at_us is not None and proof['completed_at_us'] <= graph.completed_at_us, 'evidence_schema_gap')
    sources = graph.detail(proof['sources'], 'files')
    require(bool(sources), 'artifact_gap')
    for reference in [*sources, *(c['evidence'] for c in proof['checks'])]:
        approved_file(graph, reference)
    require(all(c['result'] == 'pass' for c in proof['checks']), 'evidence_schema_gap')


def release_checks(graph: PayloadGraph, payloads: dict[str, dict[str, Any]]) -> None:
    release = plan(graph)
    # Every transitive source/fixture/CI dependency comes from the independently
    # approved plan, not just the top-level function source hash.
    for path, digest in release.artifacts.items():
        raw = graph.files.get(path)
        require(raw is not None and file_digest(raw) == digest, 'artifact_gap')

    def visit(value: Any) -> None:
        if type(value) is dict:
            if set(value) == set(a2.APPROVAL.fields):
                approved_file(graph, value['record'])
            else:
                for child in value.values():
                    visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)
    for value in payloads.values():
        visit(value)
    ports = graph.ports
    assert isinstance(ports, LoadingPorts)
    for approved in ports.approvals:
        pins = release.verifiers.get(approved.entry_point)
        require(pins is not None, 'artifact_gap')
        require(pins == (file_digest(approved.source), file_digest(approved.fixture),
                file_digest(approved.keyset) if approved.keys else None), 'artifact_gap')
    for name in ('certificate', 'binding'):
        function = getattr(ports, 'verify_' + name)
        matches = [a for a in ports.approvals if a.entry_point.endswith('.verify_' + name)]
        require(len(matches) == 1 and matches[0].function is function, 'evidence_schema_gap')


def decode_release_plan(raw: bytes, approved_sha256: str) -> ReleasePlan:
    """Validate every collection and member before iterating trusted approvals."""
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import ReleasePlan
    from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
    from trusted_router.async_settle_shadow_v2_schema import digest, name

    digest(approved_sha256)
    require(file_digest(raw) == approved_sha256, 'artifact_gap')
    value = retained_json(raw)
    require(type(value) is dict and set(value) in ({'artifacts', 'criteria', 'verifiers', 'required'},
        {'artifacts', 'criteria', 'verifiers', 'required', 'required_stages'}), 'evidence_schema_gap')
    require(all(type(value[field]) is dict for field in ('artifacts', 'criteria', 'verifiers')), 'evidence_schema_gap')
    for path, pin in value['artifacts'].items():
        a2.FILE_REF.fields['path'](path)
        digest(pin)
    for criterion, checks in value['criteria'].items():
        name(criterion)
        require(type(checks) is list and bool(checks) and all(type(c) is str for c in checks) and len(checks) == len(set(checks)), 'evidence_schema_gap')
        for check in checks:
            name(check)
    verifiers = {}
    for entry, pins in value['verifiers'].items():
        name(entry)
        require(type(pins) is list and len(pins) == 3, 'evidence_schema_gap')
        digest(pins[0])
        digest(pins[1])
        if pins[2] is not None:
            digest(pins[2])
        verifiers[entry] = tuple(pins)
    require(type(value['required']) is list and all(type(c) is str for c in value['required']) and len(value['required']) == len(set(value['required'])), 'evidence_schema_gap')
    for criterion in value['required']:
        name(criterion)
    required_stages = value.get('required_stages', {})
    from trusted_router.async_settle_shadow_v2_a2_contract import REQUIRED_RULES
    require(type(required_stages) is dict, 'evidence_schema_gap')
    for stage, criteria in required_stages.items():
        require(stage in REQUIRED_RULES and type(criteria) is list and bool(criteria) and all(type(c) is str for c in criteria)
            and len(criteria) == len(set(criteria)) and set(criteria) <= set(value['required']), 'evidence_schema_gap')
    require(not required_stages or {c for values in required_stages.values() for c in values} == set(value['required']), 'evidence_schema_gap')
    return ReleasePlan('loading/release-plan.json', raw, value['artifacts'],
        {k: tuple(v) for k, v in value['criteria'].items()}, verifiers, tuple(value['required']), {stage: tuple(values) for stage, values in required_stages.items()})


def measurement_parsers(release: ReleasePlan | None) -> dict[str, Any]:
    """The tool's approved producer format, selected by the original plan."""
    from trusted_router.async_settle_shadow_v2_gate_inputs import SHUTDOWN_SOURCE
    return {path: SHUTDOWN_SOURCE.parse for path in release.artifacts
            if path.startswith('sources/measurement/')} if release is not None else {}
