"""Native-object fast paths for P3; wire decoders remain the evidence boundary.

These schemas contain only exact JSON types and ASCII token strings. Validate
those types and schema invariants directly, compute exact canonical sizes from
children, and encode the EVENT once. Never use this for retained-string schemas.
"""
from __future__ import annotations

import re
from typing import Any

from trusted_router.async_settle_shadow_v2_contract import (
    CANDIDATE,
    DEPENDENT,
    FEATURE_FLAGS,
    FEATURES,
    REQUESTED_FACTS,
)
from trusted_router.async_settle_shadow_v2_evidence import (
    AUTHORIZE_EVENT,
    BOOKING,
    BUCKET,
    DECISION_REFERENCE,
    DIMENSIONS,
    PREDICTION,
    RECEIPT_REFERENCE,
    TERMINAL_EVENT,
)
from trusted_router.async_settle_shadow_v2_schema import COORDINATES, LOCATOR, Schema, require
from trusted_router.detached_jws import canonical

# Explicit nested edges: no introspection of validator closures or dynamic code.
NESTED = {
    "identity": AUTHORIZE_EVENT.fields["identity"], "dimensions": DIMENSIONS,
    "decision": DECISION_REFERENCE, "facts": REQUESTED_FACTS,
    "receipt_locator": COORDINATES, "original_receipt": LOCATOR,
    "booking": BOOKING, "prediction": PREDICTION, "retry_original": RECEIPT_REFERENCE,
    "features": FEATURE_FLAGS,
}
# diagnostic is nullable; its closed schema is small enough to name directly.
DIAGNOSTIC = Schema({"reason": PREDICTION.fields["reason"],
                     "required_bytes": BUCKET.fields["A"], "candidate_count": BUCKET.fields["A"]}, 256)
NESTED["diagnostic"] = DIAGNOSTIC
EDGES = {(id(schema), name): child for schema in (AUTHORIZE_EVENT, TERMINAL_EVENT)
         for name, child in NESTED.items() if name in schema.fields}
EDGES[id(REQUESTED_FACTS), "features"] = FEATURE_FLAGS


_TOKEN = re.compile(r'[ -!#-\[\]-~]*\Z')


def scalar_size(value: Any) -> int:
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        return len(str(value))
    if type(value) is str:
        # Equivalent to the strict decoder's canonical transport alphabet.
        require(_TOKEN.fullmatch(value) is not None, "json_encoding")
        return len(value)+2
    if type(value) is list:
        return 2+max(0, len(value)-1)+sum(scalar_size(v) for v in value)
    raise ValueError("evidence_schema_gap")


def native_size(schema: Schema, value: Any) -> int:
    require(type(value) is dict and value.keys() == schema.fields.keys()
            and all(type(key) is str for key in value))
    size = 2+max(0, len(value)-1)
    for name, check in schema.fields.items():
        child = value[name]
        nested = EDGES.get((id(schema), name))
        if schema is CANDIDATE and name == "features":
            require(type(child) is list and len(child) <= len(FEATURES+DEPENDENT))
            require(all(type(v) is str and v in FEATURES+DEPENDENT for v in child))
            require(len(set(child)) == len(child))
            length = scalar_size(child)
        elif name == "candidates" and child is not None:
            require(type(child) is list and 1 <= len(child) <= 128)
            length = 2+len(child)-1+sum(native_size(CANDIDATE, v) for v in child)
        elif nested is not None and child is not None and not (schema is CANDIDATE and name == "features"):
            assert isinstance(nested, Schema)
            length = native_size(nested, child)
        else:
            check(child)
            length = scalar_size(child)
        size += len(name)+3+length
    require(size <= schema.limit, "evidence_size")
    if schema.invariant is not None:
        schema.invariant(value)
    return size


def event_bytes(value: dict[str, Any]) -> bytes:
    require(type(value) is dict)
    native_size(AUTHORIZE_EVENT if value.get("phase") == "authorize" else TERMINAL_EVENT, value)
    return canonical(value)


def validate_bucket(value: dict[str, Any]) -> None:
    # BUCKET's maximum possible native encoding is below its 2048-byte limit.
    require(type(value) is dict and value.keys() == BUCKET.fields.keys()
            and all(type(key) is str for key in value))
    dims = value["dimensions"]
    require(type(dims) is dict and dims.keys() == DIMENSIONS.fields.keys()
            and all(type(key) is str for key in dims))
    for name, check in DIMENSIONS.fields.items():
        check(dims[name])
    for name, check in BUCKET.fields.items():
        if name != "dimensions":
            check(value[name])
    assert BUCKET.invariant is not None
    BUCKET.invariant(value)
