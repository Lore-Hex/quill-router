"""Canonical provider onboarding contract published on the marketplace page."""

from __future__ import annotations

import copy
from typing import Final

PROVIDER_CATALOG_SCHEMA_URL: Final = (
    "https://trustedrouter.com/providers/marketplace/catalog.schema.json"
)

PROVIDER_CATALOG_EXAMPLE: Final[dict[str, object]] = {
    "object": "list",
    "data": [
        {
            "id": "acme/atlas-70b",
            "object": "model",
            "owned_by": "acme",
            "name": "Atlas 70B",
            "type": "chat",
            "context_length": 131072,
            "max_output_tokens": 16384,
            "endpoints": ["chat/completions"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "capabilities": {
                "streaming": True,
                "tools": True,
                "structured_output": True,
                "reasoning": False,
                "prompt_caching": False,
            },
            "pricing": {
                "currency": "USD",
                "unit": "per_1m_tokens",
                "input": "0.50",
                "output": "1.50",
                "cached_input": None,
                "cache_write": None,
                "minimum_request": "0",
            },
            "lifecycle": {
                "status": "active",
                "deprecation_at": None,
                "retirement_at": None,
                "replacement_model_id": None,
            },
        }
    ],
}

_DECIMAL_PATTERN = r"^(0|[1-9][0-9]*)(\.[0-9]+)?$"
_NULLABLE_DECIMAL: Final[dict[str, object]] = {
    "type": ["string", "null"],
    "pattern": _DECIMAL_PATTERN,
}
_NULLABLE_TIMESTAMP: Final[dict[str, object]] = {
    "type": ["string", "null"],
    "format": "date-time",
}
_NULLABLE_MODEL_ID: Final[dict[str, object]] = {
    "type": ["string", "null"],
    "pattern": r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$",
}

PROVIDER_CATALOG_SCHEMA: Final[dict[str, object]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PROVIDER_CATALOG_SCHEMA_URL,
    "title": "TrustedRouter provider catalog v1",
    "description": (
        "The canonical model, capability, price, and lifecycle response for "
        "TrustedRouter provider onboarding."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "data"],
    "properties": {
        "object": {"const": "list"},
        "data": {
            "type": "array",
            "items": {"$ref": "#/$defs/model"},
        },
    },
    "$defs": {
        "model": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "id",
                "object",
                "owned_by",
                "name",
                "type",
                "context_length",
                "max_output_tokens",
                "endpoints",
                "input_modalities",
                "output_modalities",
                "capabilities",
                "pricing",
                "lifecycle",
            ],
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$",
                },
                "object": {"const": "model"},
                "owned_by": {
                    "type": "string",
                    "pattern": r"^[a-z0-9][a-z0-9._-]*$",
                },
                "name": {"type": "string", "minLength": 1},
                "type": {"const": "chat"},
                "context_length": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1},
                "endpoints": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {
                        "enum": [
                            "chat/completions",
                            "responses",
                        ]
                    },
                },
                "input_modalities": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {
                        "enum": ["text", "image", "audio", "video", "file"]
                    },
                },
                "output_modalities": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"enum": ["text", "image", "audio"]},
                },
                "capabilities": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "streaming",
                        "tools",
                        "structured_output",
                        "reasoning",
                        "prompt_caching",
                    ],
                    "properties": {
                        "streaming": {"type": "boolean"},
                        "tools": {"type": "boolean"},
                        "structured_output": {"type": "boolean"},
                        "reasoning": {"type": "boolean"},
                        "prompt_caching": {"type": "boolean"},
                    },
                },
                "pricing": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "currency",
                        "unit",
                        "input",
                        "output",
                        "cached_input",
                        "cache_write",
                        "minimum_request",
                    ],
                    "properties": {
                        "currency": {"const": "USD"},
                        "unit": {"const": "per_1m_tokens"},
                        "input": {
                            "type": "string",
                            "pattern": _DECIMAL_PATTERN,
                        },
                        "output": {
                            "type": "string",
                            "pattern": _DECIMAL_PATTERN,
                        },
                        "cached_input": _NULLABLE_DECIMAL,
                        "cache_write": _NULLABLE_DECIMAL,
                        "minimum_request": {
                            "type": "string",
                            "pattern": _DECIMAL_PATTERN,
                        },
                    },
                },
                "lifecycle": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "status",
                        "deprecation_at",
                        "retirement_at",
                        "replacement_model_id",
                    ],
                    "properties": {
                        "status": {
                            "enum": ["active", "deprecated", "retired"]
                        },
                        "deprecation_at": _NULLABLE_TIMESTAMP,
                        "retirement_at": _NULLABLE_TIMESTAMP,
                        "replacement_model_id": _NULLABLE_MODEL_ID,
                    },
                },
            },
        }
    },
}

