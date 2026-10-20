"""Reviewed pre-enable source scopes; never rewrite a dormant capture's pins."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_loading import PayloadError, retained_json
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_schema import name, require, sha256

QUALIFICATION_SCOPE = 'loading/qualification-scope.json'


def qualification_record(raw: bytes) -> dict[str, Any]:
    value = retained_json(raw)
    require(type(value) is dict and set(value) == {'production_resource', 'trial_resource',
        'production_deployment', 'trial_inventories', 'pre_envelope_sha256', 'post_envelope_sha256'})
    name(value['production_resource'])
    name(value['trial_resource'])
    a2.FILE_REF(value['production_deployment'])
    require(type(value['trial_inventories']) is list)
    for ref in value['trial_inventories']:
        a2.FILE_REF(ref)
    return value


def qualification(graph: PayloadGraph, payloads: dict[str, dict[str, Any]],
                  pre_input: dict[str, Any], pre_result: dict[str, Any]) -> None:
    """Production dormant measurements and closed trial evidence are distinct.

    The record is independently release-approved and is part of the original
    input inventory. Both envelopes still pass the unchanged frozen validator.
    Its trial inventory is the closed enabled-writer population of this graph;
    production deployment/read-health/CPU are only the retained pre projection.
    """
    from trusted_router.async_settle_shadow_v2_gate_release import approved_file
    raw = graph.files.get(QUALIFICATION_SCOPE)
    if raw is None:
        raise PayloadError(QUALIFICATION_SCOPE, 'artifact_gap')
    try:
        value = qualification_record(raw)
        require(value['production_resource'] != value['trial_resource'])
        approved_file(graph, dict(path=QUALIFICATION_SCOPE, bytes=len(raw), sha256=file_digest(raw)))
        production = value['production_deployment']
        a2.FILE_REF(production)
        require({k: production[k] for k in ('path', 'sha256')} == pre_input['deployment'])
        graph.file(production)
        require(pre_result['status'] == 'PASS' and pre_result['checklist']['dormant'] == 'PASS')
        expected = sorted(path for path in payloads if path.endswith('manifest.independent_inventory_sha256'))
        require(type(value['trial_inventories']) is list
            and sorted(ref['path'] for ref in value['trial_inventories']) == expected)
        for reference in value['trial_inventories']:
            graph.object(reference, a2.INDEPENDENT_INVENTORY)
        for stage in ('pre', 'post'):
            require(value[stage + '_envelope_sha256'] == sha256(payloads['proof.' + stage + '_budget_sha256']))
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, PayloadError):
            raise
        raise PayloadError(QUALIFICATION_SCOPE, 'evidence_schema_gap') from exc
