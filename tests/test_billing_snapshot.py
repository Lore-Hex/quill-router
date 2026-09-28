from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from trusted_router import billing_snapshot as billing
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.pricing import PriceTier

FIXTURE = Path(__file__).parent / "fixtures/async_settlement/billing_v1.json"
FIXTURE_SHA256 = "a748ef09cfbd6bdfb2f84fb0b4a05af7030e6a1a6c69e2a54cf20a096bfcef4b"
DATA = json.loads(FIXTURE.read_bytes())
ALL_CASES = DATA["cases"]
# The original evaluation vectors also drive real settlement differentials.
CASES = [case for case in ALL_CASES if "operation" not in case]


class FixtureCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    snapshot: dict[str, Any]  # Negative vectors deliberately contain invalid snapshots.
    raw_usage: dict[str, Any]
    expected_normalized_usage: billing.NormalizedUsage | None
    selected_endpoint: str
    expected_charge_micro: billing.UInt | None
    expected_exclusion: Literal[
        "invalid_snapshot", "invalid_context", "invalid_usage", "arithmetic_overflow",
        "malformed_usage", "unsupported_endpoint", "untyped", "non_credits",
        "settlement_authority", "unsupported_route", "service_tier", "app_markup",
        "custom_markup", "receipt_fee", "request_fee", "custom_model", "user_model",
        "tool_cost", "search_cost", "image_cost", "video_cost", "partner", "liberty",
        "native_batch", "fusion", "polyphemus", "private_tier_basis",
    ] | None
    expected_terminal_envelope_hash: billing.Digest | None
    context: dict[str, Any]
    phase: Literal["requested", "observed"]
    reservation_estimate_micro: int | None = None
    catalog_action: str | None = None
    freeze_cache_rate: int | None = None


class ValidationCase(BaseModel):
    """Literal inputs, including malformed wire values; never derive expectations."""

    model_config = ConfigDict(extra="forbid")
    name: str
    operation: Literal["snapshot_json", "model", "envelope", "acceptance", "type", "field", "checked", "builder"]
    input: Any
    model: Literal[
        "Frozen", "Eligibility", "Rates", "Tier", "Candidate", "BillingSnapshot",
        "RawUsage", "NormalizedUsage", "Evaluation", "TerminalEnvelope", "AcceptanceOutcome",
        "UInt", "Identity", "Digest", "SettlementMode",
    ] | None = None
    field: str | None = None
    snapshot: dict[str, Any] | None = None
    expected_error: Literal[
        "invalid_snapshot", "invalid_context", "invalid_usage", "invalid_envelope",
        "invalid_acceptance", "invalid_evaluation", "invalid_type", "invalid_builder",
        "snapshot_hash_mismatch", "charge_mismatch", "unsupported_endpoint", "arithmetic_overflow",
        "string_type",
    ] | None
    expected_hash: str | None = None
    evaluated_usage: dict[str, Any] | None = None


class FixtureSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture_version: Literal[1]
    envelope_identity: dict[str, Any]
    cases: list[FixtureCase | ValidationCase]


def check_validation_case(case: dict[str, Any]) -> None:
    def validate() -> Any:
        operation = case["operation"]
        payload = case["input"]
        if operation == "snapshot_json":
            return billing.parse_snapshot(payload)
        if operation == "envelope":
            snapshot = billing.BillingSnapshot.model_validate(case["snapshot"])
            envelope = billing.TerminalEnvelope.model_validate(payload)
            if "evaluated_usage" in case:
                # An explicit fixture fault at the evaluator boundary, not a wire field.
                result = billing.Evaluation(usage=case["evaluated_usage"], charge_micro=payload["charge_micro"])
                with patch.object(billing, "evaluate", return_value=result):
                    billing.validate_envelope(snapshot, envelope)
            else:
                billing.validate_envelope(snapshot, envelope)
            return envelope
        if operation == "acceptance":
            return billing.AcceptanceOutcome.model_validate(payload)
        if operation == "type":
            return TypeAdapter(getattr(billing, case["model"])).validate_python(payload)
        if operation == "field":
            field = getattr(billing, case["model"]).model_fields[case["field"]]
            return TypeAdapter(field.rebuild_annotation()).validate_python(payload)
        if operation == "checked":
            return billing.checked(payload)
        if operation == "builder":
            # ModelEndpoint is a dataclass; preserve raw values without coercion.
            endpoint = endpoint_from_candidate(payload["candidate"])
            endpoint = replace(endpoint, **payload.get("endpoint_overrides", {}))
            return billing.build_snapshot([endpoint], billing.Eligibility())
        return getattr(billing, case["model"]).model_validate(payload)

    error = case["expected_error"]
    if error == "string_type":
        with pytest.raises(ValidationError) as caught:
            validate()
        errors = caught.value.errors()
        assert [item["type"] for item in errors] == ["string_type"]
        location = () if case["operation"] == "field" else (case["field"],)
        assert errors[0]["loc"] == location
        return
    if error is not None:
        # Stable wire categories for model errors, exact reasons for semantic checks.
        match = None if error.startswith("invalid_") else f"^{error}$"
        with pytest.raises(ValueError, match=match):
            validate()
    else:
        result = validate()
        if case.get("expected_hash") is not None:
            assert billing.canonical_hash(result) == case["expected_hash"]


