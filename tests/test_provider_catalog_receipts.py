from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from scripts.pricing.provider_contract_catalog import (
    discover_provider_contract_catalog,
)
from trusted_router.provider_contract import (
    PROVIDER_CATALOG_EXAMPLE,
    PROVIDER_CATALOG_SCHEMA,
    PROVIDER_CATALOG_V2_EXAMPLE,
    PROVIDER_CATALOG_V2_SCHEMA,
)


def _v2_payload() -> dict[str, Any]:
    return copy.deepcopy(PROVIDER_CATALOG_V2_EXAMPLE)


def _validate_with_both_contracts(payload: dict[str, Any]) -> None:
    Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(payload)
    discover_provider_contract_catalog(payload, upstream_id_map={})


def test_v2_receipts_capability_is_valid() -> None:
    payload = _v2_payload()

    _validate_with_both_contracts(payload)

    assert payload["data"][0]["capabilities"]["receipts"] == {
        "spec": "inference-receipt/1",
        "algorithms": ["EdDSA"],
        "delivery": ["header", "stream-chunk"],
    }


def test_v2_receipts_capability_rejects_unknown_keys() -> None:
    payload = _v2_payload()
    payload["data"][0]["capabilities"]["receipts"]["unexpected"] = True

    with pytest.raises(ValidationError):
        Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(payload)
    with pytest.raises(RuntimeError, match="receipts fields invalid"):
        discover_provider_contract_catalog(payload, upstream_id_map={})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec", "inference-receipt/2"),
        ("algorithms", ["ES256"]),
        ("delivery", ["body"]),
    ],
)
def test_v2_receipts_capability_rejects_unknown_vocabulary(
    field: str,
    value: object,
) -> None:
    payload = _v2_payload()
    payload["data"][0]["capabilities"]["receipts"][field] = value

    with pytest.raises(ValidationError):
        Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(payload)
    with pytest.raises(RuntimeError, match="unsupported"):
        discover_provider_contract_catalog(payload, upstream_id_map={})


def test_v2_receipts_capability_is_optional_and_v1_remains_frozen() -> None:
    payload = _v2_payload()
    del payload["data"][0]["capabilities"]["receipts"]

    _validate_with_both_contracts(payload)

    v2_capabilities = PROVIDER_CATALOG_V2_SCHEMA["$defs"]["model"]["properties"][
        "capabilities"
    ]
    v1_capabilities = PROVIDER_CATALOG_SCHEMA["$defs"]["model"]["properties"][
        "capabilities"
    ]
    assert "receipts" not in v2_capabilities["required"]
    assert "receipts" not in v1_capabilities["properties"]
    assert "receipts" not in PROVIDER_CATALOG_EXAMPLE["data"][0]["capabilities"]


def test_v1_continues_to_reject_receipts() -> None:
    payload = copy.deepcopy(PROVIDER_CATALOG_EXAMPLE)
    payload["data"][0]["capabilities"]["receipts"] = {
        "spec": "inference-receipt/1",
        "algorithms": ["EdDSA"],
        "delivery": ["header", "stream-chunk"],
    }

    with pytest.raises(ValidationError):
        Draft202012Validator(PROVIDER_CATALOG_SCHEMA).validate(payload)
    with pytest.raises(RuntimeError, match="capabilities fields invalid"):
        discover_provider_contract_catalog(payload, upstream_id_map={})
