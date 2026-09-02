from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.catalog import Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.pricing import PriceTier
from trusted_router.receipt_keys import b64url_decode, b64url_encode
from trusted_router.spend_leases import (
    BootAuthHeader,
    SpendLeaseBoot,
    SpendLeaseEchoValue,
    SpendLeaseSigner,
    boot_auth_digest,
    build_spend_lease_shadow_event,
    freeze_spend_lease_catalog,
    mint_shadow_spend_lease,
    parse_boot_auth_header,
    spend_lease_catalog_estimate,
    spend_lease_eligible,
    spend_lease_ineligibility_reason,
    spend_lease_scope_salt,
    verify_boot_auth,
)


def _key(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "limit_microdollars": None,
        "limit_daily_microdollars": None,
        "limit_weekly_microdollars": None,
        "limit_monthly_microdollars": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _candidates(*, usage_type: str = "Credits") -> list[tuple[Model, ModelEndpoint]]:
    model = Model(id="vendor/model", name="Model", provider="vendor", context_length=4096)
    endpoint = ModelEndpoint(
        id="vendor:model:credits",
        model_id=model.id,
        provider="vendor",
        usage_type=usage_type,
        prompt_price_microdollars_per_million_tokens=2_000_000,
        completion_price_microdollars_per_million_tokens=4_000_000,
        request_price_microdollars=3,
        price_tiers=(
            PriceTier(100, 1_000_000, 2_000_000, 500_000),
            PriceTier(None, 2_000_000, 4_000_000, 1_000_000),
        ),
    )
    return [(model, endpoint)]


def _eligible(**overrides: object) -> bool:
    values: dict[str, object] = {
        "workspace_id": "ws-pilot",
        "pilot_workspace_ids": frozenset({"ws-pilot"}),
        "api_key": _key(),
        "route_type": "chat.completions",
        "endpoint_candidates": _candidates(),
        "custom_model": None,
        "user_model": None,
        "partner_mode": None,
        "additional_cost_reservation_microdollars": 0,
        "native_batch_eligible": False,
        "app_markup_basis_points": 0,
        "receipt_fee_basis_points": 0,
        "regional_lease_authorization": False,
    }
    values.update(overrides)
    return spend_lease_eligible(**values)  # type: ignore[arg-type]


def _ineligibility_reason(**overrides: object) -> str | None:
    values: dict[str, object] = {
        "workspace_id": "ws-pilot",
        "pilot_workspace_ids": frozenset({"ws-pilot"}),
        "api_key": _key(),
        "route_type": "chat.completions",
        "endpoint_candidates": _candidates(),
        "custom_model": None,
        "user_model": None,
        "partner_mode": None,
        "additional_cost_reservation_microdollars": 0,
        "native_batch_eligible": False,
        "app_markup_basis_points": 0,
        "receipt_fee_basis_points": 0,
        "regional_lease_authorization": False,
    }
    values.update(overrides)
    return spend_lease_ineligibility_reason(**values)  # type: ignore[arg-type]


def test_spend_lease_scope_salt_pins_sha256_hex_prefix() -> None:
    assert spend_lease_scope_salt("workspace:ws-123:request:req-456") == "8399"


def test_spend_lease_eligibility_accepts_exact_credits_chat_cohort() -> None:
    assert _eligible()


def test_spend_lease_eligibility_requires_pilot_workspace() -> None:
    assert not _eligible(workspace_id="ws-other")


def test_spend_lease_eligibility_route_allowlist_uses_exact_wire_values() -> None:
    assert _eligible(route_type="responses")
    assert not _eligible(route_type=None)
    assert not _eligible(route_type="chat/completions")


def test_spend_lease_eligibility_requires_every_fallback_to_be_credits() -> None:
    mixed = _candidates() + _candidates(usage_type="BYOK")
    assert not _eligible(endpoint_candidates=mixed)


def test_spend_lease_eligibility_rejects_empty_candidate_set() -> None:
    assert not _eligible(endpoint_candidates=[])


def test_spend_lease_eligibility_rejects_custom_models() -> None:
    assert not _eligible(custom_model=object())


def test_spend_lease_eligibility_rejects_user_models() -> None:
    assert not _eligible(user_model=object())


def test_spend_lease_eligibility_rejects_partner_models() -> None:
    assert not _eligible(partner_mode=object())


def test_spend_lease_eligibility_rejects_additional_cost_reservations() -> None:
    assert not _eligible(additional_cost_reservation_microdollars=1)


def test_spend_lease_eligibility_rejects_native_batch() -> None:
    assert not _eligible(native_batch_eligible=True)


def test_spend_lease_eligibility_rejects_app_markup() -> None:
    assert not _eligible(app_markup_basis_points=1)


def test_spend_lease_eligibility_rejects_signed_receipt_fee() -> None:
    assert not _eligible(receipt_fee_basis_points=1)


def test_spend_lease_eligibility_rejects_regional_lease_authorizations() -> None:
    assert not _eligible(regional_lease_authorization=True)


@pytest.mark.parametrize(
    "field",
    [
        "limit_microdollars",
        "limit_daily_microdollars",
        "limit_weekly_microdollars",
        "limit_monthly_microdollars",
    ],
)
def test_spend_lease_eligibility_rejects_each_window_limited_key(field: str) -> None:
    assert not _eligible(api_key=_key(**{field: 1_000_000}))


@pytest.mark.parametrize(
    ("expected", "overrides"),
    [
        ("not_pilot", {"workspace_id": "ws-other"}),
        ("route_type", {"route_type": None}),
        ("no_candidates", {"endpoint_candidates": []}),
        ("candidate_not_credits", {"endpoint_candidates": _candidates(usage_type="BYOK")}),
        ("custom_model", {"custom_model": object()}),
        ("user_model", {"user_model": object()}),
        ("partner_mode", {"partner_mode": object()}),
        (
            "additional_cost",
            {"additional_cost_reservation_microdollars": 1},
        ),
        ("native_batch", {"native_batch_eligible": True}),
        ("app_markup", {"app_markup_basis_points": 1}),
        ("receipt_fee", {"receipt_fee_basis_points": 1}),
        ("regional_lease", {"regional_lease_authorization": True}),
        ("key_window_limit", {"api_key": _key(limit_daily_microdollars=1)}),
    ],
)
def test_spend_lease_ineligibility_reason_names_each_security_clause(
    expected: str,
    overrides: dict[str, object],
) -> None:
    assert _ineligibility_reason(**overrides) == expected


def test_spend_lease_ineligibility_reason_is_none_for_eligible_cohort() -> None:
    assert _ineligibility_reason() is None


def test_spend_lease_signer_round_trip_with_python_ed25519_verifier() -> None:
    seed = bytes(range(32))
    signer = SpendLeaseSigner(lambda: seed)
    artifact = mint_shadow_spend_lease(
        signer=signer,
        key_hash="key-hash",
        workspace_id="ws",
        boot_kid="boot",
        cap_micro=1_000_000,
        gen=7,
        catalog={"version": "catalog-v1", "candidates": []},
        ttl_seconds=60,
        now=1_700_000_000,
    )
    header_segment, claims_segment, signature_segment = artifact.token.split(".")
    header = json.loads(b64url_decode(header_segment))
    claims = json.loads(b64url_decode(claims_segment))
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    public.verify(
        b64url_decode(signature_segment),
        f"{header_segment}.{claims_segment}".encode("ascii"),
    )
    assert header == {"alg": "EdDSA", "kid": signer.kid, "typ": "spend-lease+jws"}
    assert claims["authoritative"] is False
    assert claims["gen"] == 7
    assert claims["typ"] == "spend-lease+jws"


def test_frozen_catalog_has_complete_applicability_key_and_integer_prices_in_tier_order() -> None:
    catalog = freeze_spend_lease_catalog(
        _candidates(),
        region="us-central1",
        route_type="responses",
        service_tier=None,
    )
    candidates = catalog["candidates"]
    assert [candidate["price_tier_max_input_tokens"] for candidate in candidates] == [100, None]
    assert set(candidates[0]) == {
        "endpoint_id",
        "model",
        "provider",
        "region",
        "route_type",
        "service_tier",
        "price_tier_max_input_tokens",
        "input_price_micro_per_mtok",
        "output_price_micro_per_mtok",
        "request_price_micro",
        "cache_read_micro_per_mtok",
        "cache_write_micro_per_mtok",
    }
    assert all(isinstance(candidate["input_price_micro_per_mtok"], int) for candidate in candidates)


def test_estimator_uses_first_matching_tier_and_exact_applicability_dimensions() -> None:
    catalog = freeze_spend_lease_catalog(
        _candidates(),
        region="us-central1",
        route_type="responses",
        service_tier=None,
    )
    low = spend_lease_catalog_estimate(
        catalog,
        model="vendor/model",
        provider="vendor",
        route_type="responses",
        region="us-central1",
        service_tier=None,
        estimated_input_tokens=50,
        max_tokens=10,
    )
    assert low == 3 + 50 + 20
    assert (
        spend_lease_catalog_estimate(
            catalog,
            model="vendor/model",
            provider="other",
            route_type="responses",
            region="us-central1",
            service_tier=None,
            estimated_input_tokens=50,
            max_tokens=10,
        )
        is None
    )


def _boot_auth_fixture() -> tuple[Ed25519PrivateKey, SpendLeaseBoot, bytes, BootAuthHeader]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(public)}
    boot = SpendLeaseBoot(
        kid="boot-kid",
        jwk=jwk,
        approved=True,
        verified=True,
        image_digest="sha256:" + "11" * 32,
        attestation_kind="gcp-cs-jwt",
        registered_at="2026-08-27T00:00:00Z",
    )
    body: dict[str, object] = {
        "api_key_lookup_hash": "lookup",
        "model": "vendor/model",
        "spend_lease_echo": {
            "lease_id": None,
            "state": "empty",
            "remaining_micro": None,
            "enclave_estimate_micro": 10,
            "catalog_version": None,
            "would_admit": False,
        },
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = private.sign(boot_auth_digest("POST", "/v1/internal/gateway/authorize", raw_body))
    auth = BootAuthHeader(kid=boot.kid, signature=b64url_encode(signature))
    return private, boot, raw_body, auth


def test_boot_auth_verifies_exact_body_bytes_and_resolved_lookup_hash() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="different",
        accepted_image_digests={boot.image_digest},
    )


