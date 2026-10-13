"""One session-owned full seven-day churn construction and rendered gate."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pytest

from tests.shadow_v2_gate_a2_fixture import (
    BYTE_WITNESS_SEAMS,
    mutate_transport,
    real_fixture,
    transport,
)
from tests.shadow_v2_gate_fixture_generator import (
    build,
    exhaustive,
    no_line_tracing,
    untraced,  # noqa: F401
)
from tests.test_async_settle_shadow_v2_gates import GAPS
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_gate_load as load
from trusted_router import async_settle_shadow_v2_gate_report as reports
from trusted_router import async_settle_shadow_v2_gates as gates
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle
from trusted_router.async_settle_shadow_v2_gate_loader import load as load_original
from trusted_router.async_settle_shadow_v2_gate_result import render
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.async_settle_shadow_v2_schema import Schema
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='session')
def validation_cache():
    # The shared immutable fixture validates every distinct byte object once.
    # Keys include all arguments; mutations never inherit an old acceptance.
    patch = pytest.MonkeyPatch()
    parse = Schema.parse
    call = Schema.__call__
    validate = health.validate_publisher_traces
    # Only these parser results are immutable within this fixture family.
    # Other schemas (notably shutdown sources) are edited by fixture builders.
    schemas = {id(schema): schema for schema in (health.PUBLISHER_TRACE_PAGE, a1.COVERAGE_PAGE)}
    origins = {}
    @lru_cache(maxsize=16000)
    def parsed(schema_id, raw):
        value = parse(schemas[schema_id], raw)
        origins[id(value)] = (value, (schema_id, raw))
        if schema_id == id(health.PUBLISHER_TRACE_PAGE):
            for index, entry in enumerate(value['entries']):
                origins[id(entry)] = (entry, (id(health.PUBLISHER_SLOT_TRACE), raw, index))
        return value
    def dispatch(schema, raw):
        return parsed(id(schema), raw) if id(schema) in schemas else parse(schema, raw)
    def checked_call(schema, value):
        origin = origins.get(id(value))
        if origin is not None and origin[0] is value and origin[1][0] == id(schema):
            return
        call(schema, value)
    digest = lru_cache(maxsize=16000)(roots.file_digest)
    root = a1.coverage_root
    coverage_roots = {}
    def content(value):
        origin = origins.get(id(value))
        return origin[1] if origin is not None and origin[0] is value else canonical(value)
    def coverage(pages):
        values = tuple(pages)
        key = tuple(content(v) for v in values)
        if key not in coverage_roots:
            coverage_roots[key] = root(list(values))
        return coverage_roots[key]
    original_sha = load.sha256
    hashes = {}
    def sha(value):
        origin = origins.get(id(value))
        if origin is None or origin[0] is not value:
            return original_sha(value)
        key = origin[1]
        if key not in hashes:
            hashes[key] = original_sha(value)
        return hashes[key]
    input_inventory = reports.input_inventory
    inventories = {}
    def inventoried(ctx):
        b = ctx.bundle
        key = (b.rows, b.attachments, b.proof, b.ledgers, b.preimages)
        if key not in inventories:
            inventories[key] = input_inventory(ctx)
        return inventories[key]
    observer = gates.observer_health
    observers = {}
    def observed(ctx):
        b = ctx.bundle
        key = (tuple(r for r in b.rows if r.key.kind in (p1.KINDS[2], p1.KINDS[3])),
               tuple(r for r in b.attachments if r.key.kind in ('health_basis', 'publisher_trace')),
               b.inventory, b.cohort)
        if key not in observers:
            before = len(ctx.issues)
            observer(ctx)
            observers[key] = tuple(ctx.issues[before:])
        else:
            ctx.issues.extend(observers[key])
    checked = {}
    audits = {}
    audit = contract.validate_preimages
    def freeze(value):
        if isinstance(value, dict):
            return tuple((k, freeze(v)) for k, v in sorted(value.items()))
        if isinstance(value, list):
            return tuple(map(freeze, value))
        return value
    def audited(manifest, proof, *, artifacts, roots):
        # The generator gives every candidate exactly this digest-qualified key.
        # Unreferenced row changes cannot affect this pure supplied-root check.
        needed = {*contract.FILE_PREIMAGES, *contract.OBJECT_PREIMAGES, 'proof_manifest'}
        def references(value):
            if isinstance(value, dict):
                if set(value) == {'path', 'sha256', 'bytes'}:
                    needed.add(value['path'])
                for child in value.values():
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)
        for qualified in contract.OBJECT_PREIMAGES:
            if qualified in artifacts:
                references(json.loads(artifacts[qualified]))
        named = tuple((k, artifacts.get(k)) for k in sorted(needed))
        presence = tuple((digest(body), artifacts.get('candidates/' + digest(body) + '.json') == body)
            for key in contract.ENTRY_PREIMAGES for entry in roots[key] for body in entry['bodies'])
        key = (canonical(manifest), canonical(proof), named, freeze(roots), presence)
        if key not in audits:
            try:
                audit(manifest, proof, artifacts=artifacts, roots=roots)
            except ValueError as exc:
                audits[key] = str(exc)
            else:
                audits[key] = None
        if audits[key] is not None:
            raise ValueError(audits[key])
    def traces(publisher, entries, **kwargs):
        key = (canonical(publisher), tuple(content(e) for e in entries), tuple(sorted(kwargs.items())))
        if key not in checked:
            try:
                validate(publisher, entries, **kwargs)
            except ValueError as exc:
                checked[key] = str(exc)
            else:
                checked[key] = None
        if checked[key] is not None:
            raise ValueError(checked[key])
    from trusted_router import async_settle_shadow_v2_gate_expected as expected
    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    enumerate_traces = expected.trace_keys
    trace_populations = {}
    def trace_keys(ctx):
        b = ctx.bundle
        key = (tuple(r for r in b.attachments if r.key.kind == 'publisher_trace'), b.inventory, b.completed_at_us)
        if key not in trace_populations:
            trace_populations[key] = frozenset(enumerate_traces(ctx))
        return set(trace_populations[key])
    # Repeated transports contain equal original byte strings. Intern these at
    # the test loading boundary so per-mutation cache keys share their storage.
    row_type, file_type = loader.Row, loader.Preimage
    @lru_cache(maxsize=16000)
    def original(raw):
        return raw
    patch.setattr(loader, 'Row', lambda key, raw: row_type(key, original(raw)))
    patch.setattr(loader, 'Preimage', lambda name, raw: file_type(name, original(raw)))
    patch.setattr(expected, 'trace_keys', trace_keys)
    patch.setattr(expected, 'sha256', sha)
    patch.setattr(gates, 'observer_health', observed)
    patch.setattr(reports, 'input_inventory', inventoried)
    import sys
    original_digest = roots.file_digest
    for module_name, module in tuple(sys.modules.items()):
        if module_name.startswith('trusted_router.async_settle_shadow_v2_gate') and getattr(module, 'file_digest', None) is original_digest:
            patch.setattr(module, 'file_digest', digest)
    patch.setattr(roots, 'file_digest', digest)
    patch.setattr(a1, 'coverage_root', coverage)
    patch.setattr(load, 'sha256', sha)
    patch.setattr(a1, 'sha256', sha)
    patch.setattr(Schema, '__call__', checked_call)
    patch.setattr(contract, 'validate_preimages', audited)
    patch.setattr(Schema, 'parse', dispatch)
    patch.setattr(health, 'validate_publisher_traces', traces)
    yield
    patch.undo()
    parsed.cache_clear()
    checked.clear()
    audits.clear()
    digest.cache_clear()
    coverage_roots.clear()
    hashes.clear()
    origins.clear()
    trace_populations.clear()
    original.cache_clear()
    inventories.clear()
    observers.clear()


@pytest.fixture(scope='session')
def seven_days(tmp_path_factory: Any, validation_cache: Any) -> Bundle:
    # No test mutates this frozen bundle or its byte objects. Negative variants
    # replace only one immutable row/tuple. Short traced witnesses cover the
    # same schema and gate branches as this exhaustive 302,402-slot population.
    with no_line_tracing():
        return build(tmp_path_factory.mktemp('p5-seven-days'), seconds=604804)


@exhaustive
@pytest.mark.xdist_group('p5-seven-days')
def test_seven_day_churn_render(real_seven_days: Any) -> None:
    _, bound_ports, loaded = real_seven_days
    result = evaluate(loaded, bound_ports, gate='pre-flip')
    raw, attachments = render(result)
    report = json.loads(attachments[0])
    assert report['elapsed_seconds'] == 604802
    assert report['partition'] == dict(T=1, A=1, B=0, C=0)
    assert report['verifiers']['certificate']['calls'] == 0
    assert report['timing']['publisher_slot_rpc_elapsed_us']['count'] == 302402
    assert len(report['days']) == 8
    assert json.loads(raw)['verdict'] == 'PASS', [(f.gap, f.detail) for f in result.failures]
    assert result.gaps == () and json.loads(raw)['gaps'] == []
    assert report['complete'] is True
    assert all(tally == dict(count=0, listed=0, complete=True, day_counts=[0] * 8)
               for tally in report['failure_counts'].values())
    assert file_digest(attachments[0]) == json.loads(raw)['coverage_sha256']
    for field in ('comparator', 'admission', 'partition', 'attempts', 'checkpoint', 'handoff', 'publisher'):
        assert all(value is not None for key, value in report[field].items() if key not in ('coverage', 'peaks')), field
    # A structural PASS on the current A2 head cannot waive G7's required
    # projected/measured original sources; never fill missing facts with zeros.
    for field, values in (('fleet_peaks', report['fleet_peaks']), ('publisher.peaks', report['publisher']['peaks'])):
        assert all(value is not None for value in values.values()), (field, [key for key, value in values.items() if value is None])


# Three byte mutations each cover two/three named failures. Evaluate each
# distinct original once, retaining all corresponding named assertions.
BYTE_CASES = {gap: (gap,) for gap in GAPS if gap not in BYTE_WITNESS_SEAMS}
for representative, aliases in (
    ('artifact_gap', ('external_proofs_missing', 'pilot_prerequisite_gap')),
    ('positive_seed_gap', ('regional_seed_gap',)),
    ('prediction_demand_gap', ('prediction_unknown_gap',)),
):
    BYTE_CASES[representative] += aliases
    for alias in aliases:
        del BYTE_CASES[alias]


@exhaustive
@pytest.mark.xdist_group('p5-seven-days')
@pytest.mark.parametrize('gap', BYTE_CASES)
def test_seven_day_blocked_render(real_seven_days: Any, gap: str) -> None:
    raw, bound_ports, _ = real_seven_days
    bad = load_original(mutate_transport(raw, gap), bound_ports)
    result = evaluate(bad, bound_ports, gate='pre-flip')
    output, attachments = render(result)
    assert set(BYTE_CASES[gap]) <= set(result.gaps), (gap, result.gaps)
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report


def test_byte_witness_inventory_is_explicit() -> None:
    covered = {name for names in BYTE_CASES.values() for name in names}
    assert covered.isdisjoint(BYTE_WITNESS_SEAMS)
    assert covered | set(BYTE_WITNESS_SEAMS) == set(GAPS)
    assert len(covered) == 44 and len(BYTE_WITNESS_SEAMS) == 14


@pytest.fixture(scope='session')
def real_seven_days(tmp_path_factory: Any, seven_days: Bundle) -> Any:
    with no_line_tracing():
        original, bound_ports, _ = real_fixture(tmp_path_factory.mktemp('real-seven-days'),
            seconds=604804, pilot_stage='pre-flip', seed_bundle=seven_days)
        raw = transport(original)
        loaded = load_original(raw, bound_ports)
        assert loaded.a2_streams['loading_issues'] == []
        assert loaded.a2_streams['loading_validation'] == []
        return raw, bound_ports, loaded
