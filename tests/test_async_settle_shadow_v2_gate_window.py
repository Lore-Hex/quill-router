"""One session-owned full seven-day churn construction and rendered gate."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
from typing import Any

import pytest

from tests.shadow_v2_gate_fixture_generator import build, ports
from tests.test_async_settle_shadow_v2_gates import GAPS, damage
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_gate_load as load
from trusted_router import async_settle_shadow_v2_gate_report as reports
from trusted_router import async_settle_shadow_v2_gates as gates
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_bundle import ATTACHMENTS
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle
from trusted_router.async_settle_shadow_v2_gate_result import render
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.async_settle_shadow_v2_schema import Schema
from trusted_router.detached_jws import canonical


@contextmanager
def no_line_tracing() -> Iterator[None]:
    saved = sys.gettrace()
    # The exhaustive data adds no branches beyond the traced short witnesses.
    # Stop coverage's background-thread tracers too: on CPython 3.12 those keep
    # interpreter tracing overhead active even after sys.settrace(None).
    from coverage import Coverage
    coverage = Coverage.current()
    if coverage is not None:
        coverage.stop()
    sys.settrace(None)
    try:
        yield
    finally:
        if coverage is not None:
            coverage.start()
        else:
            sys.settrace(saved)


@pytest.fixture
def untraced() -> Iterator[None]:
    with no_line_tracing():
        yield


def exhaustive(test: Any) -> Any:
    return pytest.mark.usefixtures('untraced')(pytest.mark.timeout(method='signal')(test))


@pytest.fixture(scope='session')
def validation_cache():
    # The shared immutable fixture validates every distinct byte object once.
    # Keys include all arguments; mutations never inherit an old acceptance.
    patch = pytest.MonkeyPatch()
    parse = Schema.parse
    call = Schema.__call__
    validate = health.validate_publisher_traces
    schemas = {id(schema): schema for schema in ATTACHMENTS.values()}
    stable_schemas = {id(health.PUBLISHER_SLOT_TRACE), id(health.PUBLISHER_TRACE_PAGE), id(a1.COVERAGE_PAGE)}
    checked_values = {}
    @lru_cache(maxsize=16000)
    def parsed(schema_id, raw):
        return parse(schemas[schema_id], raw)
    def dispatch(schema, raw):
        return parsed(id(schema), raw) if id(schema) in schemas else parse(schema, raw)
    def checked_call(schema, value):
        key = (id(schema), id(value))
        if id(schema) in stable_schemas and key in checked_values and checked_values[key] is value:
            return
        call(schema, value)
        if id(schema) in stable_schemas:
            checked_values[key] = value
    digest = lru_cache(maxsize=16000)(roots.file_digest)
    root = a1.coverage_root
    coverage_roots = {}
    def coverage(pages):
        values = tuple(pages)
        key = tuple(map(id, values))
        if key not in coverage_roots:
            coverage_roots[key] = (values, root(list(values)))
        return coverage_roots[key][1]
    original_sha = load.sha256
    hashes = {}
    def sha(value):
        if (id(health.PUBLISHER_TRACE_PAGE), id(value)) not in checked_values and (id(a1.COVERAGE_PAGE), id(value)) not in checked_values:
            return original_sha(value)
        key = id(value)
        if key not in hashes:
            hashes[key] = (value, original_sha(value))
        return hashes[key][1]
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
            references(json.loads(artifacts[qualified]))
        named = tuple((k, artifacts.get(k)) for k in sorted(needed))
        presence = tuple((digest(body), artifacts.get('candidates/' + digest(body) + '.json') == body)
            for key in contract.ENTRY_PREIMAGES for entry in roots[key] for body in entry['bodies'])
        key = (canonical(manifest), canonical(proof), named, freeze(roots), presence)
        if key not in audits:
            audit(manifest, proof, artifacts=artifacts, roots=roots)
            audits[key] = True
    def traces(publisher, entries, **kwargs):
        key = (canonical(publisher), tuple(id(e) for e in entries), tuple(sorted(kwargs.items())))
        if key not in checked:
            validate(publisher, entries, **kwargs)
            checked[key] = entries  # retain identities until the session ends
    patch.setattr(gates, 'observer_health', observed)
    patch.setattr(reports, 'input_inventory', inventoried)
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
    checked_values.clear()
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
def test_seven_day_churn_render(seven_days: Bundle) -> None:
    result = evaluate(seven_days, ports(), gate='pre-flip')
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
    # projected/measured sources. Keep this acceptance requirement visible
    # while the retained-load adapter is unfinished; never fill it with zeros.
    for field, values in (('fleet_peaks', report['fleet_peaks']), ('publisher.peaks', report['publisher']['peaks'])):
        assert all(value is not None for value in values.values()), (field, [key for key, value in values.items() if value is None])


@exhaustive
@pytest.mark.xdist_group('p5-seven-days')
@pytest.mark.parametrize('gap', GAPS)
def test_seven_day_blocked_render(seven_days: Bundle, gap: str) -> None:
    bad = damage(seven_days, gap)
    if gap == 'continuous_window_gap':
        # Keep all the original evidence; cut the certified window short.
        bad = replace(seven_days, completed_at_us=seven_days.completed_at_us - 3000000)
    result = evaluate(bad, ports(), gate='pre-flip')
    raw, attachments = render(result)
    assert gap in result.gaps
    assert json.loads(raw)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report
