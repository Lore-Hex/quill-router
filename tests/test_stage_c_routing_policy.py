from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

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


def _assert_normalized_selection(source: str) -> set[str]:
    tree = ast.parse(source)
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Discover the public selection API AND any new normalized-input function.
    selectors = {
        name for name, node in functions.items()
        if (not name.startswith("_") and "candidates" in name)
        or any(
            arg.annotation is not None
            and "NormalizedRoutingInputs" in ast.unparse(arg.annotation)
            for arg in node.args.args
        )
    } - {"_coerce_routing_inputs"}
    tainted = {
        name: {arg.arg for arg in functions[name].args.args
               if arg.annotation and "NormalizedRoutingInputs" in ast.unparse(arg.annotation)}
        for name in functions
    }
    pending = list(selectors)
    visited: set[str] = set()
    checked: set[tuple[str, frozenset[str]]] = set()
    while pending:
        name = pending.pop()
        state = (name, frozenset(tainted[name]))
        if state in checked:
            continue
        checked.add(state)
        visited.add(name)
        node = functions[name]
        # Track renamed aliases and forward their provenance through helpers;
        # this also catches dynamic keys, not only literal raw-field names.
        aliases = set(tainted[name])
        for _ in range(len(list(ast.walk(node)))):
            before = set(aliases)
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and isinstance(child.value, ast.Name) and child.value.id in aliases:
                    aliases.update(target.id for target in child.targets if isinstance(target, ast.Name))
            if aliases == before:
                break
        if name in selectors:
            annotations = {ast.unparse(arg.annotation) for arg in node.args.args if arg.annotation}
            assert any(
                "NormalizedRoutingInputs" in annotation or annotation == "RoutePreferences"
                for annotation in annotations
            ), f"{name}: selection must consume normalized inputs/preferences"
        # Follow function references, including helpers passed as callbacks.
        # Only the explicit raw->normalized boundary may inspect a raw body.
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in functions:
                if child.id != "_coerce_routing_inputs":
                    pending.append(child.id)
            # No selection helper may index a string-keyed raw object. This is
            # independent of argument name, aliases, and the raw field's name.
            if isinstance(child, ast.Subscript):
                assert not (isinstance(child.value, ast.Name) and child.value.id in aliases), f"{name}: raw input subscript"
                assert not (
                    isinstance(child.slice, ast.Constant)
                    and isinstance(child.slice.value, str)
                ), f"{name}: raw subscript"
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr in {"get", "pop", "setdefault", "__getitem__"} and child.args:
                    assert not (isinstance(child.func.value, ast.Name) and child.func.value.id in aliases), f"{name}: raw input mapping"
                    assert not (
                        isinstance(child.args[0], ast.Constant)
                        and isinstance(child.args[0].value, str)
                    ), f"{name}: raw mapping access"
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                assert child.func.id not in {"getattr", "vars"}, f"{name}: dynamic input access"
                callee = child.func.id
                if callee in functions and callee != "_coerce_routing_inputs":
                    parameters = functions[callee].args.args
                    arguments = {param.arg: arg for param, arg in zip(parameters, child.args, strict=False)}
                    arguments.update({kw.arg: kw.value for kw in child.keywords if kw.arg})
                    for param, arg in arguments.items():
                        if isinstance(arg, ast.Name) and arg.id in aliases:
                            tainted[callee].add(param)
                    pending.append(callee)
    return visited


def test_selectors_cannot_read_raw_routing_fields_outside_the_builder() -> None:
    discovered = _assert_normalized_selection(inspect.getsource(routing))
    assert "decide_route_endpoint_candidates" in discovered
    assert "_apply_endpoint_provider_filters" in discovered
    assert "_sort_endpoint_candidates" in discovered


