"""Short original-byte witnesses and one session-owned seven-day group."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from tests.shadow_v2_gate_a2_fixture import (
    SEMANTIC_VARIANTS,
    mutate_transport,
    real_fixture,
    transport,
)
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_gate_fixture_generator import (
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
from trusted_router.async_settle_shadow_v2_a2_evidence import FAILURE_RULES
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_loader import load as load_original
from trusted_router.async_settle_shadow_v2_gate_result import render
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.async_settle_shadow_v2_schema import Schema
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='session')
def validation_cache(request: Any):
    # The shared immutable fixture validates every distinct byte object once.
    # Keys include all arguments; mutations never inherit an old acceptance.
    patch = pytest.MonkeyPatch()
    parse = request.getfixturevalue('payloadgraph_shared_lookup_cache')['parse']
    call = Schema.__call__
    decode = Schema._decode
    decoded_pages = {}
    def decoded(schema, raw):
        # parse() and _validate() decode the same canonical trace page twice.
        # Cache only this pure acceptance/rejection; retain immutable original
        # bytes and give the parser a fresh native object on every hit.
        if schema is not health.PUBLISHER_TRACE_PAGE:
            return decode(schema, raw)
        if raw not in decoded_pages:
            try:
                value = decode(schema, raw)
            except ValueError as exc:
                decoded_pages[raw] = str(exc)
                raise
            decoded_pages[raw] = None
            return value
        if decoded_pages[raw] is not None:
            raise ValueError(decoded_pages[raw])
        return json.loads(raw)

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
    root_entry = roots._root_entry
    @lru_cache(maxsize=16000)
    def entry_bytes(key, bodies):
        return canonical(root_entry(key, bodies))
    def entry(key, bodies):
        return json.loads(entry_bytes(key, tuple(bodies)))
    entry_root = roots.entry_root
    entry_roots = {}
    def rooted(entries, *, stream):
        values = list(entries)
        key = (stream, canonical(values))
        if key not in entry_roots:
            entry_roots[key] = entry_root(values, stream=stream)
        return entry_roots[key]

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
    patch.setattr(roots, '_root_entry', entry)
    patch.setattr(roots, 'entry_root', rooted)
    patch.setattr(a1, 'coverage_root', coverage)
    patch.setattr(load, 'sha256', sha)
    patch.setattr(a1, 'sha256', sha)
    patch.setattr(Schema, '_decode', decoded)
    patch.setattr(Schema, '__call__', checked_call)
    patch.setattr(contract, 'validate_preimages', audited)
    patch.setattr(Schema, 'parse', dispatch)
    patch.setattr(health, 'validate_publisher_traces', traces)
    yield
    patch.undo()
    parsed.cache_clear()
    decoded_pages.clear()
    checked.clear()
    audits.clear()
    digest.cache_clear()
    entry_bytes.cache_clear()
    entry_roots.clear()
    coverage_roots.clear()
    hashes.clear()
    origins.clear()
    trace_populations.clear()
    original.cache_clear()
    inventories.clear()
    observers.clear()


@exhaustive
@pytest.mark.proof_oracle
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


# Static reviewed family/stage assignment: one row for every contract gap.

WITNESSES = json.loads(Path(__file__).with_name('shadow_v2_gate_witnesses.json').read_text())
SHORT_CASES = [row for row in WITNESSES if row['family'] == 'short']
LONG_CASES = {'continuous_window_gap': ('continuous_window_gap',),
              'continuous_window_day_gap': ('continuous_window_gap',),
              'positive_seed_gap': ('positive_seed_gap', 'regional_seed_gap')}


@pytest.fixture(scope='module')
def short_family(tmp_path_factory: Any, validation_cache: Any) -> Any:
    original, ports, _ = real_fixture(tmp_path_factory.mktemp('short-originals'), seconds=400, pilot_stage='post-flip')
    return transport(original), ports


@pytest.mark.parametrize('case', SHORT_CASES, ids=lambda row: row['gap'])
def test_short_blocked_render(short_family: Any, tmp_path: Any, case: Any) -> None:
    gap = case['gap']
    if gap in SEMANTIC_VARIANTS:
        original, bound_ports, _ = real_fixture(tmp_path, variant=gap)
        bad = load_original(transport(original), bound_ports)
        assert bad.a2_streams['loading_issues'] == []
        assert bad.a2_streams['loading_validation'] == []
    else:
        raw, bound_ports = short_family
        bad = load_original(mutate_transport(raw, gap), bound_ports)
    result = evaluate(bad, bound_ports, gate=case['stage'])
    output, attachments = render(result)
    assert gap in result.gaps, (gap, result.gaps)
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report
    if gap in SEMANTIC_VARIANTS:
        assert not {'missing_support', 'evidence_schema_gap'} & set(result.gaps)
        assert all(f.detail == 'required report aggregate unavailable'
                   for f in result.failures if f.gap == 'artifact_gap')
        tally = result.gate_report['failure_counts'][gap]
        assert tally['count'] > 0 and tally['complete'] is True
        assert result.gate_report['verifiers']['binding']['calls'] > 0


@exhaustive
@pytest.mark.proof_oracle
@pytest.mark.xdist_group('p5-seven-days')
@pytest.mark.parametrize('gap', LONG_CASES)
def test_seven_day_blocked_render(real_seven_days: Any, gap: str) -> None:
    raw, bound_ports, _ = real_seven_days
    bad = load_original(mutate_transport(raw, gap), bound_ports)
    result = evaluate(bad, bound_ports, gate='pre-flip')
    output, attachments = render(result)
    assert set(LONG_CASES[gap]) <= set(result.gaps), (gap, result.gaps)
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report
    if gap == 'continuous_window_day_gap':
        assert result.report.elapsed_seconds == 172798
        assert len(result.report.gap_timestamps_us) == 1


def test_byte_witness_inventory_is_explicit() -> None:
    assert len(WITNESSES) == len({row['gap'] for row in WITNESSES}) == 58
    assert len(SEMANTIC_VARIANTS) == 14
    assert {row['gap']: row['rule'] for row in WITNESSES} == {rule.name: rule.rule for rule in FAILURE_RULES}
    assert {row['gap'] for row in WITNESSES} == set(GAPS)
    covered = {row['gap'] for row in SHORT_CASES} | {name for names in LONG_CASES.values() for name in names}
    assert covered == set(GAPS)
    assert all(row['why'] and row['family'] in ('short', 'long') for row in WITNESSES)
    for row in WITNESSES:
        assert row['rule'] in contract.REQUIRED_RULES[row['stage']]
        assert not any(row['rule'] in rules for stage, rules in contract.REQUIRED_RULES.items()
            if stage != row['stage'] and list(contract.REQUIRED_RULES).index(stage) < list(contract.REQUIRED_RULES).index(row['stage'])) or row['rule'] in ('G10', 'G11') or row['gap'] == 'close_handoff_budget_gap'


@pytest.fixture(scope='session')
def real_seven_days(tmp_path_factory: Any, validation_cache: Any) -> Any:
    with no_line_tracing():
        original, bound_ports, _ = real_fixture(tmp_path_factory.mktemp('real-seven-days'),
            seconds=604804, pilot_stage='pre-flip', churn=True)
        raw = transport(original)
        loaded = load_original(raw, bound_ports)
        assert loaded.a2_streams['loading_issues'] == []
        assert loaded.a2_streams['loading_validation'] == []
        return raw, bound_ports, loaded


def test_short_family_population(short_family: Any) -> None:
    raw, ports = short_family
    bundle = load_original(raw, ports)
    assert len(bundle.days) == 1
    assert bundle.inventory is not None
    routers = [boot for boot in bundle.inventory.boots if boot.role == 'router']
    assert len(routers) == 1
    assert routers[0].end_us - routers[0].start_us == 400000000
    assert sum(len(json.loads(row.raw)['entries']) for row in bundle.attachments
               if row.key.kind == 'publisher_trace') == 200


def test_traced_midnight_continuity(tmp_path: Any, monkeypatch: Any) -> None:
    from tests import shadow_v2_gate_fixture_generator as generator
    # Tiny boundary witness covers the same cross-day code as the long grid.
    monkeypatch.setattr(generator, 'START', generator.START + generator.DAY - 4000000)
    original, ports, _ = real_fixture(tmp_path, seconds=16)
    bundle = load_original(transport(original), ports)
    assert bundle.a2_streams['loading_issues'] == []
    assert bundle.a2_streams['loading_validation'] == []
    result = evaluate(bundle, ports, gate='shadow-serving')
    assert result.gaps == ()
    assert result.report.elapsed_seconds == 14
    assert len(result.gate_report['days']) == 2
    assert json.loads(render(result)[0])['verdict'] == 'PASS'


def test_long_family_has_ci_selection() -> None:
    import shlex
    import tomllib

    import yaml
    from _pytest.mark.expression import Expression

    root = Path(__file__).parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())['tool']['pytest']['ini_options']
    options = shlex.split(config['addopts']) if isinstance(config['addopts'], str) else config['addopts']
    expression = Expression.compile(options[options.index('-m') + 1]) if '-m' in options else Expression.compile('')
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text())
    command = '\n'.join(step.get('run', '') for step in workflow['jobs']['proof-oracle']['steps'])
    for test in (test_seven_day_churn_render, test_seven_day_blocked_render):
        markers = {mark.name for mark in test.pytestmark}
        default = expression.evaluate(lambda name, markers=markers: name in markers)
        assert default or ('proof_oracle' in markers and 'tests/test_async_settle_shadow_v2_gate_window.py' in command)


def test_bulk_trace_generator_preserves_original_bytes() -> None:
    from tests.shadow_v2_gate_fixture_generator import START, TAG, fixture, trace_rows
    from trusted_router.async_settle_shadow_v2_schema import sha256
    body = fixture('health')
    first = START // 2000000
    body['slot'] = first + 1
    template = fixture('publisher_slot_trace')
    generated = trace_rows('2026-10-06', 'boot0', first, first + 60, template, body)
    previous = None
    for row in generated:
        page = health.PUBLISHER_TRACE_PAGE.parse(row.raw)
        expected = []
        for slot in range(page['first_slot'], page['last_slot'] + 1):
            stamp = slot * 2000000
            entry = template.copy()
            entry.update(day='2026-10-06', boot='boot0', slot=slot, claim_start_us=stamp,
                claim_at_ns=stamp * 1000, observed_at_ns=stamp * 1000, publisher_heartbeat_ns=stamp * 1000,
                ack_at_us=stamp + 400000, body_sha256=sha256(body) if slot == body['slot'] else sha256(dict(slot=slot, boot='boot0')))
            expected.append(entry)
        original = dict(**TAG, day='2026-10-06', boot='boot0', first_slot=page['first_slot'],
            last_slot=page['last_slot'], previous_sha256=previous, entries=expected)
        assert row.raw == canonical(original)
        previous = sha256(original)


@pytest.mark.parametrize('mode', ['continuous', 'gap', 'seed', 'restart', 'lifetime', 'threshold'])
def test_short_verified_clock_branches(short_family: Any, mode: str) -> None:
    """Clock algorithm with verified prerequisite views as an explicit precondition.

    This emits no gate output and does not waive the original-prior-graph seam.
    The export acceptance tests still require that seam and remain fail-closed.
    """
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle, verify_samples
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key
    from trusted_router.async_settle_shadow_v2_schema import day_at

    raw, ports = short_family
    bundle = load_original(raw, ports)
    ctx = decode_bundle(bundle, ports, 'pre-flip')
    assert all('prior gate' in i.detail or 'prior_gate_' in i.detail for i in ctx.issues)
    ctx.issues.clear()
    start = min(b.start_us for b in bundle.inventory.boots)
    ctx.gates = {name: dict(verdict='PASS', completed_at_us=start) for name in ('transport', 'fleet', 'pre')}
    verify_samples(ctx)
    template = next(v for v in ctx.rows.values() if 'disabled_flag_intervals' in v)
    if mode == 'seed':
        ctx.verified.clear()
    elif mode in ('gap', 'restart'):
        original = template['disabled_flag_intervals'][0]
        template['disabled_flag_intervals'] = [
            {**original, 'interval': dict(start_us=start, end_us=start + 100000000)},
            {**original, 'interval': dict(start_us=start + (101000000 if mode == 'gap' else 100000000), end_us=bundle.completed_at_us)}]
    elif mode == 'lifetime':
        template['disabled_flag_intervals'][0]['interval']['end_us'] += 100000000
        ctx.bundle = replace(bundle, completed_at_us=bundle.completed_at_us + 100000000)
    elif mode == 'threshold':
        end = start + 604802000000
        days = []
        for midnight in range(start // 86400000000 * 86400000000, end, 86400000000):
            day = day_at(midnight)
            days.append(day)
            manifest = json.loads(canonical(template))
            manifest['day'] = day
            interval = dict(start_us=max(start, midnight), end_us=min(end, midnight + 86400000000))
            manifest['disabled_flag_intervals'][0]['interval'] = interval
            for writer in manifest['router_writers'].values():
                writer['lifetime'] = interval
            ctx.rows[Key(p1.KINDS[5], day + '/manifest-v2')] = manifest
        ctx.bundle = replace(bundle, days=tuple(days), completed_at_us=end,
            inventory=replace(bundle.inventory, boots=tuple(replace(b, end_us=end) for b in bundle.inventory.boots)))
    seed, elapsed, _ = gates.clock(ctx, ())
    assert elapsed == (None if mode == 'seed' else 98 if mode == 'gap' else 604800 if mode == 'threshold' else 398)
    assert seed == (None if mode == 'seed' else start + 2000000)
    assert any(i.gap == 'continuous_window_gap' for i in ctx.issues) == (mode != 'threshold')
    if mode == 'seed':
        assert any(i.gap == 'regional_seed_gap' for i in ctx.issues)