def test_boot_auth_digest_matches_v1_wire_formula_and_uppercases_method() -> None:
    raw_body = b'{ "model": "vendor/model" }'
    path = "/v1/internal/gateway/authorize"
    expected = hashlib.sha256(
        b"tr-authorize-v1" + b"POST" + path.encode() + hashlib.sha256(raw_body).digest()
    ).digest()
    assert boot_auth_digest("post", path, raw_body) == expected


def test_boot_auth_rejects_one_byte_tamper_inside_received_echo_body() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    mutated = bytearray(raw_body)
    target = b'"enclave_estimate_micro":10'
    changed_byte = raw_body.index(target) + len(target) - 1
    mutated[changed_byte] = ord("1")
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=bytes(mutated),
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={boot.image_digest},
    )


def test_boot_auth_refuses_unknown_digest_even_if_persisted_approved() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert boot.approved is True
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests={"sha256:" + "22" * 32},
    )


def test_boot_auth_empty_current_accepted_set_refuses_every_digest() -> None:
    _private, boot, raw_body, auth = _boot_auth_fixture()
    assert not verify_boot_auth(
        boot=boot,
        auth=auth,
        method="POST",
        path="/v1/internal/gateway/authorize",
        exact_body_bytes=raw_body,
        signed_lookup_hash="lookup",
        resolved_lookup_hash="lookup",
        accepted_image_digests=frozenset(),
    )


