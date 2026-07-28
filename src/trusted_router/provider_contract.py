"""Canonical provider onboarding contract published on the provider apply page."""

from __future__ import annotations

from typing import Final

PROVIDER_CATALOG_SCHEMA_URL: Final = (
    "https://trustedrouter.com/providers/apply/catalog.schema.json"
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
