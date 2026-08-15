"""Property tests for BYOK associated-data separation.

AES-GCM gives cross-binding rejection — an envelope sealed for one context
failing to open in another — only if the associated data map is injective.
The BYOK AAD is

    f"trustedrouter:byok:{workspace_id}:{provider}"

and `encrypt_control_secret` seals control secrets in the SAME namespace with
its `purpose` in the `provider` slot. So a BYOK entry whose provider string
equals a control purpose produces two envelopes in one workspace under
identical associated data, and each opens the other.

The console POST route accepted any lowercased string up to 64 characters as
`provider` and passed it straight into `encrypt_byok_secret`, so a tenant could
create that collision through the ordinary UI. The API route already validated
against the catalog. This module pins that both doors agree, and that no
catalog-valid provider slug can collide with a control purpose.

    for every accepted provider string p,
        p is a catalog provider supporting BYOK,
        and aad(workspace, p) differs from aad(workspace, purpose)
        for every control purpose

Since step 2 of the migration this module also covers the v2 format, which
length-prefixes every AAD component and adds a namespace separating provider
keys from control secrets. v1 envelopes still exist in storage until the step-3
backfill, so both formats must open and the v1 collision is still asserted
below rather than quietly dropped.

The v2 encoding must stay byte-identical to aadV2 in quill-cloud-proxy. Both
sides pin the same hex vector; see test_the_v2_vector_matches_the_enclave.
"""

from __future__ import annotations

import dataclasses
import secrets as secrets_module

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trusted_router.byok_crypto import (
    ALGORITHM,
    ALGORITHM_V2,
    NAMESPACE_CONTROL,
    NAMESPACE_PROVIDER,
    _aad,
    _aad_v2,
    decrypt_byok_secret,
    decrypt_control_secret,
    encrypt_byok_secret,
    encrypt_control_secret,
)
from trusted_router.catalog import PROVIDERS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_compat import canonical_byok_provider
from trusted_router.storage import STORE
from trusted_router.storage_models import EncryptedSecretEnvelope