PROVIDER_CATALOG_V2_SCHEMA_URL: Final = (
    "https://trustedrouter.com/providers/marketplace/catalog.v2.schema.json"
)

_V1_EXAMPLE_DATA = PROVIDER_CATALOG_EXAMPLE["data"]
assert isinstance(_V1_EXAMPLE_DATA, list)
_V2_MODEL_EXAMPLE = copy.deepcopy(_V1_EXAMPLE_DATA[0])
assert isinstance(_V2_MODEL_EXAMPLE, dict)
_V2_CAPABILITIES_EXAMPLE = _V2_MODEL_EXAMPLE["capabilities"]
assert isinstance(_V2_CAPABILITIES_EXAMPLE, dict)
_V2_CAPABILITIES_EXAMPLE["receipts"] = {
    "spec": "inference-receipt/1",
    "algorithms": ["EdDSA"],
    "delivery": ["header", "stream-chunk"],
}
_V2_MODEL_EXAMPLE["reliability"] = {
    "first_token_timeout_seconds": 20,
    "completion_timeout_seconds": 120,
    "stream_idle_timeout_seconds": 30,
    "capacity_scope": "model_region",
}

PROVIDER_CATALOG_V2_EXAMPLE: Final[dict[str, object]] = {
    "object": "list",
    "contract_version": "2.0",
    "provider": {
        "id": "acme",
        "status_url": "https://status.acme.example",
        "support_contact": "mailto:support@acme.example",
        "incident_contact": "mailto:incidents@acme.example",
        "regions": ["us-east", "eu-west"],
        "request_id_header": "x-request-id",
        "error_contract": {
            "rate_limit_status": 429,
            "overload_status": 503,
            "retry_after_header": "Retry-After",
            "account_quota_error_codes": ["account_quota_exceeded"],
        },
    },
    "data": [_V2_MODEL_EXAMPLE],
}

PROVIDER_CATALOG_V2_SCHEMA: Final[dict[str, object]] = copy.deepcopy(
    PROVIDER_CATALOG_SCHEMA
)
PROVIDER_CATALOG_V2_SCHEMA["$id"] = PROVIDER_CATALOG_V2_SCHEMA_URL
PROVIDER_CATALOG_V2_SCHEMA["title"] = "TrustedRouter provider reliability contract v2"
PROVIDER_CATALOG_V2_SCHEMA["description"] = (
    "Canonical models, pricing, lifecycle, regional capacity, deadlines, and "
    "machine-readable overload semantics for TrustedRouter providers."
)
PROVIDER_CATALOG_V2_SCHEMA["required"] = [
    "object",
    "contract_version",
    "provider",
    "data",
]
properties = PROVIDER_CATALOG_V2_SCHEMA["properties"]
assert isinstance(properties, dict)
properties["contract_version"] = {"const": "2.0"}
properties["provider"] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "status_url",
        "support_contact",
        "incident_contact",
        "regions",
        "request_id_header",
        "error_contract",
    ],
    "properties": {
        "id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9._-]*$"},
        "status_url": {"type": "string", "format": "uri"},
        "support_contact": {"type": "string", "format": "uri"},
        "incident_contact": {"type": "string", "format": "uri"},
        "regions": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "request_id_header": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9-]+$",
        },
        "error_contract": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "rate_limit_status",
                "overload_status",
                "retry_after_header",
                "account_quota_error_codes",
            ],
            "properties": {
                "rate_limit_status": {"const": 429},
                "overload_status": {"const": 503},
                "retry_after_header": {"const": "Retry-After"},
                "account_quota_error_codes": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
defs = PROVIDER_CATALOG_V2_SCHEMA["$defs"]
assert isinstance(defs, dict)
model_schema = defs["model"]
assert isinstance(model_schema, dict)
model_required = model_schema["required"]
assert isinstance(model_required, list)
model_required.append("reliability")
model_properties = model_schema["properties"]
assert isinstance(model_properties, dict)
capabilities_schema = model_properties["capabilities"]
assert isinstance(capabilities_schema, dict)
capabilities_properties = capabilities_schema["properties"]
assert isinstance(capabilities_properties, dict)
capabilities_properties["receipts"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["spec", "algorithms", "delivery"],
    "properties": {
        "spec": {"const": "inference-receipt/1"},
        "algorithms": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"enum": ["EdDSA"]},
        },
        "delivery": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"enum": ["header", "stream-chunk"]},
        },
    },
}
model_properties["reliability"] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "first_token_timeout_seconds",
        "completion_timeout_seconds",
        "stream_idle_timeout_seconds",
        "capacity_scope",
    ],
    "properties": {
        "first_token_timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 300,
        },
        "completion_timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 900,
        },
        "stream_idle_timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 300,
        },
        "capacity_scope": {
            "enum": ["global", "region", "model", "model_region"],
        },
    },
}