def test_boot_auth_header_parser_rejects_ambiguous_values() -> None:
    assert parse_boot_auth_header("kid=boot,sig=abc") == BootAuthHeader("boot", "abc")
    assert parse_boot_auth_header("kid=one,kid=two,sig=abc") is None
    assert parse_boot_auth_header("kid=boot,sig=abc,extra=value") is None
    assert parse_boot_auth_header("kid=boot") is None


def test_shadow_event_constructs_declines_and_never_drops_invalid_echo() -> None:
    echo = SpendLeaseEchoValue(None, "empty", None, 9, "catalog", True)
    event = build_spend_lease_shadow_event(
        event_id="decline-id",
        created_at="2026-08-27T00:00:00Z",
        workspace_id="ws",
        key_hash="key",
        boot_kid="boot",
        boot_verified=False,
        no_lease_reason="route_type",
        echo=echo,
        server_estimate_micro=None,
        server_verdict="declined_other",
    )
    assert event.divergence == "echo_invalid"
    assert event.server_verdict == "declined_other"
    assert event.server_estimate_micro is None
    assert event.no_lease_reason == "route_type"
    assert event.payload()["schema_version"] == 1


def test_spend_lease_settings_default_off_and_validate_enabled_dependencies() -> None:
    assert Settings(environment="test").spend_lease_issuance_enabled is False
    with pytest.raises(ValueError, match="PILOT_WORKSPACE_IDS"):
        Settings(
            environment="test",
            spend_lease_issuance_enabled=True,
            spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        )
    with pytest.raises(ValueError, match="operational analytics"):
        Settings(
            environment="test",
            spend_lease_issuance_enabled=True,
            spend_lease_pilot_workspace_ids="ws-a",
            spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
        )
    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        operational_analytics_outbox_enabled=True,
        spend_lease_pilot_workspace_ids=" ws-a,ws-b,ws-a ",
        spend_lease_signing_secret_name="test-secret-name",  # noqa: S106
    )
    assert settings.spend_lease_pilot_workspaces == frozenset({"ws-a", "ws-b"})
