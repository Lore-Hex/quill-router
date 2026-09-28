"""Required negative controls: mutate only module memory, restore in finally."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.test_billing_snapshot import ALL_CASES, check_case
from trusted_router import billing_snapshot as billing

MUTATIONS = [
    (
        "aggregate_rounding", ["component_half_up"],
        'cost = checked(cost + checked(product + 500_000) // 1_000_000)',
        'cost = checked(cost + product)',
    ),
    (
        "reasoning_double_added", ["reasoning_subset"],
        '(raw.output_tokens, rates.output_micro_per_million)',
        '(raw.output_tokens + raw.reasoning_tokens, rates.output_micro_per_million)',
    ),
    (
        "wrong_cache_convention", ["openai_cache"],
        'if candidate.prompt_convention == "includes_cache":',
        'if candidate.prompt_convention == "excludes_cache":',
    ),
    (
        "exclusive_tier_boundary", ["tier_at"],
        'total <= tier.max_prompt_tokens', 'total < tier.max_prompt_tokens',
    ),
    (
        "missing_minimum", ["one_micro_minimum"],
        'charge_micro=max(cost, 1) if positive else 0', 'charge_micro=cost',
    ),
    (
        "unconditional_hold_clamp", ["no_estimate_clamp"],
        'charge_micro=max(cost, 1) if positive else 0',
        'charge_micro=min(max(cost, 1) if positive else 0, 1)',
    ),
    (
        "accept_excluded_fee", ["nonzero_endpoint_fee"],
        'if self.request_fee_micro != 0:', 'if False:',
    ),
    (
        "skip_tier_validation",
        ["tier_nonfinal_unbounded", "tier_duplicate_boundary", "tier_descending_boundaries"],
        'enumerate(self.tiers)', 'enumerate(())',
    ),
    (
        "ignore_unknown_eligibility_fields",
        ["unknown_context_field_requested", "unknown_context_field_observed"],
        '    typed: Annotated[bool, Field(strict=True)] = True',
        '    model_config = ConfigDict(extra="ignore")\n\n'
        '    typed: Annotated[bool, Field(strict=True)] = True',
    ),
]


@pytest.mark.parametrize("name,names,old,new", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_is_killed(name: str, names: list[str], old: str, new: str) -> None:
    assert billing.__file__ is not None
    source = Path(billing.__file__).read_text()
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    if name == "aggregate_rounding":
        # The actual bug: round ONCE after adding unrounded products.
        mutated = mutated.replace(
            'return Evaluation(usage=usage, charge_micro=max(cost, 1) if positive else 0)',
            'cost = (cost + 500_000) // 1_000_000\n'
            '    return Evaluation(usage=usage, charge_micro=max(cost, 1) if positive else 0)',
        )
    selected = [case for case in ALL_CASES if case["name"] in names]
    assert len(selected) == len(names)
    original = billing.__dict__.copy()
    try:
        exec(compile(mutated, billing.__file__, "exec"), billing.__dict__)  # noqa: S102
        for case in selected:
            with pytest.raises((AssertionError, pytest.fail.Exception)):
                check_case(case)
    finally:
        billing.__dict__.clear()
        billing.__dict__.update(original)
    for case in selected:
        check_case(case)
    print(f"{name}: red {len(selected)}/{len(selected)} -> green {len(selected)}/{len(selected)}")


RULES = json.loads((Path(__file__).parent / "fixtures/async_settlement/billing_v1.rules.json").read_bytes())


def mutate_rule(source: str, rule: dict) -> str:
    """Limit field mutations to one model; other models retain their constraints."""
    old, new = rule["old"], rule["new"]
    if scope := rule.get("scope"):
        start = source.index(f"class {scope}(")
        end = source.find("\n\nclass ", start + 1)
        if end == -1:
            end = source.find("\n\ndef ", start + 1)
        part = source[start:end]
        assert part.count(old) == 1, (rule["name"], old)
        return source[:start] + part.replace(old, new) + source[end:]
    assert source.count(old) == 1, (rule["name"], old)
    return source.replace(old, new)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r["name"])
def test_validation_mutation_is_killed(rule: dict) -> None:
    assert billing.__file__ is not None
    source = Path(billing.__file__).read_text()
    selected = [case for case in ALL_CASES if case["name"] in rule["cases"]]
    assert len(selected) == len(rule["cases"])
    for case in selected:
        check_case(case)
    original = billing.__dict__.copy()
    try:
        exec(compile(mutate_rule(source, rule), billing.__file__, "exec"), billing.__dict__)  # noqa: S102
        for case in selected:
            # Rejection controls require acceptance, not a different validation error.
            # Explicit error-code and positive controls have their own failure modes.
            if rule.get("failure") == "rejected_positive":
                with pytest.raises(ValueError):
                    check_case(case)
            elif rule.get("failure") == "value":
                with pytest.raises(AssertionError):
                    check_case(case)
            else:
                failure = (AssertionError, pytest.fail.Exception) if rule.get("failure") == "error_code" else pytest.fail.Exception
                with pytest.raises(failure, match="Regex pattern did not match|DID NOT RAISE"):
                    check_case(case)
        if all(case.get("expected_error") == "string_type" for case in selected):
            # Widening one Identity/Digest binding must kill only its own vector.
            for case in ALL_CASES:
                if case["name"] not in rule["cases"]:
                    check_case(case)
    finally:
        billing.__dict__.clear()
        billing.__dict__.update(original)
    for case in selected:
        check_case(case)
    n = len(selected)
    print(f"{rule['name']}: red {n}/{n} selected -> green {n}/{n} selected")


def test_frozen_mutation_is_killed() -> None:
    # Python object assignment is outside JSON. PR 2 must test Go copying/aliasing.
    from tests.test_billing_snapshot import (
        test_builder_copies_and_sorts_and_freezes,
        test_envelope_binding_and_refund,
    )

    assert billing.__file__ is not None
    source = Path(billing.__file__).read_text()
    assert source.count("frozen=True") == 1
    checks = [test_builder_copies_and_sorts_and_freezes, test_envelope_binding_and_refund]
    original = billing.__dict__.copy()
    try:
        exec(compile(source.replace("frozen=True", "frozen=False"), billing.__file__, "exec"), billing.__dict__)  # noqa: S102
        for check in checks:
            with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
                check()
    finally:
        billing.__dict__.clear()
        billing.__dict__.update(original)
    for check in checks:
        check()
    print("frozen_assignment: red 2/2 selected -> green 2/2 selected (Python objects)")


def assert_binding_type_rejections(rules: list[dict]) -> None:
    """A bound primitive needs its own wrong-type vector and scoped control."""
    cases = {case["name"]: case for case in ALL_CASES}
    for name, model in vars(billing).items():
        if not isinstance(model, type) or not issubclass(model, billing.Frozen):
            continue
        for field_name, field in model.model_fields.items():
            adapter = TypeAdapter(field.rebuild_annotation())
            schema = adapter.json_schema()
            variants = schema.get("anyOf", [schema])
            # Cover constrained strings/integers, strict booleans and nullable
            # bindings. Literal/enum and nested DTO rules have separate controls.
            types = {
                variant.get("type") for variant in variants
                if "enum" not in variant and "const" not in variant
            }
            for kind in types & {"string", "integer", "boolean"}:
                error_type = {"string": "string_type", "integer": "int_type", "boolean": "bool_type"}[kind]
                witnessed = False
                for rule in rules:
                    if rule.get("scope") != name or not rule["rule"].startswith(f"{name}.{field_name}:"):
                        continue
                    assert rule["old"].startswith(f"    {field_name}:")
                    for case_name in rule["cases"]:
                        case = cases[case_name]
                        if case.get("model") != name or case.get("expected_error") is None:
                            continue
                        if case["operation"] == "field" and case["field"] == field_name:
                            value = case["input"]
                        elif case["operation"] == "model" and field_name in case["input"]:
                            value = case["input"][field_name]
                        else:
                            continue
                        # Null-only controls prove presence, not the primitive type.
                        if value is None:
                            continue
                        try:
                            adapter.validate_python(value)
                        except ValidationError as exc:
                            if [(error["loc"], error["type"]) for error in exc.errors()] == [((), error_type)]:
                                witnessed = True
                assert witnessed, (name, field_name, error_type)


@pytest.mark.parametrize("rule", [rule for rule in RULES if rule["cases"] and all(
    next(case for case in ALL_CASES if case["name"] == case_name).get("expected_error") == "string_type"
    for case_name in rule["cases"]
)], ids=lambda rule: rule["name"])
def test_inventory_requires_bound_string_type_control(rule: dict) -> None:
    remaining = [entry for entry in RULES if entry is not rule]
    with pytest.raises(AssertionError, match="string_type"):
        assert_binding_type_rejections(remaining)


def test_rule_inventory_links() -> None:
    names = {case["name"] for case in ALL_CASES}
    assert len({rule["name"] for rule in RULES}) == len(RULES)
    doc = (Path(__file__).parents[1] / "docs/async-settlement-billing-v1.md").read_text()
    for rule in RULES:
        assert rule["cases"]
        assert set(rule["cases"] + rule.get("positive_cases", [])) <= names
        n = len(rule["cases"])
        case_names = rule["cases"] + rule.get("positive_cases", [])
        row = ("| " + rule["rule"].replace("|", "\\|") + " | "
               + ", ".join(f"`{name}`" for name in case_names)
               + f" | `{rule['name']}` | {n}/{n} → {n}/{n} |")
        assert row in doc
    # A newly declared field/model cannot silently miss the validation inventory.
    for name, model in vars(billing).items():
        if isinstance(model, type) and issubclass(model, billing.Frozen):
            assert any(rule["name"] == f"extra_{name.lower()}" for rule in RULES)
            for field in model.model_fields:
                assert any(rule["rule"].startswith(f"{name}.{field}:") for rule in RULES), (name, field)
    assert_binding_type_rejections(RULES)
