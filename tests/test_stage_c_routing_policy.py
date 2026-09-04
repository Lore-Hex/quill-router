from __future__ import annotations

import ast
import dataclasses
import inspect

from trusted_router import routing
from trusted_router.config import Settings


def _normalized() -> routing.NormalizedRoutingInputs:
    return routing.normalize_routing_inputs(
        {
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "provider": {
                "allow_fallbacks": False,
                "data_collection": "deny",
                "only": ["anthropic"],
                "usage": "credits",
            },
            "region": "us-central1",
            "route_type": "chat.completions",
        },
        Settings(environment="test"),
        resolved_region="us-central1",
    )


def test_normalized_fields_are_exactly_the_hash_serializer_fields() -> None:
    normalized = _normalized()
    declared = {field.name for field in dataclasses.fields(normalized)}

    assert set(normalized.canonical_document()) == declared


def test_every_allowed_provider_field_is_mapped_or_explicitly_ineligible() -> None:
    classified = (
        set(routing.NORMALIZED_PROVIDER_FIELD_MAP)
        | set(routing.LOCAL_ADMISSION_INELIGIBLE_PROVIDER_FIELDS)
    )

    assert classified == set(routing._PROVIDER_ROUTING_FIELDS)  # noqa: SLF001
    assert not (
        set(routing.NORMALIZED_PROVIDER_FIELD_MAP)
        & set(routing.LOCAL_ADMISSION_INELIGIBLE_PROVIDER_FIELDS)
    )


def test_mutating_each_normalized_field_changes_the_policy_hash() -> None:
    normalized = _normalized()
    mutations = {
        "model_ids": ("openai/gpt-4.1-mini",),
        "preferences": dataclasses.replace(
            normalized.preferences,
            only=frozenset({"openai"}),
        ),
        "route_type": "responses",
        "region": "us-east4",
        "service_tier": "priority",
        "usage_type": "BYOK",
        "fallback_policy": True,
        "priority_eligibility_bucket": "above_threshold",
        "models_fallback_present": True,
    }

    assert set(mutations) == {
        field.name for field in dataclasses.fields(normalized)
    }
    for field, value in mutations.items():
        assert (
            dataclasses.replace(normalized, **{field: value}).routing_policy_hash
            != normalized.routing_policy_hash
        ), field


def test_selectors_cannot_read_raw_routing_fields_outside_the_builder() -> None:
    tree = ast.parse(inspect.getsource(routing))
    selectors = {
        "chat_route_candidates",
        "chat_route_endpoint_candidates",
        "image_route_endpoint_candidates",
        "embeddings_route_endpoint_candidates",
        "video_route_endpoint_candidates",
    }
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in selectors:
            continue
        found.add(node.name)
        for child in ast.walk(node):
            assert not (
                isinstance(child, ast.Subscript)
                and isinstance(child.value, ast.Name)
                and child.value.id == "inputs"
            ), node.name
            assert not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "inputs"
                and child.func.attr == "get"
            ), node.name

    assert found == selectors


def test_local_admission_excludes_models_sort_and_priority_overflow() -> None:
    settings = Settings(environment="test")
    base = {
        "model": "anthropic/claude-haiku-4.5",
        "estimated_input_tokens": 100,
    }
    assert routing.normalize_routing_inputs(base, settings).local_admission_eligible
    assert not routing.normalize_routing_inputs(
        {**base, "models": ["anthropic/claude-haiku-4.5"]}, settings
    ).local_admission_eligible
    assert not routing.normalize_routing_inputs(
        {**base, "provider": {"sort": "price"}}, settings
    ).local_admission_eligible
    assert not routing.normalize_routing_inputs(
        {
            **base,
            "estimated_input_tokens": (
                routing.OPENAI_PRIORITY_MAX_PROMPT_TOKENS + 1
            ),
        },
        settings,
    ).local_admission_eligible
