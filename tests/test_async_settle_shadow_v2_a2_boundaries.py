"""A2 numeric, transport, stream and relationship boundary proofs."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests import shadow_v2_a2_fixture_generator as generator
from tests.test_async_settle_shadow_v2_a2 import SCHEMAS, VECTORS, obj
from tests.test_async_settle_shadow_v2_contract import assert_round_trip
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router import async_settle_shadow_v2_a2_contract as c
from trusted_router import async_settle_shadow_v2_a2_evidence as e
from trusted_router import async_settle_shadow_v2_a2_roots as r
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router.detached_jws import canonical


def nested(value: Any) -> Any:
    yield value
    if type(value) is dict:
        for child in value.values():
            yield from nested(child)
    elif type(value) is list:
        for child in value:
            yield from nested(child)


def seeds() -> dict[str, Any]:
    result: dict[str, Any] = {}
    candidates = [x for _, v in VECTORS if v['expected'] == 'accept' for x in nested(v['object']) if type(x) is dict]
    by_fields: dict[frozenset[str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_fields.setdefault(frozenset(candidate), []).append(candidate)
    for name, schema in SCHEMAS.items():
        if name in ('ROOT_ENTRY', 'GATE_OUTPUT', 'COORDINATES'):
            continue
        for value in by_fields.get(frozenset(schema.fields), []):
            if set(value) == set(schema.fields):
                try:
                    schema(value)
                except ValueError:
                    continue
                result[name] = value
                break
    return result


SEEDS = seeds()


def test_traced_nonempty_detail_streams() -> None:
    for stream, schema_name in (
        ('writers', 'WRITER_REPORT'), ('regions', 'REGION_REPORT'),
        ('input_inventory', 'INPUT_INVENTORY_ENTRY'), ('reset_history', 'RESET_ENTRY'),
        ('shutdown_writers', 'SHUTDOWN_ENTRY'), ('segments', 'LOAD_SEGMENT'),
        ('operations', 'EXPORT_OPERATION'), ('files', 'FILE_REF'),
    ):
        entries = [copy.deepcopy(SEEDS[schema_name])]
        pages = make_pages(entries)
        assert r.entry_root(entries, stream=stream) == s.sha256(entries)
        r.validate_detail(make_detail(entries, pages, stream), pages, stream=stream)
    with pytest.raises(ValueError):
        r.validate_detail(SEEDS['DETAIL'], [], stream='close_result_root')


def test_traced_virtual_file_and_region_budget_subjects() -> None:
    assert_round_trip(e.FAILURE_SUBJECT, e.O('a2_file', 'evidence/source.json'))
    assert_round_trip(e.FAILURE_SUBJECT, e.K('post', 'us-central1'))


def test_traced_nonempty_preimage_graph() -> None:
    from tests.test_async_settle_shadow_v2_a2 import vector

    args = obj('root_preimages', 'all-defined-preimages-empty')
    artifacts = {k: bytes.fromhex(v) for k, v in args.pop('artifacts_hex').items()}
    args['artifacts'] = artifacts
    # Match the literal roots' w1 identity to the supplied manifest/proof scope.
    writer = args['manifest']['router_writers']['issuer']
    args['manifest']['router_writers'] = {'w1': writer}
    args['proof']['rosters_by_day'] = {args['manifest']['day']: {'router': {'w1': writer}, 'enclave': {}}}
    args['proof']['instance_regions_by_day'] = {args['manifest']['day']: {'w1': writer['region']}}
    body = b'{}\n'
    artifacts['candidates/' + r.file_digest(body) + '.json'] = body
    for qualified, stream in c.ENTRY_PREIMAGES.items():
        literal = vector('root_preimages', 'literal-' + stream)['object']['entries'][0]
        args['roots'][qualified] = [{'key': literal['key'], 'bodies': [body]}]
        owner, field = qualified.split('.')
        args[owner][field] = r.entry_root([r._root_entry(literal['key'], [body])], stream=stream)
    for qualified, stream in c.COVERAGE_PREIMAGES.items():
        pages = obj('root_preimages', 'frozen-page-root-' + stream)
        pages[0]['day'] = args['manifest']['day']
        args['roots'][qualified] = pages
        args['manifest'][qualified.split('.')[1]] = e.a1.coverage_root(pages)
    for qualified, stream in c.CROSS_DAY_PREIMAGES.items():
        pages = obj('root_preimages', 'frozen-page-root-' + stream)
        pages[0]['day'] = args['manifest']['day']
        args['roots'][qualified] = [{'day': pages[0]['day'], 'pages': pages}]
        args['proof'][qualified.split('.')[1]] = s.sha256([{'day': pages[0]['day'], 'root': e.a1.coverage_root(pages), 'count': sum(len(p['entries']) for p in pages)}])
    artifacts['proof_manifest'] = canonical(args['proof']) + b'\n'
    args['manifest']['proof_manifest_sha256'] = r.file_digest(artifacts['proof_manifest'])
    c.validate_preimages(**args)
    args['manifest']['proof_manifest_sha256'] = None
    c.validate_preimages(**args)


def test_all_new_shapes_seeded() -> None:
    assert set(SEEDS) == set(SCHEMAS) - {'ROOT_ENTRY', 'GATE_OUTPUT', 'COORDINATES'}
    assert all(type(SCHEMAS[name]) is s.Schema for name in SEEDS)


@exhaustive
@pytest.mark.parametrize('name', sorted(SEEDS))
def test_every_shape_native_transport_total_and_bounds(name: str) -> None:
    schema, value = SCHEMAS[name], SEEDS[name]
    assert_round_trip(schema, value)
    for bad in (None, True, 0, 1.0, '', [], [value], {'extra': 0}):
        with pytest.raises(ValueError):
            schema(bad)
        with pytest.raises(ValueError):
            schema.parse(canonical(bad))
    raw = canonical(value)
    for offset in (0, 1):
        padded = raw + b' ' * (schema.limit + offset - len(raw))
        if offset:
            with pytest.raises(ValueError):
                schema.parse(padded)
        else:
            assert schema.parse(padded) == value
    for field in value:
        with pytest.raises(ValueError):
            schema({k: v for k, v in value.items() if k != field})
    key = next(iter(value))
    with pytest.raises(ValueError):
        schema.parse(b'{' + canonical(key) + b':' + canonical(value[key]) + b',' + raw[1:])
    for spelling in (b'2.0', b'2e0', b'NaN', b'-0', b'9223372036854775808'):
        with pytest.raises(ValueError):
            schema.parse(b'{' + canonical(key) + b':' + spelling + b'}')
    for bad in ({**value, 'surrogate': '\ud800'}, {**value, 'tuple': (1,)}, {**value, 1: 0}):
        with pytest.raises(ValueError):
            schema(bad)


@pytest.mark.parametrize('name', ['GATE_REPORT', 'GATE_FAILURE', 'RATIO', 'STAT', 'FILE_REF', 'SHUTDOWN_ENTRY'])
def test_traced_native_and_byte_branches(name: str) -> None:
    schema, value = SCHEMAS[name], SEEDS[name]
    assert_round_trip(schema, value)
    with pytest.raises(ValueError):
        schema({**value, 'extra': 0})
    raw = canonical(value)
    assert schema.parse(raw + b' ' * (schema.limit - len(raw))) == value
    with pytest.raises(ValueError):
        schema.parse(raw + b' ' * (schema.limit + 1 - len(raw)))


@exhaustive
@pytest.mark.parametrize('name', sorted(SEEDS))
def test_every_nullable_and_numeric_field(name: str) -> None:
    schema, value = SCHEMAS[name], SEEDS[name]
    for field, check in schema.fields.items():
        if not callable(check) or isinstance(check, s.Schema):
            continue
        wrapped = getattr(check, '__qualname__', '') == 'nullable.<locals>.validate'
        primitive = check.__closure__[0].cell_contents if wrapped else check
        if wrapped:
            check(None)
            # Native and serialized acceptance must agree, including invariants.
            native = {**value, field: None}
            try:
                schema(native)
            except ValueError:
                with pytest.raises(ValueError):
                    schema.parse(canonical(native))
            else:
                assert_round_trip(schema, native)
        if primitive in (s.integer, s.signed_integer):
            for n in (0, s.INT64):
                check(n)
            for n in (True, 1.0, '1', s.INT64 + 1, -s.INT64 - 1):
                with pytest.raises(ValueError):
                    check(n)
                with pytest.raises(ValueError):
                    schema({**value, field: n})


@pytest.mark.parametrize('count', [15, 16, 17, s.INT64])
def test_tally_limits(count: int) -> None:
    value = {'count': count, 'listed': min(count, 16), 'complete': count <= 16, 'day_counts': [count] * 32}
    assert_round_trip(e.FAILURE_TALLY, value)
    for change in ({'listed': 17}, {'count': s.INT64 + 1}, {'day_counts': [count] * 33}, {'day_counts': [s.INT64 + 1]}, {'complete': count > 16}):
        with pytest.raises(ValueError):
            e.FAILURE_TALLY({**value, **change})


def failure_entries() -> list[dict[str, Any]]:
    # 48 names with freely varying valid object/writer/bucket/reset identities
    # suffice to exercise the fourth page; run-only names remain singletons.
    result = []
    for _, v in VECTORS:
        if not v['name'].startswith('constructor-'):
            continue
        base = v['object']
        sub = base['subject']
        if sub['scope'] in ('writer', 'bucket'):
            for i in range(16):
                result.append({**base, 'subject': {**sub, 'boot': f'w{i:02}'}})
        elif sub['scope'] == 'reset':
            for i in range(16):
                changed = copy.deepcopy(base)
                parts = list(e.decode_key(changed['subject']['object']['key']))
                parts[0] = str(i)
                changed['subject'].update(at_us=i)
                changed['subject']['object']['key'] = e.L(*parts)
                result.append(changed)
        elif sub['scope'] == 'object' and sub['object']['event_seq'] is not None:
            for i in range(16):
                changed = copy.deepcopy(base)
                changed['subject']['object']['event_seq'] = f'{i + 1:020}'
                result.append(changed)
        elif sub['scope'] == 'object' and not sub['object']['kind'].startswith('a2_'):
            for i in range(16):
                changed = copy.deepcopy(base)
                changed['subject']['object']['key'] = f'2026-10-12/w{i:02}'
                result.append(changed)
        else:
            result.append(base)
    return sorted(result, key=lambda f: canonical([f['gap'], f['subject']]))


def make_pages(entries: list[dict[str, Any]], limit: int = 255) -> list[dict[str, Any]]:
    pages = []
    for i in range(0, len(entries), limit):
        pages.append({'v': 2, 'policy_version': 'shadow-v2', 'contract_revision': 'a2',
            'page_seq': f'{len(pages) + 1:020}', 'previous_sha256': s.sha256(pages[-1]) if pages else None, 'entries': entries[i:i + limit]})
    return pages


def make_detail(entries: list[dict[str, Any]], pages: list[dict[str, Any]], stream: str) -> dict[str, Any]:
    return {'root': r.entry_root(entries, stream=stream), 'count': len(entries), 'pages': len(pages), 'tail_sha256': s.sha256(pages[-1]) if pages else None}


@exhaustive
def test_four_page_chain_drop_swap_tail_order_and_counts() -> None:
    entries = failure_entries()
    assert 765 < len(entries) <= 928
    pages = make_pages(entries)
    assert len(pages) == 4
    detail = make_detail(entries, pages, 'failures')
    r.validate_detail(detail, pages, stream='failures')
    for bad in (pages[:-1], [pages[1], pages[0], *pages[2:]], [*pages, pages[-1]], list(reversed(pages))):
        with pytest.raises(ValueError):
            r.validate_detail(detail, bad, stream='failures')
    for field, value in (('count', len(entries) - 1), ('pages', 3), ('tail_sha256', 'f' * 64), ('root', 'f' * 64)):
        with pytest.raises(ValueError):
            r.validate_detail({**detail, field: value}, pages, stream='failures')
    bad = copy.deepcopy(pages)
    bad[0]['entries'].reverse()
    with pytest.raises(ValueError):
        r.validate_detail(detail, bad, stream='failures')


def test_seventeen_subjects_rejected() -> None:
    base = obj('gate_failures', 'constructor-writer_closure_gap')
    entries = [{**base, 'subject': {**base['subject'], 'boot': f'w{i:02}'}} for i in range(17)]
    r.entry_root(entries[:16], stream='failures')
    with pytest.raises(ValueError):
        r.entry_root(entries, stream='failures')


@exhaustive
def test_failure_cardinality_and_all_page_count_ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    base = obj('gate_failures', 'constructor-writer_closure_gap')
    # The conservative 928 bound is larger than the reachable population when
    # run-only names are singletons. Isolate it from name vocabulary/subject
    # restrictions, retaining the real root function and per-name limit of 16.
    schema, page_schema, limit, bound = r.STREAMS['failures']
    fields = {**schema.fields, 'gap': s.name}
    monkeypatch.setitem(r.STREAMS, 'failures', (s.Schema(fields, 1024), page_schema, limit, bound))
    entries = [{**base, 'gap': f'name{i // 16:02}',
                'subject': {**base['subject'], 'boot': f'w{i % 16:02}'}} for i in range(929)]
    assert r.entry_root(entries[:928], stream='failures') == s.sha256(entries[:928])
    with pytest.raises(ValueError):
        r.entry_root(entries, stream='failures')
    # Count checks are also isolated at every schema's array boundary.
    for schema, seed, maximum in ((e.FAILURE_PAGE, base, 255),
        (e.WRITER_REPORT_PAGE, obj('writer_reports', 'unknown-region'), 16),
        (e.REGION_REPORT_PAGE, obj('region_reports', 'region'), 8),
        (e.ROOT_ENTRY_PAGE, obj('root_preimages', 'conflict-entry-2'), 128)):
        check = schema.fields['entries']
        check([seed] * maximum)
        with pytest.raises(ValueError):
            check([seed] * (maximum + 1))
    assert r.STREAMS['failures'][3] == 928


@pytest.mark.parametrize('stream,parts', [
    ('counter_root', ('2026-10-12', 'w1')), ('closure_root', ('2026-10-12', 'w1')),
    ('receipts_root', ('2026-10-12', 'w1', '00000000000000000001')),
    ('prerequisite_ledger_root', ('router', 'w1', 'a' * 40, 'region', '10')),
    ('ledger_root', ('router', 'w1', 'a' * 40, 'region', '10')),
    ('publisher_slot_trace_sha256', ('2026-10-12', 'w1', '10')),
])
def test_every_new_root_uses_conflict_construction(stream: str, parts: tuple[str, ...]) -> None:
    key = e.L(*parts)
    entries = [r._root_entry(key, b) for b in ([b'{}', b'{}\n'], [b'{}\n', b'{}'], [b'{}', b'{}\n', b'{}'])]
    assert entries[0] == entries[1] == entries[2]
    assert len({r.entry_root([entry], stream=stream) for entry in entries}) == 1
    assert r._root_entry(key, [])['state'] == 'missing'
    assert r._root_entry(key, [b'{}', b'{}'])['state'] == 'present'
    with pytest.raises(ValueError):
        r.entry_root([entries[0], entries[0]], stream=stream)


def test_typed_tuple_order_and_exact_encoding() -> None:
    entries = [r._root_entry(e.L('2026-10-12', 'w1', n), [b'{}']) for n in ('10', '2')]
    assert r.entry_root(entries, stream='publisher_slot_trace_sha256') == s.sha256(list(reversed(entries)))
    for key in ('01:x', '1:x/', '0:', '1:x/2:y', '1:x/01:y', '513:' + 'x' * 513):
        with pytest.raises(ValueError):
            e.decode_key(key)
    with pytest.raises(ValueError):
        r.entry_root([r._root_entry(e.L('2026-10-12', 'w1', '01'), [b'{}'])], stream='publisher_slot_trace_sha256')


def test_clock_sentinel_and_original_seed() -> None:
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    args['report'].update(clock_start_us=1, first_seed_at_us=1, elapsed_seconds=1)
    args['output'].update(clock_start_us=1, elapsed_seconds=1, coverage_sha256=s.sha256(args['report']))
    c.validate_gate_output(**args)
    for changes in ({'first_seed_at_us': 2}, {'elapsed_seconds': 0}, {'clock_start_us': 2}):
        bad = copy.deepcopy(args)
        bad['report'].update(changes)
        bad['output'].update(clock_start_us=bad['report']['clock_start_us'], elapsed_seconds=bad['report']['elapsed_seconds'], coverage_sha256=s.sha256(bad['report']))
        with pytest.raises(ValueError):
            c.validate_gate_output(**bad)


def test_nullable_report_completeness_and_not_exercised() -> None:
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    report = args['report']
    for field in ('admission', 'comparator'):
        report[field] = {k: 0 for k in report[field] if k != 'coverage'} | {'coverage': {'state': 'not_exercised', 'numerator': 0, 'denominator': 0}}
    report['partition'] = dict.fromkeys(report['partition'], 0)
    args['output']['coverage_sha256'] = s.sha256(report)
    c.validate_gate_output(**args)
    report['partition']['T'] = None
    args['output']['coverage_sha256'] = s.sha256(report)
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)


@pytest.mark.parametrize('value', [None, 1, True, 1.0, '', {}, [None]])
def test_functions_total_over_json(value: Any) -> None:
    for run in (lambda: r.entry_root(value, stream='failures'), lambda: r.validate_detail(value, [], stream='failures'),
                lambda: c.display_failure(value), lambda: e.Q(value), lambda: e.decode_key(value)):
        with pytest.raises(ValueError):
            run()
    with pytest.raises(ValueError):
        c.validate_preimages(value, {}, artifacts={}, roots={})


@exhaustive
def test_reordered_self_consistent_chain_rejected() -> None:
    entries = failure_entries()[:255]
    pages = make_pages(list(reversed(entries)))
    detail = make_detail(entries, pages, 'failures')
    with pytest.raises(ValueError):
        r.validate_detail(detail, pages, stream='failures')


def test_pass_with_count_and_hidden_gap() -> None:
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    failure = obj('gate_failures', 'constructor-artifact_gap')
    pages = make_pages([failure])
    args['failures'] = pages
    args['report']['failures'] = make_detail([failure], pages, 'failures')
    args['report']['failure_counts']['artifact_gap'] = {'count': 1, 'listed': 1, 'complete': True, 'day_counts': [1]}
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)


@exhaustive
@pytest.mark.parametrize('name', sorted(SEEDS))
def test_every_array_count_limit_independent_of_bytes(name: str) -> None:
    schema = SCHEMAS[name]
    for field, check in schema.fields.items():
        if getattr(check, '__qualname__', '') != 'array.<locals>.validate':
            continue
        cells = dict(zip(check.__code__.co_freevars, (cell.cell_contents for cell in check.__closure__), strict=True))
        maximum = cells['maximum']
        child = cells['check']
        values = SEEDS[name][field]
        if values:
            seed = values[0]
        elif isinstance(child, s.Schema):
            seed = next(v for n, v in SEEDS.items() if SCHEMAS[n] is child)
        else:
            for candidate in (0, '2026-10-12', 'a' * 64, 'region'):
                try:
                    child(candidate)
                except ValueError:
                    continue
                seed = candidate
                break
            else:
                raise AssertionError((name, field))
        assert not cells['unique'], (name, field)
        check([seed] * maximum)
        with pytest.raises(ValueError):
            check([seed] * (maximum + 1))


def test_nullable_shutdown_refs_and_grace() -> None:
    entry = obj('shutdown_inventory', 'missing-close')
    for value in (None, obj('external_proofs', 'file')):
        for field in ('original_close', 'close_result'):
            assert_round_trip(e.SHUTDOWN_ENTRY, {**entry, field: value})
    assert_round_trip(e.SHUTDOWN_ENTRY, {**entry, 'termination_applies': False, 'grace_us': None})


def test_event_subject_uses_verified_coordinates() -> None:
    from tests.test_async_settle_shadow_v2_a2 import vector
    event = vector('gate_failures', 'subject-E')['event']
    with pytest.raises(ValueError):
        e.E('2026-10-11', 'w1', '00000000000000000001', event['seq'], event=event)
    with pytest.raises(ValueError):
        e.E('2026-10-12', 'w1', '00000000000000000001', '00000000000000000099', event=event)
    with pytest.raises(ValueError):
        e.I('2026-10-12', 'w1', '00000000000000000001', 128)
    assert e.I('2026-10-12', 'w1', '00000000000000000001', 127)['object']['key'].endswith('/3:127')


@exhaustive
def test_preimage_coverage_containers_total_over_json() -> None:
    from tests.test_async_settle_shadow_v2_a2 import vector
    original = vector('root_preimages', 'all-defined-preimages-empty')['object']
    for field in (*c.COVERAGE_PREIMAGES, *c.CROSS_DAY_PREIMAGES):
        for bad in (None, True, 1, 1.0, '', {}, [None]):
            args = copy.deepcopy(original)
            args['artifacts'] = {k: bytes.fromhex(v) for k, v in args.pop('artifacts_hex').items()}
            args['roots'][field] = bad
            with pytest.raises(ValueError):
                c.validate_preimages(**args)
        if field in c.CROSS_DAY_PREIMAGES:
            for bad in (None, True, 1, '', {}):
                args = copy.deepcopy(original)
                args['artifacts'] = {k: bytes.fromhex(v) for k, v in args.pop('artifacts_hex').items()}
                args['roots'][field] = [{'day': '2026-10-12', 'pages': bad}]
                with pytest.raises(ValueError):
                    c.validate_preimages(**args)


def test_preimage_single_lf_framing_and_original_object_transport() -> None:
    value = obj('external_proofs', 'file')
    raw = canonical(value)
    assert c._object_bytes(raw + b' ' * (e.FILE_REF.limit - len(raw)) + b'\n', e.FILE_REF) == value
    with pytest.raises(ValueError):
        c._object_bytes(raw + b' ' * (e.FILE_REF.limit - len(raw) + 1) + b'\n', e.FILE_REF)
    with pytest.raises(ValueError):
        c._object_bytes(raw + b'\n\n', e.FILE_REF)


def test_maximum_subject_bytes() -> None:
    # Independently determine the exact allowed canonical byte boundary; no
    # encoder truncation/hash substitution of a too-large logical key.
    value = obj('gate_failures', 'subject-O')
    value['object'].update(kind='k' * 128, key='')
    size = 768 - len(canonical(value))
    assert 1 <= size < 512
    value['object']['key'] = 'x' * size
    assert len(canonical(value)) == 768
    assert_round_trip(e.FAILURE_SUBJECT, value)
    value['object']['key'] += 'x'
    with pytest.raises(ValueError):
        e.FAILURE_SUBJECT(value)
    with pytest.raises(ValueError):
        e.FAILURE_SUBJECT.parse(canonical(value))


@pytest.mark.parametrize('name,field,limit', [('FILE_REF', 'bytes', 262144),
    ('LOAD_SEGMENT', 'workspace_count', 32), ('FAILURE_TALLY', 'listed', 16)])
def test_bounded_numeric_fields(name: str, field: str, limit: int) -> None:
    schema = SCHEMAS[name]
    value = copy.deepcopy(SEEDS[name])
    value[field] = limit
    if name == 'FAILURE_TALLY':
        value.update(count=16, complete=True)
    assert_round_trip(schema, value)
    value[field] = limit + 1
    with pytest.raises(ValueError):
        schema(value)
    with pytest.raises(ValueError):
        schema.parse(canonical(value))


def test_required_null_counters_cannot_be_unevaluable() -> None:
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    report = args['report']
    report['rules']['G5'] = 'pass'
    for field in ('admission', 'comparator'):
        report[field] = {k: 0 for k in report[field] if k != 'coverage'} | {'coverage': {'state': 'not_exercised', 'numerator': 0, 'denominator': 0}}
    report['partition'] = dict.fromkeys(report['partition'], 0)
    for key in e.TIMINGS[:8]:
        report['timing'][key] = {k: 0 if k in ('count', 'null_count') else None for k in report['timing'][key]}
    report['deltas'] = {'count': 0, 'null_count': 0, 'min_micro': None, 'max_micro': None, 'bins': dict.fromkeys(e.BINS, 0)}
    for key in ('eligibility', 'comparison', 'prediction', 'booking', 'delta'):
        report['null_counts'][key] = 0
    args['output']['coverage_sha256'] = s.sha256(report)
    c.validate_gate_output(**args)
    for key in ('eligibility', 'comparison', 'prediction', 'booking', 'delta'):
        bad = copy.deepcopy(args)
        bad['report']['null_counts'][key] = None
        bad['output']['coverage_sha256'] = s.sha256(bad['report'])
        with pytest.raises(ValueError):
            c.validate_gate_output(**bad)


@exhaustive
@pytest.mark.parametrize('name', sorted(SEEDS))
def test_nullable_nonnull_native_and_transport_agree(name: str) -> None:
    schema, original = SCHEMAS[name], SEEDS[name]
    for field, check in schema.fields.items():
        if getattr(check, '__qualname__', '') != 'nullable.<locals>.validate':
            continue
        primitive = check.__closure__[0].cell_contents
        candidates = [0, 1, '2026-10-12', 'router', 'w1', 'settle', 'a' * 40, 'a' * 64,
                      '00000000000000000001', 'value',
                      {'adapter': None, 'route_type': None, 'streamed': None},
                      {'kind': 'row', 'key': 'row', 'event_seq': None}]
        if isinstance(primitive, s.Schema):
            candidates = [v for n, v in SEEDS.items() if SCHEMAS[n] is primitive] or candidates[-2:]
        for candidate in candidates:
            try:
                primitive(candidate)
            except ValueError:
                continue
            value = {**original, field: candidate}
            try:
                schema(value)
            except ValueError:
                with pytest.raises(ValueError):
                    schema.parse(canonical(value))
            else:
                assert_round_trip(schema, value)
            break
        else:
            raise AssertionError((name, field))


def test_cross_day_shape_native_and_bounds() -> None:
    value = {'day': '2026-10-12', 'root': s.sha256([]), 'count': s.INT64}
    assert_round_trip(r.DAY_ROOT, value)
    for bad in (None, True, 1.0, s.INT64 + 1):
        with pytest.raises(ValueError):
            r.DAY_ROOT({**value, 'count': bad})
    with pytest.raises(ValueError):
        r.DAY_ROOT.parse(canonical(value) + b' ' * (129 - len(canonical(value))))


# Independent §7.5.1/§8 transcription. None of these expected paths is read
# from PASS_AGGREGATES or production schema fields.
DESIGN_RULE_PATHS = {
    'G1': '', 'G2': '',
    'G3': 'attempts.authorize_fresh attempts.authorize_replay attempts.terminal_original attempts.terminal_retry attempts.completion',
    'G4': 'partition.T partition.A partition.B partition.C null_counts.comparison',
    'G5': 'comparator.eligible comparator.compared comparator.exact comparator.explained comparator.mismatch comparator.unevaluable admission.E admission.Y admission.N admission.U partition.T partition.A partition.B partition.C timing.authorize_shadow_us.count timing.handoff_prepare_us.count timing.router_settle_us.count timing.booking_confirm_us.count timing.comparator_us.count timing.evidence_write_us.count timing.workspace_age_us.count timing.health_age_us.count deltas.count null_counts.eligibility null_counts.comparison null_counts.prediction null_counts.booking null_counts.delta',
    'G6': 'publisher.scheduled_slots publisher.claim_attempts publisher.claims publisher.lost_claims publisher.scans publisher.publications publisher.failures publisher.unknown_outcomes publisher.late_cycles publisher.missed_slots timing.publisher_cycle_us.count timing.publisher_slot_rpc_elapsed_us.count',
    'G7': 'fleet_peaks.simultaneous_routers fleet_peaks.simultaneous_publishers fleet_peaks.configured_routers fleet_peaks.configured_publishers fleet_peaks.startup_operations_1s fleet_peaks.close_operations_1s fleet_peaks.export_operations_1s fleet_peaks.reads_per_second fleet_peaks.pending_rows_per_second fleet_peaks.trust_rows_per_second fleet_peaks.health_rows_per_second fleet_peaks.claim_operations_per_second fleet_peaks.scan_operations_per_second fleet_peaks.publication_operations_per_second fleet_peaks.export_operations_per_second fleet_peaks.export_bytes_per_second fleet_peaks.configured_reads_per_second fleet_peaks.configured_pending_rows_per_second fleet_peaks.cpu_percent',
    'G8': 'checkpoint.attempts checkpoint.failures checkpoint.recoveries checkpoint.unknown_commits checkpoint.unreconciled_commits checkpoint.irrecoverable_losses checkpoint.evidence_loss handoff.expected handoff.landed handoff.attempts_lower handoff.attempts_upper handoff.failures_lower handoff.failures_upper handoff.unknown_reply_upper timing.close_us.count timing.handoff_us.count timing.drain_us.count',
    'G9': '', 'G10': 'first_seed_at_us clock_start_us elapsed_seconds', 'G11': '',
}
DESIGN_REQUIRED_RULES = {
    'transport': ('G1',), 'fleet-budgets-pre': ('G1', 'G7'),
    'fleet-budgets': ('G1', 'G2', 'G5', 'G6', 'G7', 'G8'),
    'shadow-serving': ('G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9', 'G10'),
    'pre-flip': ('G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9', 'G10', 'G11'),
    'post-flip': ('G1', 'G11'),
}


@pytest.mark.parametrize('stage', DESIGN_REQUIRED_RULES)
def test_stage_rule_availability_table(stage: str) -> None:
    assert c.REQUIRED_RULES == DESIGN_REQUIRED_RULES
    expected = {rule: tuple(paths.split()) for rule, paths in DESIGN_RULE_PATHS.items()}
    if stage in ('fleet-budgets', 'shadow-serving', 'pre-flip'):
        expected['G7'] += tuple(path.replace('fleet_peaks.', 'publisher.peaks.') for path in DESIGN_RULE_PATHS['G7'].split())
        expected['G7'] += ('timing.close_us.count', 'timing.handoff_us.count', 'timing.drain_us.count')
    assert c.PASS_AGGREGATES[stage] == expected
    # Prove every path is enforced for every passing rule, even outside PASS.
    for rule, paths in expected.items():
        for path in paths:
            report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
            report['gate'] = stage
            report['rules'] = dict.fromkeys(report['rules'], 'not_required')
            report['rules'][rule] = 'pass'
            for other in paths:
                node = report
                parts = other.split('.')
                for part in parts[:-1]:
                    node = node[part]
                if node[parts[-1]] is None:
                    node[parts[-1]] = 0
            c._pass_aggregates(report)
            node = report
            parts = path.split('.')
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = None
            with pytest.raises(ValueError):
                c._pass_aggregates(report)


def test_regional_partition_preserves_unknown_and_checks_overflow() -> None:
    report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
    region = obj('region_reports', 'region')
    # Unknown regional identity must not turn known fleet observations into zero.
    report['timing']['close_us']['count'] = 1
    report['timing']['handoff_us']['count'] = 1
    c._regional_metrics(report, [region])
    region['timing'] = copy.deepcopy(report['timing'])
    c._regional_metrics(report, [region])
    report['timing']['close_us']['count'] = None
    report['timing']['handoff_us']['count'] = None
    c._regional_metrics(report, [region])
    region['timing']['close_us']['count'] = s.INT64
    region['timing']['handoff_us']['count'] = s.INT64
    with pytest.raises(ValueError):
        c._regional_metrics(report, [region, region])


def test_fallback_N_requires_G1_and_incomplete_but_intrinsic_run_does_not() -> None:
    assert e.RUN_SCOPED_FAILURES == { 'evidence_export_budget_gap', 'positive_seed_gap', 'continuous_window_gap'}
    args = obj('gate_outputs_a2', 'complete-listed-fallback-N')
    artifact = obj('gate_failures', 'constructor-artifact_gap')
    entries = [artifact, *args['failures'][0]['entries']]
    pages = make_pages(entries)
    args['failures'] = pages
    args['report']['failures'] = make_detail(entries, pages, 'failures')
    args['report']['failure_counts']['artifact_gap'] = {'count': 1, 'listed': 1, 'complete': True, 'day_counts': [1]}
    args['output']['gaps'] = ['artifact_gap', 'writer_closure_gap']
    args['report']['complete'] = False
    args['report']['rules']['G1'] = 'unevaluable'
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    c.validate_gate_output(**args)
    args['report']['rules']['G1'] = 'pass'
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    failure = obj('gate_failures', 'constructor-evidence_export_budget_gap')
    pages = make_pages([failure])
    args['failures'] = pages
    args['report']['failures'] = make_detail([failure], pages, 'failures')
    args['report']['failure_counts'][failure['gap']] = {'count': 1, 'listed': 1, 'complete': True, 'day_counts': [1]}
    args['report']['rules']['G7'] = 'unevaluable'
    args['output'].update(gaps=[failure['gap']], verdict='BLOCKED', coverage_sha256=s.sha256(args['report']))
    c.validate_gate_output(**args)


def test_reset_history_common_64_page_erratum() -> None:
    from pathlib import Path

    design = (Path(__file__).parents[1] / 'docs/design/async-settle-shadow-v2.md').read_text()
    assert 'history points to RESET_ENTRY entries in common ATTACHMENT_PAGE envelopes (64 entries/page, ≤132,096 bytes/page; every nonfinal page has 64 entries).' in design
    reset = obj('resolution_dag', 'reset')
    entries = [{**reset, 'at_us': i} for i in range(128)]
    for entry in entries:
        e.RESET_ENTRY(entry)
    page = make_pages(entries)[0]
    with pytest.raises(ValueError):
        e.ATTACHMENT_PAGE(page)
    pages = make_pages(entries, 64)
    root = 'b19202f484e1c13b9cfc8bc2025706083c4b1ed70ca609dde049fece4e65517d'
    assert r.entry_root(entries, stream='reset_history') == root
    detail = {'root': root, 'count': 128, 'pages': 2, 'tail_sha256': s.sha256(pages[-1])}
    r.validate_detail(detail, pages, stream='reset_history')


@pytest.mark.parametrize('qualified,parts,bad_index,bad_value', [
    ('manifest.counter_root', ('2026-10-12', 'issuer'), 1, 'absent'),
    ('manifest.counter_root', ('2026-10-12', 'issuer'), 1, 'enclave-only'),
    ('manifest.closure_root', ('2026-10-12', 'issuer'), 1, 'enclave-only'),
    ('manifest.receipts_root', ('2026-10-12', 'issuer', '00000000000000000001'), 1, 'enclave-only'),
    ('manifest.closure_root', ('2026-10-12', 'issuer'), 1, 'absent'),
    ('manifest.receipts_root', ('2026-10-12', 'issuer', '00000000000000000001'), 1, 'absent'),
    ('proof.publisher_slot_trace_sha256', ('2026-10-12', 'issuer', '0'), 0, '2026-10-11'),
    ('proof.publisher_slot_trace_sha256', ('2026-10-12', 'issuer', '0'), 1, 'absent'),
    *[(owner + '.' + stream, ('router', 'issuer', 'a' * 40, 'us-central1', '0'), index, value)
      for owner, stream in [('manifest', 'prerequisite_ledger_root'), ('proof', 'ledger_root')]
      for index, value in [(0, 'enclave'), (1, 'absent'), (2, 'b' * 40), (3, 'wrong-region')]],
])
def test_preimage_root_identities(qualified: str, parts: tuple[str, ...], bad_index: int, bad_value: str) -> None:
    args = obj('root_preimages', 'all-defined-preimages-empty')
    args['artifacts'] = {k: bytes.fromhex(raw) for k, raw in args.pop('artifacts_hex').items()}
    args['manifest']['proof_manifest_sha256'] = None
    args['manifest']['enclave_writers']['enclave-only'] = copy.deepcopy(args['manifest']['router_writers']['issuer'])
    owner, field = qualified.split('.')
    for bad in (False, True):
        components = list(parts)
        if bad:
            components[bad_index] = bad_value
        key = '/'.join(str(len(part)) + ':' + part for part in components)
        args['roots'][qualified] = [{'key': key, 'bodies': []}]
        args[owner][field] = s.sha256([{'key': key, 'bytes': 0, 'sha256': None, 'state': 'missing'}])
        if bad:
            with pytest.raises(ValueError):
                c.validate_preimages(**args)
        else:
            c.validate_preimages(**args)


@pytest.mark.parametrize('stream', ['close_result', 'publisher_trace', 'health_basis', 'handoff_accounting', 'authorization_obligation'])
def test_cross_day_root_binds_proof_days(stream: str) -> None:
    args = obj('root_preimages', 'all-defined-preimages-empty')
    args['artifacts'] = {k: bytes.fromhex(raw) for k, raw in args.pop('artifacts_hex').items()}
    args['manifest']['proof_manifest_sha256'] = None
    for day, rejected in [('2026-10-12', False), ('2026-10-11', True)]:
        args['roots']['proof.' + stream + '_root'] = [{'day': day, 'pages': []}]
        args['proof'][stream + '_root'] = s.sha256([{'day': day, 'root': s.sha256([]), 'count': 0}])
        if rejected:
            with pytest.raises(ValueError):
                c.validate_preimages(**args)
        else:
            c.validate_preimages(**args)


def _review_preargs() -> Any:
    a = obj('root_preimages', 'all-defined-preimages-empty')
    a['artifacts'] = {k: bytes.fromhex(v) for k, v in a.pop('artifacts_hex').items()}
    a['manifest']['proof_manifest_sha256'] = None
    return a


def test_file_digest_retains_lf() -> None:
    assert r.file_digest(b'{}') != r.file_digest(b'{}\n')


def test_conflict_sums_original_bytes() -> None:
    assert r._root_entry('10:2026-10-12/2:w1', [b'{}', b'{}\n'])['bytes'] == 5


def test_conflict_deduplicates_exact_exports() -> None:
    a = r._root_entry('10:2026-10-12/2:w1', [b'{}', b'{}\n'])
    assert r._root_entry(a['key'], [b'{}', b'{}\n', b'{}']) == a
    assert a['state'] == 'conflict'


def test_root_rejects_duplicate_logical_keys() -> None:
    v = r._root_entry('10:2026-10-12/2:w1', [b'{}'])
    with pytest.raises(ValueError):
        r.entry_root([v, v], stream='counter_root')


def test_root_sorts_numeric_slot_as_integer() -> None:
    vals = [r._root_entry(e.L('2026-10-12', 'w1', n), [b'{}']) for n in ['2', '10']]
    assert r.entry_root(vals, stream='publisher_slot_trace_sha256') == s.sha256(vals)


def test_invalid_receipt_position_zero_based() -> None:
    assert e.I('2026-10-12', 'w1', '00000000000000000001', 0)['object']['key'].endswith('/1:0')


def test_rational_reduced() -> None:
    with pytest.raises(ValueError):
        e.RATIONAL(dict(numerator=2, denominator=4))


def test_tally_complete_iff_all_listed() -> None:
    with pytest.raises(ValueError):
        e.FAILURE_TALLY(dict(count=17, listed=16, complete=True, day_counts=[17]))


def test_day_tally_vector_matches_days() -> None:
    v = obj('gate_report', 'pre-enable-null-populations')
    v['failure_counts']['artifact_gap']['day_counts'] = []
    with pytest.raises(ValueError):
        e.GATE_REPORT(v)


def test_post_budget_link_matches_pre() -> None:
    a = _review_preargs()
    k = 'proof.post_budget_sha256'
    v = json.loads(a['artifacts'][k])
    v['pre_envelope_sha256'] = 'f' * 64
    a['artifacts'][k] = canonical(v)
    a['proof']['post_budget_sha256'] = s.sha256(v)
    with pytest.raises(ValueError):
        c.validate_preimages(**a)


def test_file_ref_length_matches_exact_bytes() -> None:
    a = _review_preargs()
    k = 'manifest.independent_inventory_sha256'
    v = json.loads(a['artifacts'][k])
    v['serving']['bytes'] += 1
    a['artifacts'][k] = canonical(v)
    a['manifest']['independent_inventory_sha256'] = s.sha256(v)
    with pytest.raises(ValueError):
        c.validate_preimages(**a)


def test_detail_tail_is_committed() -> None:
    f = obj('gate_failures', 'constructor-writer_closure_gap')
    p = dict(v=2, policy_version='shadow-v2', contract_revision='a2', page_seq='00000000000000000001', previous_sha256=None, entries=[f])
    d = dict(root=r.entry_root([f], stream='failures'), count=1, pages=1, tail_sha256='f' * 64)
    with pytest.raises(ValueError):
        r.validate_detail(d, [p], stream='failures')



def test_union_ledger_binds_instance_region_mapping() -> None:
    args = _review_preargs()
    key = '6:router/6:issuer/40:' + 'a' * 40 + '/11:us-central1/1:0'
    args['roots']['proof.ledger_root'] = [{'key': key, 'bodies': []}]
    args['proof']['ledger_root'] = s.sha256([{'key': key, 'bytes': 0, 'sha256': None, 'state': 'missing'}])
    c.validate_preimages(**args)
    args['proof']['instance_regions_by_day']['2026-10-12']['issuer'] = 'wrong-region'
    with pytest.raises(ValueError):
        c.validate_preimages(**args)


@pytest.mark.parametrize('stage', DESIGN_REQUIRED_RULES)
def test_all_aggregate_residences(stage: str) -> None:
    """Independent per-rule residence matrix, with deletion of every member."""
    for rule, declared in DESIGN_RULE_PATHS.items():
        gate = tuple(declared.split())
        actual = stage in ('fleet-budgets', 'shadow-serving', 'pre-flip')
        close = ('timing.close_us.count', 'timing.handoff_us.count', 'timing.drain_us.count')
        if rule == 'G7':
            region = tuple(path.replace('fleet_peaks.', 'peaks.') for path in gate)
            if actual:
                gate += tuple(path.replace('peaks.', 'publisher.peaks.') for path in region) + close
                region += close
        else:
            region = tuple(path for path in gate if path.startswith('timing.'))
        writer = tuple(path for path in gate if path.split('.')[0] in
                       ('comparator', 'admission', 'partition', 'attempts', 'checkpoint'))
        expected = {'GATE_REPORT': gate, 'REGION_REPORT': region, 'WRITER_REPORT': writer}
        assert c.PASS_AGGREGATE_LOCATIONS[stage][rule] == expected
        report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
        report['gate'] = stage
        report['rules'] = dict.fromkeys(report['rules'], 'not_required')
        report['rules'][rule] = 'pass'
        report['attempts'] = dict.fromkeys(report['attempts'], 0)
        for field in ('first_seed_at_us', 'clock_start_us', 'elapsed_seconds'):
            report[field] = 1
        reg = obj('region_reports', 'region')
        reg.update(peaks=copy.deepcopy(report['fleet_peaks']), timing=copy.deepcopy(report['timing']))
        wr = obj('writer_reports', 'unknown-region')
        for field in ('comparator', 'admission', 'partition', 'attempts', 'checkpoint'):
            wr[field] = copy.deepcopy(report[field])
        records = {'GATE_REPORT': report, 'REGION_REPORT': reg, 'WRITER_REPORT': wr}
        c._pass_aggregates(report, [reg], [wr])
        for location, paths in expected.items():
            for path in paths:
                node = records[location]
                parts = path.split('.')
                for part in parts[:-1]:
                    node = node[part]
                original = node[parts[-1]]
                node[parts[-1]] = None
                with pytest.raises(ValueError):
                    c._pass_aggregates(report, [reg], [wr])
                node[parts[-1]] = original


@pytest.mark.parametrize('stage', DESIGN_REQUIRED_RULES)
def test_regional_G7_pass_in_BLOCKED_report(stage: str) -> None:
    args = obj('gate_outputs_a2', 'regional-G7-' + stage)
    failure = obj('gate_failures', 'constructor-artifact_gap')
    args['failures'] = make_pages([failure])
    args['report']['failures'] = make_detail([failure], args['failures'], 'failures')
    args['report']['failure_counts']['artifact_gap'] = {'count': 1, 'listed': 1, 'complete': True, 'day_counts': [1]}
    args['report']['rules']['G1'] = 'fail'
    args['output'].update(gaps=['artifact_gap'], verdict='BLOCKED')
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    c.validate_gate_output(**args)
    args['regions'][0]['entries'][0]['peaks']['configured_publishers'] = None
    args['report']['regions'] = make_detail(args['regions'][0]['entries'], args['regions'], 'regions')
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)


@pytest.mark.parametrize('key', ['publisher_slot_rpc_elapsed_us', 'close_us', 'handoff_us', 'drain_us'])
def test_regional_required_values_have_no_null_observations(key: str) -> None:
    report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
    region = obj('region_reports', 'region')
    region['timing'] = copy.deepcopy(report['timing'])
    # Fleet unavailability cannot mask a malformed regional population.
    report['timing'] = {name: dict.fromkeys(stat) for name, stat in report['timing'].items()}
    c._regional_metrics(report, [region])
    region['timing'][key]['null_count'] = 1
    with pytest.raises(ValueError):
        c._regional_metrics(report, [region])
    region['timing'] = copy.deepcopy(report['timing'])
    c._regional_metrics(report, [region])


@pytest.mark.parametrize('defect', ['S', 'J', 'self-timing', 'H', 'Hterm'])
def test_common_regional_timing_population_invariants(defect: str) -> None:
    report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
    region = obj('region_reports', 'region')
    region['timing'] = copy.deepcopy(report['timing'])
    report['timing'] = {name: dict.fromkeys(stat) for name, stat in report['timing'].items()}
    key, field = {'S': ('authorize_shadow_us', 'null_count'), 'J': ('publisher_cycle_us', 'null_count'),
                  'self-timing': ('evidence_write_us', 'count'), 'H': ('close_us', 'count'),
                  'Hterm': ('drain_us', 'count')}[defect]
    region['timing'][key][field] = 1
    with pytest.raises(ValueError):
        c._regional_metrics(report, [region])


def test_closed_empty_regions_still_reconcile() -> None:
    report = obj('gate_outputs_a2', 'closed-actual-empty')['report']
    c._regional_metrics(report, [])
    report['timing']['close_us']['count'] = report['timing']['handoff_us']['count'] = 1
    with pytest.raises(ValueError):
        c._regional_metrics(report, [])
    writer = obj('writer_reports', 'unknown-region')
    c._regional_metrics(report, [], [writer])
    writer['region'] = 'us-central1'
    with pytest.raises(ValueError):
        c._regional_metrics(report, [], [writer])
    report['failure_counts']['regional_roster_unknown']['count'] = 17
    c._regional_metrics(report, [], [writer])


@pytest.mark.parametrize('serialized', [False, True])
def test_sorted_retained_tools_need_not_be_unique(serialized: bool) -> None:
    value = obj('verifier_ports', 'repeated-sorted-tool')
    assert value['retained_tools'][0] == value['retained_tools'][1]
    if serialized:
        e.VERIFIERS.parse(canonical(value))
    else:
        e.VERIFIERS(value)


@pytest.mark.parametrize('path', ['evidence//source.json', 'evidence/./source.json'])
@pytest.mark.parametrize('serialized', [False, True])
def test_relative_file_keys_need_not_be_normalized(path: str, serialized: bool) -> None:
    value = {'path': path, 'sha256': 'a' * 64, 'bytes': 0}
    if serialized:
        e.FILE_REF.parse(canonical(value))
    else:
        e.FILE_REF(value)


@pytest.mark.parametrize('gap', [row[0] for row in generator.rules()])
def test_every_N_failure_completeness(gap: str) -> None:
    # Names/producers are transcribed from the design by the offline generator,
    # never from FAILURE_RULES or RUN_SCOPED_FAILURES.
    producer = next(row[1].split()[0] for row in generator.rules() if row[0] == gap)
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    names = sorted({gap, 'artifact_gap'})
    failures = [{'v': 2, 'policy_version': 'shadow-v2', 'contract_revision': 'a2',
                 'gap': name, 'subject': generator.subject('run'), 'count': 1} for name in names]
    args['failures'] = make_pages(failures)
    args['report']['failures'] = make_detail(failures, args['failures'], 'failures')
    for name in names:
        args['report']['failure_counts'][name] = {'count': 1, 'listed': 1, 'complete': True, 'day_counts': [1]}
    args['report']['rules'][producer] = 'fail'
    args['report']['rules']['G1'] = 'fail'
    args['report']['complete'] = False
    args['output'].update(gaps=names, verdict='BLOCKED', coverage_sha256=s.sha256(args['report']))
    c.validate_gate_output(**args)
    args['report']['complete'] = True
    if gap in ('evidence_export_budget_gap', 'positive_seed_gap', 'continuous_window_gap'):
        failures = [failure for failure in failures if failure['gap'] == gap]
        args['failures'] = make_pages(failures)
        args['report']['failures'] = make_detail(failures, args['failures'], 'failures')
        args['report']['failure_counts']['artifact_gap'] = {'count': 0, 'listed': 0, 'complete': True, 'day_counts': [0]}
        args['report']['rules']['G1'] = 'pass'
        args['output']['gaps'] = [gap]
        args['output']['coverage_sha256'] = s.sha256(args['report'])
        c.validate_gate_output(**args)
    else:
        args['output']['coverage_sha256'] = s.sha256(args['report'])
        with pytest.raises(ValueError):
            c.validate_gate_output(**args)


@pytest.mark.parametrize('serialized', [False, True])
def test_stat_components_need_not_be_jointly_available(serialized: bool) -> None:
    value = obj('gate_report', 'independently-known-percentile')
    if serialized:
        e.STAT.parse(canonical(value))
    else:
        e.STAT(value)


@pytest.mark.parametrize('field', ['comparator', 'admission'])
def test_writer_ratio_uses_same_population_invariant(field: str) -> None:
    args = obj('gate_outputs_a2', 'regional-G7-fleet-budgets')
    writer = obj('writer_reports', 'unknown-region')
    writer['region'] = 'us-central1'
    for key in ('comparator', 'admission', 'partition', 'attempts', 'checkpoint'):
        writer[key] = copy.deepcopy(args['report'][key])
    args['writers'] = [{'v': 2, 'policy_version': 'shadow-v2', 'contract_revision': 'a2',
                        'page_seq': '00000000000000000001', 'previous_sha256': None, 'entries': [writer]}]
    args['report']['writers'] = make_detail([writer], args['writers'], 'writers')
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    c.validate_gate_output(**args)
    writer[field]['coverage'] = {'state': 'evaluated', 'numerator': 0, 'denominator': 1}
    args['report']['writers'] = make_detail([writer], args['writers'], 'writers')
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)