def check_case(case: dict[str, Any]) -> None:
    if "operation" in case:
        check_validation_case(case)
        return
    error = case["expected_exclusion"]
    if error == "invalid_snapshot":
        with pytest.raises(ValueError):
            billing.parse_snapshot(json.dumps(case["snapshot"]))
        return
    snapshot = billing.parse_snapshot(json.dumps(case["snapshot"]))
    if error == "invalid_context":
        with pytest.raises(ValidationError):
            billing.Eligibility.model_validate(case["context"])
        return
    context = billing.Eligibility.model_validate(case["context"])
    if case["phase"] == "requested":
        with pytest.raises(ValueError, match=error):
            billing.build_snapshot([endpoint_from_candidate(case["snapshot"]["candidates"][0])], context)
        return
    if error == "invalid_usage":
        with pytest.raises(ValueError):
            billing.RawUsage.model_validate(case["raw_usage"])
        return
    raw = billing.RawUsage.model_validate(case["raw_usage"])
    if error:
        with pytest.raises(ValueError, match=error):
            billing.evaluate(snapshot, case["selected_endpoint"], raw, context)
        return
    result = billing.evaluate(snapshot, case["selected_endpoint"], raw, context)
    assert result.charge_micro == case["expected_charge_micro"]
    assert result.usage.model_dump() == case["expected_normalized_usage"]
    envelope = billing.TerminalEnvelope.model_validate({
        **DATA["envelope_identity"], "selected_endpoint": case["selected_endpoint"],
        "snapshot_hash": billing.canonical_hash(snapshot), "usage": result.usage,
        "charge_micro": result.charge_micro,
        "route_type": context.route_type, "streamed": context.streamed,
    })
    billing.validate_envelope(snapshot, envelope)
    assert billing.canonical_hash(envelope) == case["expected_terminal_envelope_hash"]
    assert billing.parse_snapshot(billing.canonical_bytes(snapshot)) == snapshot


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["name"])
def test_literal_golden(case: dict[str, Any]) -> None:
    check_case(case)


