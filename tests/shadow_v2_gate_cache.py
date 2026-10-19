"""Module-local pure lookup caches; retained/verifier calls always execute.

Only immutable bytes are retained. Every caller receives a fresh decoded object,
so a mutation or a consumer's pop/update cannot poison another graph. Content
and parser identity qualify both acceptance and rejection; graph time, ports,
approvals and FILE_REF resolution are deliberately never cached.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from functools import lru_cache
from typing import Any

import pytest


@pytest.fixture(scope='module', autouse=True)
def default_dependency_lookup_cache(payloadgraph_shared_lookup_cache):
    """Share pure byte decoding in default modules that import this fixture."""
    from trusted_router import async_settle_shadow_v2_gate_dependencies as dependencies
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    original = dependencies.retained_json
    dependencies.retained_json = payloads.retained_json
    try:
        yield
    finally:
        dependencies.retained_json = original
    assert dependencies.retained_json is original


@pytest.fixture(scope='module', autouse=True)
def payloadgraph_shared_lookup_cache():
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    from trusted_router.async_settle_shadow_v2_schema import Schema
    patch = pytest.MonkeyPatch()
    schema_parse = Schema.parse
    schema_validate = Schema._validate
    parse, lookup = contract._object_bytes, payloads.retained_json

    schemas = {}
    validations: OrderedDict[Any, str | None] = OrderedDict()
    def validated(schema, value):
        # __call__ still checks the exact native domain before this boundary;
        # tuple/list, subclass/native and binary64/int cannot alias here.
        from trusted_router.detached_jws import canonical
        schemas[id(schema)] = schema
        key = (id(schema), canonical(value))
        if key not in validations:
            try:
                schema_validate(schema, value)
            except ValueError as exc:
                validations[key] = str(exc)
            else:
                validations[key] = None
            if len(validations) > 16000:
                validations.popitem(last=False)
        else:
            validations.move_to_end(key)
        if validations[key] is not None:
            raise ValueError(validations[key])

    @lru_cache(maxsize=16000)
    def schema_result(schema_id, raw):
        try:
            return json.dumps(schema_parse(schemas[schema_id], raw), separators=(',', ':')), None
        except ValueError as exc:
            return None, str(exc)

    def parsed(schema, raw):
        if type(raw) is not bytes:
            return schema_parse(schema, raw)
        schemas[id(schema)] = schema
        value, error = schema_result(id(schema), raw)
        if error is not None:
            raise ValueError(error)
        return json.loads(value)

    @lru_cache(maxsize=16000)
    def checked(raw, schema_id):
        try:
            value = parse(raw, schemas[schema_id])
        except ValueError as exc:
            return None, str(exc)
        return json.dumps(value, separators=(',', ':')), None

    def object_bytes(raw, schema):
        schemas[id(schema)] = schema
        value, error = checked(raw, id(schema))
        if error is not None:
            raise ValueError(error)
        return json.loads(value)

    @lru_cache(maxsize=16000)
    def looked_up(raw):
        try:
            return json.dumps(lookup(raw), separators=(',', ':')), None
        except ValueError as exc:
            return None, str(exc)

    def retained_json(raw):
        value, error = looked_up(raw)
        if error is not None:
            raise ValueError(error)
        return json.loads(value)

    detail = payloads.PayloadGraph.detail
    indexes: dict[Any, str] = {}
    def indexed(graph, reference, stream):
        schema = payloads.roots.STREAMS[stream][1]
        key = (tuple(graph.files.items()), id(schema))
        if id(schema) not in graph._containers and key in indexes:
            graph._containers[id(schema)] = json.loads(indexes[key])
        try:
            return detail(graph, reference, stream)
        finally:
            if id(schema) in graph._containers and key not in indexes:
                if len(indexes) >= 128:
                    indexes.clear()
                indexes[key] = json.dumps(graph._containers[id(schema)], separators=(',', ':'))

    # Contract graph validators are pure. Memoize complete original-byte
    # arguments, including ordering, absent keys and all dependency bytes.
    # Ports, approvals and semantic evaluation remain outside this cache.
    preimage_checks = contract.validate_preimages
    output_checks = contract.validate_gate_output
    graph_results: OrderedDict[Any, str | None] = OrderedDict()

    def freeze(value):
        if isinstance(value, dict):
            return (type(value), tuple((k, freeze(v)) for k, v in sorted(value.items())))
        if isinstance(value, (list, tuple)):
            return (type(value), tuple(freeze(v) for v in value))
        return (type(value), value)

    def pure_check(function, args, kwargs):
        key = (function, freeze(args), freeze(kwargs))
        if key not in graph_results:
            try:
                function(*args, **kwargs)
            except ValueError as exc:
                graph_results[key] = str(exc)
            else:
                graph_results[key] = None
            if len(graph_results) > 256:
                graph_results.popitem(last=False)
        else:
            graph_results.move_to_end(key)
        if graph_results[key] is not None:
            raise ValueError(graph_results[key])

    def checked_preimages(*args, **kwargs):
        return pure_check(preimage_checks, args, kwargs)

    def checked_output(*args, **kwargs):
        return pure_check(output_checks, args, kwargs)

    originals = [(contract, 'validate_preimages', preimage_checks),
        (contract, 'validate_gate_output', output_checks), (Schema, '_validate', schema_validate),
        (Schema, 'parse', schema_parse), (contract, '_object_bytes', parse),
        (payloads, 'retained_json', lookup), (payloads.PayloadGraph, 'detail', detail)]
    patch.setattr(contract, 'validate_preimages', checked_preimages)
    patch.setattr(contract, 'validate_gate_output', checked_output)
    patch.setattr(Schema, '_validate', validated)
    patch.setattr(Schema, 'parse', parsed)
    patch.setattr(contract, '_object_bytes', object_bytes)
    patch.setattr(payloads, 'retained_json', retained_json)
    patch.setattr(payloads.PayloadGraph, 'detail', indexed)
    yield dict(schema=schema_result, objects=checked, lookups=looked_up, indexes=indexes, parse=schema_parse)
    patch.undo()
    assert all(getattr(owner, name) is original for owner, name, original in originals)
    graph_results.clear()
    validations.clear()
    schema_result.cache_clear()
    checked.cache_clear()
    looked_up.cache_clear()
    indexes.clear()


PACKAGE_MODULES = frozenset('test_async_settle_shadow_v2_' + name + '.py' for name in (
    'gates', 'gate_export', 'gate_a2', 'gate_a3', 'gate_cli', 'gate_loading', 'gate_complete',
    'gate_retained', 'gate_window'))


def live_objects() -> int:
    import gc
    gc.collect()
    return len(gc.get_objects())


def release_data_caches(*, shared: bool = True) -> None:
    """Release large transient graphs; keep small shared originals across modules."""
    from tests import shadow_v2_gate_a2_fixture as a2
    from tests import shadow_v2_gate_fixture_generator as generator
    from trusted_router import async_settle_shadow_v2_gate_preimages as preimages
    from trusted_router import async_settle_shadow_v2_gate_prior as prior

    caches = (a2._transport_object, generator._variants, preimages._entry_digest)
    if shared:
        caches += (a2.fixture_bytes, a2._shared_prior_chain)
    for cached in caches:
        cached.cache_clear()
        assert cached.cache_info().currsize == 0
    # Prior-result keys include module-local parser functions. Those entries
    # cannot be reused by the next module and would retain old closures.
    prior._VERIFIED.clear()
    assert not prior._VERIFIED
    generator.TYPED_PORTS.clear()
    assert not generator.TYPED_PORTS


def reachable_objects() -> int:
    """Count the same module-rooted walk used by the follow-on frame checks."""
    import time

    from tests.fakes.frozen_package import ALIAS, _namespace_roots, _references

    started = time.perf_counter()
    namespaces = (ALIAS, 'trusted_router', 'tests.fakes.spanner')
    count = sum(1 for _ in _references(_namespace_roots(*namespaces), namespaces=namespaces))
    print(f'P5 reference walk: count={count} seconds={time.perf_counter() - started:.6f}')
    return count


def installed_lookups() -> tuple[Any, ...]:
    """Identities that each module must restore before its data is released."""
    import sys

    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a1_health as health
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router import async_settle_shadow_v2_a2_roots as roots
    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    from trusted_router import async_settle_shadow_v2_gate_loading as loading
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    from trusted_router import async_settle_shadow_v2_gate_preimages as preimages
    from trusted_router.async_settle_shadow_v2_schema import Schema

    digests = tuple((name, module.file_digest) for name, module in sorted(sys.modules.items())
                    if name.startswith('trusted_router.async_settle_shadow_v2_gate')
                    and hasattr(module, 'file_digest'))
    assert all(digest is roots.file_digest for _, digest in digests)
    return (roots.file_digest, roots._root_entry, roots.entry_root,
            a1.coverage_root, health.validate_publisher_traces, loader.Row, loader.Preimage,
            contract.validate_preimages, contract.validate_gate_output,
            contract._object_bytes, payloads.retained_json, payloads.PayloadGraph.detail,
            Schema.parse, Schema._validate, Schema._decode, loading.transport_values,
            preimages._entry_digest)


def assert_released_data() -> None:
    from tests import shadow_v2_gate_a2_fixture as a2
    from tests import shadow_v2_gate_fixture_generator as generator
    from trusted_router import async_settle_shadow_v2_gate_preimages as preimages
    from trusted_router import async_settle_shadow_v2_gate_prior as prior

    for cached in (a2._transport_object, generator._variants, preimages._entry_digest):
        assert cached.cache_info().currsize == 0
    assert not prior._VERIFIED
    assert not generator.TYPED_PORTS
