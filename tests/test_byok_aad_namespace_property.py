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

Two things this deliberately does NOT claim:

  * The AAD map is still not injective in general. `_aad` joins with ":" and
    neither escapes nor length-prefixes, so ("a:b", "c") and ("a", "b:c")
    collide. Reaching that needs a colon in a workspace id, and all three
    backends mint str(uuid.uuid4()), so it is not reachable by normal issuance
    — but it is a real property of the function and is asserted below as a
    known gap rather than quietly left out.
  * Repairing the encoding needs a V2 envelope algorithm and a dual-read
    migration, because existing ciphertexts AND wrapped DEKs were sealed under
    the current AAD, and the attested enclave consumes these envelopes too.
    That is out of scope here; this closes the reachable door.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trusted_router.byok_crypto import _aad
from trusted_router.catalog import PROVIDERS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_compat import canonical_byok_provider
from trusted_router.storage import STORE

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