def test_fixture_schema_and_pin() -> None:
    FixtureSchema.model_validate(DATA)
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert FIXTURE.read_bytes() == (
        json.dumps(DATA, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("ascii")
    # Reconstruct the exact Round 2 file, including object order and whitespace.
    original = dict(DATA, cases=ALL_CASES[:102])
    original_bytes = (json.dumps(original, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    assert hashlib.sha256(original_bytes).hexdigest() == "aeb630a2d33eb60ff1a349a382f074a1a1e031c40bff2e79ccc5c6b11b338d4b"
    # Round 4 only appends: preserve all 717 Round 3 cases byte-for-byte.
    round3 = dict(DATA, cases=ALL_CASES[:717])
    round3_bytes = (json.dumps(round3, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    assert hashlib.sha256(round3_bytes).hexdigest() == "949e7eab8be042cb3d8044c0ba087ff0c9e98c96c7894e2f0c3ebd27b2db185b"
    assert len({c["name"] for c in ALL_CASES}) == len(ALL_CASES)
    assert json.loads(FIXTURE.with_suffix(".schema.json").read_bytes()) == FixtureSchema.model_json_schema()
    for case in CASES:
        accepted = case["expected_exclusion"] is None
        assert accepted == (case["expected_charge_micro"] is not None)
        assert accepted == (case["expected_normalized_usage"] is not None)
        assert accepted == (case["expected_terminal_envelope_hash"] is not None)


def endpoint_from_candidate(candidate: dict[str, Any], *, cached: Any = "frozen") -> ModelEndpoint:
    base = candidate["rates"]
    tiers = candidate["tiers"] or [{"max_prompt_tokens": None, "rates": base}]
    return ModelEndpoint(
        id=candidate["endpoint_id"], model_id=candidate["model_id"],
        provider=candidate["provider"], usage_type=candidate["usage_type"],
        prompt_price_microdollars_per_million_tokens=base["input_micro_per_million"],
        completion_price_microdollars_per_million_tokens=base["output_micro_per_million"],
        request_price_microdollars=candidate["request_fee_micro"],
        price_tiers=tuple(PriceTier(
            t["max_prompt_tokens"], t["rates"]["input_micro_per_million"],
            t["rates"]["output_micro_per_million"],
            t["rates"]["cached_input_micro_per_million"] if cached == "frozen" else cached,
        ) for t in tiers),
    )


@pytest.mark.parametrize("case", [c for c in CASES if "freeze_cache_rate" in c], ids=lambda c: c["name"])
def test_freeze_resolves_missing_vs_explicit_cache(case: dict[str, Any]) -> None:
    endpoint = endpoint_from_candidate(case["snapshot"]["candidates"][0], cached=case["freeze_cache_rate"])
    frozen = billing.build_snapshot([endpoint], billing.Eligibility())
    assert frozen.model_dump(mode="json") == case["snapshot"]
    assert billing.evaluate(frozen, endpoint.id, billing.RawUsage(**case["raw_usage"]),
                            billing.Eligibility()).charge_micro == case["expected_charge_micro"]


def test_builder_copies_and_sorts_and_freezes() -> None:
    first = endpoint_from_candidate(CASES[4]["snapshot"]["candidates"][0])
    second = endpoint_from_candidate(CASES[5]["snapshot"]["candidates"][0])
    endpoints = [first, second]
    snapshot = billing.build_snapshot(endpoints, billing.Eligibility())
    before = billing.canonical_bytes(snapshot)
    endpoints.clear()  # Removal of catalog objects cannot affect an admitted snapshot.
    assert billing.canonical_bytes(snapshot) == before
    with pytest.raises(ValidationError):
        snapshot.candidates[0].rates.input_micro_per_million = 999
    with pytest.raises(ValueError, match="unique"):
        billing.build_snapshot([first, first], billing.Eligibility())
    with pytest.raises(ValueError):
        billing.build_snapshot([], billing.Eligibility())


@pytest.mark.parametrize("raw", [b'{}', b'[]', b'null', b'\xff'])
def test_bad_json(raw: bytes) -> None:
    with pytest.raises(ValueError):
        billing.parse_snapshot(raw)


@pytest.mark.parametrize("mutation,reason", [
    ({"tiers": [
        {"max_prompt_tokens": maximum, "rates": CASES[4]["snapshot"]["candidates"][0]["rates"]}
        for maximum in (None, 1)
    ]}, "unbounded tier must be last"),
    ({"prompt_convention": "excludes_cache"}, "unsupported prompt convention"),
    ({"unknown_fee": 1}, "Extra inputs are not permitted"),
])
def test_candidate_rejects_unsupported_semantics(mutation: dict[str, Any], reason: str) -> None:
    data = copy.deepcopy(CASES[4]["snapshot"])
    data["candidates"][0].update(mutation)
    with pytest.raises(ValueError, match=reason):
        billing.parse_snapshot(json.dumps(data))


def test_envelope_binding_and_refund() -> None:
    case = CASES[4]
    snapshot = billing.parse_snapshot(json.dumps(case["snapshot"]))
    payload = dict(DATA["envelope_identity"], selected_endpoint=case["selected_endpoint"],
                   snapshot_hash=billing.canonical_hash(snapshot),
                   usage=case["expected_normalized_usage"], charge_micro=13)
    for changes, reason in [({"snapshot_hash": "0" * 64}, "snapshot_hash"),
                            ({"charge_micro": 12}, "charge_mismatch"),
                            ({"selected_endpoint": "absent"}, "unsupported_endpoint")]:
        with pytest.raises(ValueError, match=reason):
            billing.validate_envelope(snapshot, billing.TerminalEnvelope(**dict(payload, **changes)))
    with pytest.raises(ValueError):
        billing.TerminalEnvelope(**dict(payload, terminal_kind="refund"))
    refund = billing.TerminalEnvelope(**dict(payload, terminal_kind="refund", charge_micro=0))
    billing.validate_envelope(snapshot, refund)
    assert billing.canonical_hash(refund) != case["expected_terminal_envelope_hash"]
    with pytest.raises(ValidationError):
        refund.usage.output_tokens = 999


def test_typed_acceptance_outcomes() -> None:
    for status in billing.AcceptanceStatus:
        durable = status in (billing.AcceptanceStatus.ACCEPTED, billing.AcceptanceStatus.DUPLICATE)
        kwargs = {"payload_hash": "a" * 64, "settlement_status": "pending"} if durable else {}
        assert billing.AcceptanceOutcome(status=status, **kwargs).status == status
        with pytest.raises(ValueError):
            billing.AcceptanceOutcome(status=status, **({} if durable else {
                "payload_hash": "a" * 64, "settlement_status": "pending",
            }))


@pytest.mark.parametrize("field,value", [
    ("authorization_id", "another-auth"), ("generation_id", "another-generation"),
    ("workspace_id", "another-workspace"), ("key_id", "another-key"),
    ("invocation_nonce", "another-nonce"), ("journal_region", "europe-west4"),
    ("epoch", 2), ("route_type", "responses"), ("streamed", True),
])
def test_envelope_hash_binds_identity_and_accounting_metadata(field: str, value: Any) -> None:
    case = CASES[4]
    snapshot = billing.parse_snapshot(json.dumps(case["snapshot"]))
    payload = dict(DATA["envelope_identity"], selected_endpoint=case["selected_endpoint"],
                   snapshot_hash=billing.canonical_hash(snapshot),
                   usage=case["expected_normalized_usage"], charge_micro=13)
    payload[field] = value
    changed = billing.TerminalEnvelope.model_validate(payload)
    assert billing.canonical_hash(changed) != case["expected_terminal_envelope_hash"]
