"""Session-local pure lookup caches; retained/verifier calls always execute.

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


@pytest.fixture(scope='session', autouse=True)
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

    patch.setattr(Schema, '_validate', validated)
    patch.setattr(Schema, 'parse', parsed)
    patch.setattr(contract, '_object_bytes', object_bytes)
    patch.setattr(payloads, 'retained_json', retained_json)
    patch.setattr(payloads.PayloadGraph, 'detail', indexed)
    yield dict(schema=schema_result, objects=checked, lookups=looked_up, indexes=indexes, parse=schema_parse)
    patch.undo()
    validations.clear()
    schema_result.cache_clear()
    checked.cache_clear()
    looked_up.cache_clear()
    indexes.clear()