WORKSPACE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(name="console")
def _console() -> TestClient:
    """Authenticated console client. The guard under test lives in the route,
    so these drive the real endpoint rather than a reimplementation of it."""
    # Rate limiting off: the property drives hundreds of POSTs at one endpoint,
    # and a 429 would report as a namespace failure rather than what it is.
    settings = Settings(environment="local", rate_limit_enabled=False)
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user("byok-namespace@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=user.email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client


def _post_provider(console: TestClient, provider: str) -> str:
    """POST the console form and return the redirect target."""
    response = console.post(
        "/console/byok",
        data={"provider": provider, "api_key": "sk-test-namespace-key-0001"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]

# The control-secret purposes that share the BYOK namespace. Broadcast
# destination keys are the reachable family: their ids are short enough to fit
# inside the console form's 64-character limit.
CONTROL_PURPOSES = [
    "broadcast:bdst_abc123:api-key",
    "broadcast:bdst_abc123:signing-key",
    "smtp:password",
    "webhook:signing-secret",
]

BYOK_PROVIDERS = sorted(slug for slug, p in PROVIDERS.items() if p.supports_byok)


# ---------------------------------------------------- namespace separation ---


@given(
    provider=st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=64
    )
)
@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_whatever_the_console_stores_never_collides_with_a_control_purpose(
    console: TestClient, provider: str
) -> None:
    """Anything the route actually stores has AAD distinct from every control
    purpose.

    Driven through the real endpoint, and asserted on what landed in storage,
    so the property cannot pass by agreeing with a copy of the guard. Quantified
    over arbitrary text because the input is a free-form form field and the
    interesting values are the ones nobody thought to enumerate.
    """
    location = _post_provider(console, provider)
    workspace_id = next(iter(STORE.workspaces))
    slug = canonical_byok_provider(provider)
    stored = STORE.get_byok_provider(workspace_id, slug)

    if location.endswith("error=provider"):
        assert stored is None, f"rejected {provider!r} but still stored a row"
        return

    assert stored is not None
    byok_aad = _aad(workspace_id, stored.provider)
    for purpose in CONTROL_PURPOSES:
        assert byok_aad != _aad(workspace_id, purpose), (
            f"stored provider {stored.provider!r} shares associated data with "
            f"control purpose {purpose!r}"
        )


@pytest.mark.parametrize("purpose", CONTROL_PURPOSES)
def test_a_control_purpose_cannot_be_registered_as_a_provider(
    console: TestClient, purpose: str
) -> None:
    """The direct attempt: register a BYOK entry named after a control secret.

    This is the reachable path — broadcast destination keys are sealed with
    their purpose in the same AAD slot, and those purposes fit inside the
    form's 64-character limit.
    """
    assert _post_provider(console, purpose).endswith("error=provider")
    workspace_id = next(iter(STORE.workspaces))
    assert STORE.get_byok_provider(workspace_id, purpose) is None


@pytest.mark.parametrize("provider", BYOK_PROVIDERS[:8])
def test_every_catalog_byok_provider_is_still_accepted(
    console: TestClient, provider: str
) -> None:
    """The guard must not have narrowed the legitimate set."""
    assert _post_provider(console, provider) == "/console/byok"


def test_console_and_api_agree_on_which_providers_are_acceptable(
    console: TestClient,
) -> None:
    """Both doors, one rule. The console used to accept a superset."""
    from trusted_router.routes.byok import _require_byok_provider

    for provider in [*BYOK_PROVIDERS[:8], *CONTROL_PURPOSES, "nonsense", "OpenAI"]:
        console_ok = not _post_provider(console, provider).endswith("error=provider")
        try:
            _require_byok_provider(provider)
            api_ok = True
        except Exception:
            api_ok = False
        assert console_ok == api_ok, f"routes disagree about provider {provider!r}"


# --------------------------------------------- known gap, asserted not hidden ---


def test_aad_encoding_is_not_injective_in_general() -> None:
    """A standing record of the unfixed half.

    `_aad` joins with ":" without escaping or length-prefixing, so component
    boundaries are ambiguous. Reaching this needs a colon inside a workspace
    id, and all three backends mint str(uuid.uuid4()), so it is not reachable
    by normal issuance — but the encoding is still wrong, and repairing it
    needs a V2 envelope algorithm plus a dual-read migration because existing
    ciphertexts and wrapped DEKs were sealed under the current AAD.

    If this test ever starts FAILING, the encoding was fixed and this test
    should be deleted along with the workaround it documents.
    """
    assert _aad("a:b", "c") == _aad("a", "b:c")


# ---------------------------------------------------------------- v2 format ---
#
# Step 2 of docs/design/byok-aad-v2-migration.md. New envelopes are written v2,
# whose AAD is length-prefixed and carries a namespace component. v1 envelopes
# still exist in storage until the step-3 backfill, so both formats must open.


def _settings() -> Settings:
    return Settings(environment="test")


def test_the_v2_vector_matches_the_enclave() -> None:
    """The two implementations must agree byte for byte.

    This exact hex is pinned in
    quill-cloud-proxy/enclave-go/internal/byokcache/aad_v2_test.go. If the two
    ever diverge it is not a test failure in the usual sense — it is every BYOK
    key written by one plane failing to open on the other, which is precisely
    the outage the migration ordering exists to avoid.
    """
    assert _aad_v2("provider", "ws-1", "openai").hex() == (
        "0000001574727573746564726f757465722f62796f6b2f7632"
        "0000000870726f76696465720000000477732d31000000066f70656e6169"
    )


@given(
    namespace=st.sampled_from([NAMESPACE_PROVIDER, NAMESPACE_CONTROL]),
    workspace=st.text(alphabet=":/abc\x00", max_size=6),
    context=st.text(alphabet=":/abc\x00", max_size=6),
)
@settings(max_examples=400)
def test_v2_aad_is_injective(namespace: str, workspace: str, context: str) -> None:
    """The property v1 lacks: distinct tuples produce distinct bytes.

    Quantified over an alphabet of exactly the characters that break a
    delimiter-joined encoding — colons, slashes, NUL — because those are the
    inputs where a naive scheme silently collapses two tuples into one.
    """
    encoded = _aad_v2(namespace, workspace, context)
    for other in [
        (namespace, workspace, context + "x"),
        (namespace, workspace + "x", context),
        (NAMESPACE_CONTROL if namespace == NAMESPACE_PROVIDER else NAMESPACE_PROVIDER,
         workspace, context),
    ]:
        if other != (namespace, workspace, context):
            assert _aad_v2(*other) != encoded


def test_v2_separates_the_two_v1_collision_classes() -> None:
    """Both concrete v1 collisions are gone under v2."""
    assert _aad("a:b", "c") == _aad("a", "b:c")  # v1, still true
    assert _aad_v2("provider", "a:b", "c") != _aad_v2("provider", "a", "b:c")
    assert _aad_v2(NAMESPACE_PROVIDER, "w", "x") != _aad_v2(NAMESPACE_CONTROL, "w", "x")


def test_new_byok_secrets_are_written_v2() -> None:
    envelope = encrypt_byok_secret(
        "sk-provider-key", _settings(), workspace_id=WORKSPACE, provider="openai"
    )
    assert envelope.algorithm == ALGORITHM_V2
    assert (
        decrypt_byok_secret(
            envelope, _settings(), workspace_id=WORKSPACE, provider="openai"
        )
        == "sk-provider-key"
    )


def test_new_control_secrets_are_written_v2() -> None:
    envelope = encrypt_control_secret(
        "shhh", _settings(), workspace_id=WORKSPACE, purpose="broadcast:bdst_1:api-key"
    )
    assert envelope.algorithm == ALGORITHM_V2
    assert (
        decrypt_control_secret(
            envelope,
            _settings(),
            workspace_id=WORKSPACE,
            purpose="broadcast:bdst_1:api-key",
        )
        == "shhh"
    )


def test_a_control_secret_does_not_open_as_a_provider_key() -> None:
    """The whole point of the namespace component.

    Under v1 these two shared associated data whenever the purpose string
    equalled the provider slug, so each opened the other. The console guard
    (#544) made that unreachable; the namespace makes it impossible.
    """
    shared_context = "openai"
    control = encrypt_control_secret(
        "control-secret", _settings(), workspace_id=WORKSPACE, purpose=shared_context
    )

    with pytest.raises(InvalidTag):
        decrypt_byok_secret(
            control, _settings(), workspace_id=WORKSPACE, provider=shared_context
        )


def test_a_provider_key_does_not_open_as_a_control_secret() -> None:
    shared_context = "openai"
    provider = encrypt_byok_secret(
        "sk-provider-key", _settings(), workspace_id=WORKSPACE, provider=shared_context
    )

    with pytest.raises(InvalidTag):
        decrypt_control_secret(
            provider, _settings(), workspace_id=WORKSPACE, purpose=shared_context
        )


def test_v1_envelopes_still_decrypt() -> None:
    """Rows written before the migration must keep working until the backfill.

    Sealed here the way v1 sealed them, since nothing writes v1 any more.
    """
    envelope = _seal_v1("legacy-secret", workspace_id=WORKSPACE, context="openai")
    assert envelope.algorithm == ALGORITHM
    assert (
        decrypt_byok_secret(
            envelope, _settings(), workspace_id=WORKSPACE, provider="openai"
        )
        == "legacy-secret"
    )


def test_an_unknown_algorithm_is_still_refused() -> None:
    envelope = _seal_v1("x", workspace_id=WORKSPACE, context="openai")
    envelope = dataclasses.replace(envelope, algorithm="TR-BYOK-ENVELOPE-AES-256-GCM-V3")
    with pytest.raises(ValueError, match="unsupported BYOK envelope algorithm"):
        decrypt_byok_secret(
            envelope, _settings(), workspace_id=WORKSPACE, provider="openai"
        )


@given(
    workspace=st.sampled_from(["ws-1", "ws-2"]),
    context=st.sampled_from(["openai", "anthropic"]),
)
def test_v2_still_rejects_a_wrong_binding(workspace: str, context: str) -> None:
    """Cross-binding rejection is what AAD is for; v2 must not weaken it."""
    envelope = encrypt_byok_secret(
        "sk", _settings(), workspace_id=workspace, provider=context
    )
    for other_ws, other_ctx in [("ws-other", context), (workspace, "other-provider")]:
        with pytest.raises(InvalidTag):
            decrypt_byok_secret(
                envelope, _settings(), workspace_id=other_ws, provider=other_ctx
            )


def _seal_v1(secret: str, *, workspace_id: str, context: str):
    """Seal an envelope the way v1 did, for backward-compatibility tests."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from trusted_router.byok_crypto import _b64, _key_ref, _wrap_dek

    dek = secrets_module.token_bytes(32)
    nonce = secrets_module.token_bytes(12)
    dek_nonce = secrets_module.token_bytes(12)
    aad = _aad(workspace_id, context)
    return EncryptedSecretEnvelope(
        algorithm=ALGORITHM,
        key_ref=_key_ref(_settings()),
        encrypted_dek=_b64(_wrap_dek(dek, dek_nonce, aad, _settings())),
        dek_nonce=_b64(dek_nonce),
        ciphertext=_b64(AESGCM(dek).encrypt(nonce, secret.encode(), aad)),
        nonce=_b64(nonce),
    )
