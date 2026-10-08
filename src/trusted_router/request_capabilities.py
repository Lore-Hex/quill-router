"""Request capabilities after gateway adaptation, alongside OR's parameter union.

Reviewed contracts live in data/request_capabilities.json. A row may carry only
a tools contract from the provider's own documentation; evidence stays scoped
to that provider's model routes. Missing reasoning_effort means unknown and
preserves any declared parameter. A parameter name alone does not establish
its enum; see /docs#model-capabilities for unsupported versus unknown.
"""

from __future__ import annotations

import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

from trusted_router.catalog_capabilities import union_supported_parameters
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL, Model, ModelEndpoint, offers_chat
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement

EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_TEXT_ONLY_MODELS = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})
_CONTRACT_PATH = Path(__file__).parent / "data/request_capabilities.json"


class RequestCapabilities(TypedDict):
    reasoning_effort: list[str] | None
    tools: bool
    seed: bool
    vision: bool
    confidential: bool


@lru_cache(maxsize=1)
def _reviewed_contracts() -> dict[tuple[str, str], dict[str, Any]]:
    data = json.loads(_CONTRACT_PATH.read_text())
    return {
        (provider, model_id): row
        for row in data["contracts"]
        for provider in row["providers"]
        for model_id in row["models"]
    }


def normalize_request_capabilities(
    model: Model, endpoints: list[ModelEndpoint],
) -> tuple[Model, list[ModelEndpoint]]:
    """Correct declarations once, before publishing the routing registry.

    Keep model declarations unless every route explicitly rejects them. A
    missing route declaration is not evidence against model-level support.
    Readers need only the resulting parameters and modalities, not overrides.
    """
    corrected = []
    unsupported_parameters = set(model.supported_parameters)
    unsupported_modalities = set(model.input_modalities)
    for endpoint in endpoints:
        reviewed = _reviewed_contracts().get((endpoint.provider, model.id), {})
        support: dict[str, bool] = {
            name: reviewed[name] for name in ("tools", "seed") if name in reviewed
        }
        if reviewed.get("reasoning_effort") is not None:
            support["reasoning_effort"] = bool(reviewed["reasoning_effort"])
        # Mistral needs random_seed, which the gateway does not translate;
        # Anthropic's native adapter does not forward seed at all.
        if endpoint.provider in {"anthropic", "mistral"}:
            support["seed"] = False
        parameters = union_supported_parameters(
            (name for name in endpoint.supported_parameters if support.get(name) is not False),
            (name for name, supported in support.items() if supported),
        )
        modalities = endpoint.input_modalities
        if modalities is None:
            modalities = model.input_modalities
        vision = False if model.id in _TEXT_ONLY_MODELS else reviewed.get("vision")
        if vision is False:
            modalities = tuple(value for value in modalities if value != "image")
        elif vision is True and "image" not in modalities:
            modalities = (*modalities, "image")
        corrected.append(replace(
            endpoint, supported_parameters=parameters, input_modalities=modalities,
        ))
        # Intersect explicit rejections while preserving unknown declarations.
        unsupported_parameters.intersection_update(
            name for name, supported in support.items() if not supported
        )
        unsupported_modalities.intersection_update(("image",) if vision is False else ())
    if endpoints:
        model = replace(
            model,
            supported_parameters=tuple(
                name for name in model.supported_parameters if name not in unsupported_parameters
            ),
            input_modalities=tuple(
                value for value in model.input_modalities if value not in unsupported_modalities
            ),
        )
    return model, corrected


def endpoint_capabilities(model: Model, endpoint: ModelEndpoint) -> RequestCapabilities:
    """Read a provider/credential route's corrected request declarations."""
    effort = _reviewed_contracts().get((endpoint.provider, model.id), {}).get("reasoning_effort")
    return {
        "reasoning_effort": (
            [value for value in EFFORT_ORDER if value in effort] if effort is not None else None
        ),
        "tools": "tools" in endpoint.supported_parameters,
        "seed": "seed" in endpoint.supported_parameters,
        "vision": "image" in (endpoint.input_modalities or ()),
        "confidential": offers_chat(model) and endpoint_meets_privacy_requirement(
            endpoint, PRIVACY_TIER_CONFIDENTIAL,
        ),
    }


def model_capabilities(
    endpoints: list[RequestCapabilities],
    supported_parameters: tuple[str, ...],
    input_modalities: tuple[str, ...],
) -> RequestCapabilities:
    """Match model discovery fields; union verified effort and privacy support."""
    efforts = {
        value for endpoint in endpoints for value in (endpoint["reasoning_effort"] or [])
    }
    all_reject_effort = bool(endpoints) and all(
        endpoint["reasoning_effort"] == [] for endpoint in endpoints
    )
    return {
        "reasoning_effort": (
            [value for value in EFFORT_ORDER if value in efforts]
            if efforts or all_reject_effort else None
        ),
        "tools": "tools" in supported_parameters,
        "seed": "seed" in supported_parameters,
        "vision": "image" in input_modalities,
        "confidential": any(endpoint["confidential"] for endpoint in endpoints),
    }
