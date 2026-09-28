"""Runtime enforcement of the strict billing raw-JSON boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypeAlias

import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, ValidationInfo, model_validator

from tests.test_billing_snapshot import dto_roundtrip_values
from trusted_router import billing_snapshot as billing

DUPLICATE = '{"usage_type":"USD","usage_type":"Credits"}'
ADAPTERS = ["direct", "assignment", "type_alias", "annotated", "pep695", "list", "optional"]
PARSERS = {
    "Eligibility": "parse_eligibility",
    "BillingSnapshot": "parse_snapshot",
    "TerminalEnvelope": "parse_envelope",
    "AcceptanceOutcome": "parse_acceptance",
}


def adapter_for(kind: str) -> TypeAdapter:
    Alias = billing.Eligibility
    Facts: TypeAlias = billing.Eligibility
    AnnotatedFacts: TypeAlias = Annotated[Facts, Field(description="Eligibility")]
    if kind == "pep695":
        if sys.version_info < (3, 12):
            pytest.skip("PEP 695 requires Python 3.12+")
        namespace = {"Eligibility": billing.Eligibility}
        exec("type Facts = Eligibility", namespace)  # noqa: S102
        return TypeAdapter(namespace["Facts"])
    return TypeAdapter({
        "direct": billing.Eligibility,
        "assignment": Alias,
        "type_alias": Facts,
        "annotated": AnnotatedFacts,
        "list": list[Facts],
        "optional": Facts | None,
    }[kind])


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize("raw", ["{}", DUPLICATE])
def test_adapter_json_is_rejected(kind: str, raw: str) -> None:
    if kind == "list":
        raw = f"[{raw}]"
    with pytest.raises(ValidationError, match="unsupported_wire_path"):
        adapter_for(kind).validate_json(raw)


@pytest.mark.parametrize("raw", ["{}", DUPLICATE])
def test_foreign_model_json_is_rejected(raw: str) -> None:
    Facts: TypeAlias = billing.Eligibility

    class Container(BaseModel):
        facts: Facts

    with pytest.raises(ValidationError, match="unsupported_wire_path"):
        Container.model_validate_json('{"facts":' + raw + '}')


@pytest.mark.parametrize("name", list(dto_roundtrip_values()))
def test_every_dto_adapter_is_rejected(name: str) -> None:
    value = dto_roundtrip_values()[name]
    with pytest.raises(ValidationError, match="unsupported_wire_path"):
        TypeAdapter(type(value)).validate_json(billing.canonical_bytes(value))


@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_supported_wire_entry_points(strict: bool | None, as_bytes: bool) -> None:
    # Includes snapshot -> candidate -> tier/rates and envelope -> usage nesting.
    for name, value in dto_roundtrip_values().items():
        encoded = billing.canonical_bytes(value)
        raw = encoded if as_bytes else encoded.decode("utf-8")
        assert type(value).model_validate_json(raw, strict=strict) == value
        if name in PARSERS:
            assert getattr(billing, PARSERS[name])(raw) == value


@pytest.mark.parametrize("entry", ["dto", "parser"])
def test_supported_paths_reject_duplicate_keys(entry: str) -> None:
    validate = (billing.Eligibility.model_validate_json if entry == "dto"
                else billing.parse_eligibility)
    with pytest.raises(ValueError, match="duplicate"):
        validate(DUPLICATE)


@pytest.mark.parametrize("outcome", ["success", "decode_error", "validation_error", "type_error"])
def test_wire_context_does_not_leak(outcome: str) -> None:
    if outcome == "success":
        billing.parse_eligibility("{}")
    else:
        raw = {"decode_error": DUPLICATE, "validation_error": '{"typed":1}',
               "type_error": bytearray(b"{}")}[outcome]
        with pytest.raises((ValueError, TypeError)):
            billing.Eligibility.model_validate_json(raw)
    # Behavioral check: a subsequent untrusted JSON call still fails closed.
    test_adapter_json_is_rejected("type_alias", DUPLICATE)


@pytest.mark.parametrize("worker", [False, True])
def test_copied_context_cannot_authorize_wire(worker: bool) -> None:
    contexts = []

    class Capture(billing.Eligibility):
        @model_validator(mode="after")
        def capture(self):
            contexts.append(copy_context())
            return self

    Capture.model_validate_json("{}")
    validate = partial(contexts[0].run, test_adapter_json_is_rejected, "direct", DUPLICATE)
    if worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(validate).result()
    else:
        validate()


def test_child_task_cannot_authorize_wire() -> None:
    async def run():
        tasks = []

        async def validate():
            test_adapter_json_is_rejected("direct", DUPLICATE)

        class Capture(billing.Eligibility):
            @model_validator(mode="after")
            def capture(self):
                tasks.append(asyncio.create_task(validate()))
                return self

        Capture.model_validate_json("{}")
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_reentrant_adapter_cannot_authorize_wire() -> None:
    class Capture(billing.Eligibility):
        @model_validator(mode="after")
        def capture(self):
            test_adapter_json_is_rejected("direct", DUPLICATE)
            # An independent supported call is still allowed while one is active.
            assert billing.parse_eligibility("{}").typed is True
            test_foreign_model_json_is_rejected(DUPLICATE)
            return self

    Capture.model_validate_json("{}")


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("copied", [False, True])
def test_captured_validation_context_is_revoked(fail: bool, copied: bool) -> None:
    contexts = []

    class Capture(billing.Eligibility):
        @model_validator(mode="after")
        def capture(self, info: ValidationInfo):
            contexts.append(copy.copy(info.context) if copied else info.context)
            if fail:
                raise ValueError("capture failed")
            return self

    if fail:
        with pytest.raises(ValueError, match="capture failed"):
            Capture.model_validate_json("{}")
    else:
        Capture.model_validate_json("{}")
    with pytest.raises(ValidationError, match="unsupported_wire_path"):
        TypeAdapter(billing.Eligibility).validate_json(DUPLICATE, context=contexts[0])


@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize("by_alias,by_name", [(None, None), (True, False), (False, True), (True, True)])
def test_context_and_name_options_preserve_nested_validation(strict, by_alias, by_name) -> None:
    seen = []
    caller = {"trace": object()}
    before = caller.copy()

    class Child(billing.Eligibility):
        @model_validator(mode="after")
        def capture(self, info: ValidationInfo):
            seen.append(info.context)
            assert info.context["trace"] is caller["trace"]
            return self

    class Parent(billing.Frozen):
        child: Child

    result = Parent.model_validate_json(
        '{"child":{}}', strict=strict, extra=None, context=caller,
        by_alias=by_alias, by_name=by_name,
    )
    assert result.child.typed is True
    assert seen[0] is not caller
    assert caller == before
    with pytest.raises(ValueError):
        Parent.model_validate_json(
            '{"child":{"new_fee":123}}', strict=strict, context=caller,
            by_alias=by_alias, by_name=by_name,
        )
    assert caller == before
    with pytest.raises(ValidationError, match="unsupported_wire_path"):
        TypeAdapter(billing.Eligibility).validate_json(DUPLICATE, context=caller)


@pytest.mark.parametrize("context", [True, object(), []])
def test_non_dict_wire_context_is_rejected(context) -> None:
    with pytest.raises(TypeError, match="unsupported_option"):
        billing.Eligibility.model_validate_json("{}", context=context)


STRICT_INPUTS = [
    ("RawUsage", {"input_tokens": True, "output_tokens": 0}),
    ("RawUsage", {"input_tokens": "1", "output_tokens": 0}),
    ("Eligibility", {"typed": 1}),
    ("Eligibility", {}),
]


@pytest.mark.parametrize("entry", ["json", "python"])
@pytest.mark.parametrize("name,data", STRICT_INPUTS)
def test_strict_false_is_rejected(entry: str, name: str, data: dict) -> None:
    model = getattr(billing, name)
    validate = model.model_validate_json if entry == "json" else model.model_validate
    with pytest.raises(TypeError, match="unsupported_option: strict=False"):
        validate(json.dumps(data) if entry == "json" else data, strict=False)


@pytest.mark.parametrize("entry", ["json", "python"])
@pytest.mark.parametrize("extra", ["ignore", "allow", "forbid"])
def test_extra_override_is_rejected(entry: str, extra: str) -> None:
    validate = (billing.Eligibility.model_validate_json if entry == "json"
                else billing.Eligibility.model_validate)
    with pytest.raises(TypeError, match="unsupported_option: extra"):
        validate('{"new_fee":123}' if entry == "json" else {"new_fee": 123}, extra=extra)


def test_python_validation_and_unrelated_metadata_remain_supported() -> None:
    facts = billing.Eligibility()
    assert billing.Eligibility.model_validate({}) == facts
    assert TypeAdapter(billing.Eligibility).validate_python({}) == facts
    assert TypeAdapter(billing.Eligibility).validate_python(
        SimpleNamespace(usage_type="Credits"), from_attributes=True,
    ) == facts

    class Container(BaseModel):
        facts: billing.Eligibility

    assert Container.model_validate({"facts": {}}).facts == facts
    assert TypeAdapter(Annotated[str, Field(description="Eligibility")]).validate_json(
        '"Credits"',
    ) == "Credits"


RULES = json.loads((Path(__file__).parent / "fixtures/async_settlement/billing_v1.rules.json").read_bytes())
WIRE_MUTATIONS = next(rule for rule in RULES if rule["name"] == "duplicate_json_keys")["wire_boundary"]["mutations"]


@pytest.mark.parametrize("mutation", WIRE_MUTATIONS, ids=lambda m: m["name"])
def test_runtime_boundary_mutation_is_killed(mutation: dict) -> None:
    name = mutation["name"]
    if name == "wire_check_disabled":
        checks = [partial(test_adapter_json_is_rejected, kind, raw)
                  for kind in ADAPTERS for raw in ["{}", DUPLICATE]]
        checks += [partial(test_foreign_model_json_is_rejected, raw) for raw in ["{}", DUPLICATE]]
    elif name == "wire_mode_check_removed":
        checks = [test_python_validation_and_unrelated_metadata_remain_supported]
    elif name == "wire_context_never_set":
        checks = [partial(test_supported_wire_entry_points, strict, as_bytes)
                  for strict in [None, True] for as_bytes in [False, True]]
    elif name == "wire_context_leaked":
        checks = [partial(test_captured_validation_context_is_revoked, fail, copied)
                  for fail in [False, True] for copied in [False, True]]
    elif name == "wire_capability_ambient":
        checks = [partial(test_copied_context_cannot_authorize_wire, worker)
                  for worker in [False, True]]
        checks += [test_child_task_cannot_authorize_wire, test_reentrant_adapter_cannot_authorize_wire]
    elif name == "strict_false_forwarded":
        checks = [partial(test_strict_false_is_rejected, entry, model, data)
                  for entry in ["json", "python"] for model, data in STRICT_INPUTS]
    else:
        assert name == "extra_forwarded"
        checks = [partial(test_extra_override_is_rejected, entry, extra)
                  for entry in ["json", "python"] for extra in ["ignore", "allow"]]
    for check in checks:
        check()
    assert billing.__file__ is not None
    source = Path(billing.__file__).read_text()
    assert source.count(mutation["old"]) == 1
    mutated = source.replace(mutation["old"], mutation["new"])
    for replacement in mutation.get("replacements", []):
        assert mutated.count(replacement["old"]) == 1
        mutated = mutated.replace(replacement["old"], replacement["new"])
    original = billing.__dict__.copy()
    try:
        for check in checks:
            # Fresh module state for each leak control; nothing is written to disk.
            exec(compile(mutated, billing.__file__, "exec"), billing.__dict__)  # noqa: S102
            if name in ("wire_context_never_set", "wire_mode_check_removed"):
                with pytest.raises(ValidationError, match="unsupported_wire_path"):
                    check()
            else:
                with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
                    check()
    finally:
        billing.__dict__.clear()
        billing.__dict__.update(original)
    for check in checks:
        check()
    print(f"{name}: red {len(checks)}/{len(checks)} -> green {len(checks)}/{len(checks)}")