@pytest.mark.parametrize("indirect", [False, True])
def test_enumeration_guard_detects_new_raw_selector_and_helper(indirect: bool) -> None:
    body = "alias = renamed; field = 'new_routing_field'; return alias.get(field)"
    helper = ""
    if indirect:
        body = "return new_helper(renamed)"
        helper = "\ndef new_helper(alias):\n    field = 'new_routing_field'\n    return alias[field]\n"
    mutation = (
        "\ndef new_route_endpoint_candidates(renamed: NormalizedRoutingInputs):\n"
        f"    {body}\n" + helper
    )
    with pytest.raises(AssertionError, match="raw"):
        _assert_normalized_selection(inspect.getsource(routing) + mutation)


def test_each_nested_preference_is_hashed_individually() -> None:
    normalized = _normalized()
    mutations = {
        "order": ("openai",), "only": frozenset({"openai"}),
        "ignore": frozenset({"openai"}), "allow_fallbacks": True,
        "data_collection": "allow", "sort": "price", "sort_partition": "global",
        "usage_type": "BYOK", "provider_jurisdiction": "us", "min_privacy_rank": 1,
        "privacy_requirements": frozenset({1}), "require_parameters": True,
        "requested_parameters": frozenset({"tools"}),
        "max_prompt_price_microdollars_per_million_tokens": 123,
        "max_completion_price_microdollars_per_million_tokens": 456,
    }
    assert set(mutations) == {field.name for field in dataclasses.fields(normalized.preferences)}
    assert set(normalized.canonical_document()["preferences"]) == set(mutations)
    for field, value in mutations.items():
        changed = dataclasses.replace(
            normalized, preferences=dataclasses.replace(normalized.preferences, **{field: value}),
        )
        assert changed.routing_policy_hash != normalized.routing_policy_hash, field


def test_green_normalization_hashes_resolved_renewable_provider_set(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="test")
    body = {"model": routing.GREEN_MODEL_ID, "route_type": "chat.completions"}
    monkeypatch.setattr(routing, "renewable_provider_slugs", lambda: frozenset({"anthropic"}))
    first = routing.normalize_routing_inputs(body, settings)
    monkeypatch.setattr(routing, "renewable_provider_slugs", lambda: frozenset({"openai"}))
    second = routing.normalize_routing_inputs(body, settings)
    assert first.preferences.only == frozenset({"anthropic"})
    assert second.preferences.only == frozenset({"openai"})
    assert first.routing_policy_hash != second.routing_policy_hash


def test_polyphemus_selection_route_and_retention_are_hashed() -> None:
    from trusted_router.routes.internal.gateway import (
        POLYPHEMUS_MODEL_ID,
        POLYPHEMUS_SELECT_ROUTE_TYPE,
    )

    settings = Settings(environment="test")
    body = {"model": POLYPHEMUS_MODEL_ID, "route_type": POLYPHEMUS_SELECT_ROUTE_TYPE}
    normalized = routing.normalize_routing_inputs(body, settings)
    assert normalized.route_type == POLYPHEMUS_SELECT_ROUTE_TYPE
    for change in [{"route_type": "responses"}, {"provider": {"data_collection": "deny"}}]:
        assert routing.normalize_routing_inputs({**body, **change}, settings).routing_policy_hash != normalized.routing_policy_hash


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


def test_decide_selection_consumes_hashed_normalized_preferences() -> None:
    settings = Settings(environment="test")
    base = {"model": "typesafe-ai/jev", "route_type": "decide"}
    first = routing.normalize_routing_inputs({**base, "provider": {"only": ["typesafe"]}}, settings)
    second = routing.normalize_routing_inputs({**base, "provider": {"only": ["vercel-ai-gateway"]}}, settings)
    assert first.routing_policy_hash != second.routing_policy_hash
    assert {endpoint.provider for _, endpoint in routing.decide_route_endpoint_candidates(first)} == {"typesafe"}
    assert {endpoint.provider for _, endpoint in routing.decide_route_endpoint_candidates(second)} == {"vercel-ai-gateway"}
