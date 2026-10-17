"""Original-byte retained delegation and explicit reviewed delta boundaries."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_a2_roots import file_digest

# DELEGATED_PAYLOAD itself has no output field. Budget envelopes supply it;
# other uses commit this supplemental original capture in the approved finite
# dependency inventory. Missing captures are a named interface seam, not trust
# in a prior PASS transcript or an invented contract field.
from trusted_router.async_settle_shadow_v2_gate_inputs import RETAINED_OUTPUT, LoadingPorts
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_schema import digest, require
from trusted_router.detached_jws import canonical


def capture_path(payload_sha256: str) -> str:
    digest(payload_sha256)
    return 'loading/retained-output/' + payload_sha256 + '.json'


# These are replaced by A1 checks / the v2 simultaneous sweep, not waived.
# All other checklist bits remain binding; an overall v1 PASS is never required.
POST_DELTAS = frozenset(('pre_enable_before_opt_in', 'complete_instance_roster',
    'regional_coverage', 'freshness_components', 'combined_health_age',
    'combined_workspace_age', 'maximum_instance_headroom', 'fleet_read_headroom'))
SHADOW_DELTAS = frozenset(('durable_evaluable_sample', 'manifest_coverage',
    'serving_counter_coverage', 'serving_control_coverage', 'observer_counters',
    'fleet_pass'))


def delegate(graph: PayloadGraph, envelope: dict[str, Any],
             output: dict[str, Any] | None = None) -> tuple[Any, Any]:
    graph.delegated(envelope)
    ports = graph.ports
    require(isinstance(ports, LoadingPorts) and ports.retained is not None, 'artifact_gap')
    assert isinstance(ports, LoadingPorts) and ports.retained is not None
    runner = [a for a in ports.approvals if a.function is ports.retained]
    require(len(runner) == 1, 'artifact_gap')
    entry = envelope['entry_point']
    approved = [a for a in ports.approvals if a.entry_point.endswith('.' + entry)
                or a.entry_point == entry]
    require(len(approved) == 1, 'artifact_gap')
    record = approved[0]
    require(graph.file(envelope['source']) == record.source, 'artifact_gap')
    require(file_digest(record.source) == record.source_pin, 'artifact_gap')
    dependencies = graph.detail(envelope['dependencies'], 'files')
    files = {ref['path']: graph.file(ref) for ref in dependencies}
    from trusted_router.async_settle_shadow_v2_gate_loading import PayloadError
    try:
        payload = retained_json(graph.file(envelope['payload']))
        result = ports.retained(entry, payload, files)
    except PayloadError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise PayloadError(envelope['payload']['path'], 'evidence_schema_gap') from exc
    require(result.calls.get(entry, 0) >= 1 and set(result.opened) <= files.keys(), 'evidence_schema_gap')
    if entry == 'shadow_report.collect_rollbacks':
        require(result.calls.get('shadow_report.report', 0) >= 1, 'evidence_schema_gap')
    decoded = retained_json(result.output)
    require(result.output == canonical(decoded), 'evidence_schema_gap')
    if output is None:
        path = capture_path(envelope['payload']['sha256'])
        raw = graph.files.get(path)
        if raw is None:
            raise PayloadError(path, 'artifact_gap')
        try:
            capture = RETAINED_OUTPUT.parse(raw)
        except ValueError as exc:
            raise PayloadError(path, 'evidence_schema_gap') from exc
        require(capture['entry_point'] == entry and capture['payload_sha256'] == envelope['payload']['sha256'], 'evidence_schema_gap')
        output = capture['output']
    # Canonical function outputs, no invented CLI completion timestamp.
    require(result.output == graph.file(output), 'artifact_gap')
    if entry in ('pilot_enablement.pre_flip', 'pilot_enablement.shadow_serving'):
        fleet_input = {**payload['observer'], 'serving': payload['serving'], 'rows': payload['rows']}
        unchanged_checks('pilot_enablement.fleet_budgets', fleet_input, result.components['fleet_budgets'])
        if entry.endswith('.pre_flip'):
            unchanged_checks('pilot_enablement.shadow_serving', payload, result.components['shadow_serving'])
    unchanged_checks(entry, payload, decoded)
    adapters = [a for a in ports.approvals if a.function is unchanged_checks]
    require(len(adapters) == 1, 'artifact_gap')
    used_calls = [(record, result.calls[entry])]
    for name, count in result.calls.items():
        if name == entry:
            continue
        matches = [a for a in ports.approvals if a.entry_point.endswith('.' + name)]
        require(len(matches) == 1 and type(count) is int and count > 0, 'evidence_schema_gap')
        used_calls.append((matches[0], count))
    used_calls.extend(((runner[0], 1), (adapters[0], 1 + len(result.components))))
    for used, count in used_calls:
        graph.streams.setdefault('retained_calls', []).append(dict(entry_point=used.entry_point,
            calls=count, source_sha256=used.source_pin, fixture_sha256=used.fixture_pin, keyset_sha256=None))
    return payload, decoded


def unchanged_checks(entry: str, payload: Any, result: Any) -> None:
    """Keep every untouched check. Superseded checks are evaluated from A1.

    Row IDs and region names are taken from the original retained payload; an
    arbitrary checklist key cannot masquerade as a reviewed delta.
    """
    if entry.endswith('roster_coverage'):
        require(bool(result), 'evidence_schema_gap')
        return
    if entry.endswith('counter_coverage_report'):
        require(not result['legacy_region_labels'] and not result['legacy_revision_labels'], 'evidence_schema_gap')
        require(set(result['reasons']) <= {'counter_roster_gap'}, 'evidence_schema_gap')
        return
    if entry == 'shadow_report.collect_rollbacks':
        require(not any(gap.endswith(':missing_support') for gap in result['report']['gaps']), 'evidence_schema_gap')
        return
    allowed: set[str] = set()
    if entry.endswith('.fleet_budgets'):
        allowed.update(POST_DELTAS)
        allowed.update(row['id'] for row in payload['rows'])
        allowed.update(region + '_headroom' for region in payload['regional_budgets'])
    elif entry.endswith('.shadow_serving'):
        allowed.update(SHADOW_DELTAS)
    elif entry.endswith('.pre_flip'):
        # pre_flip's shadow_serving aggregate must be unpacked by the caller,
        # never silently ignored. Its seven-day rule is evaluated by G10.
        allowed.update(('seven_day_shadow_pass', 'shadow_serving'))
    require(bool(result['checklist']), 'evidence_schema_gap')
    require(all(value == 'PASS' for key, value in result['checklist'].items()
                if key not in allowed), 'evidence_schema_gap')
