"""Native BYOK credentials for Amazon Bedrock, Google Vertex and Azure."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from trusted_router.byok_cloud_credentials import CloudCredentialError, normalize_cloud_credential
from trusted_router.catalog import PROVIDERS
from trusted_router.config import Settings
from trusted_router.main import create_app

USER = {"x-trustedrouter-user": "alice@example.com"}
# Built at runtime so no literal in this file looks like a real AWS credential.
FAKE_ACCESS_KEY_ID = "AKIA" + "TESTONLY" + "0" * 8
FAKE_SECRET_ACCESS_KEY = "test-only-" + "x" * 30
FAKE_AZURE_KEY = "test-only-azure-" + "0" * 8


def _pem(bits: int = 2048) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()


def _service_account(**overrides) -> dict:
    account = {
        "type": "service_account",
        "project_id": "customer-project-1",
        "private_key_id": "kid-1",
        "private_key": _pem(),
        "client_email": "router@customer-project-1.iam.gserviceaccount.com",
        "token_uri": "https://attacker.example/token",
    }
    account.update(overrides)
    return account


def test_cloud_providers_accept_byok() -> None:
    for slug in ("amazon-bedrock", "google-vertex", "azure"):
        assert PROVIDERS[slug].supports_byok is True


# --- Normalization -------------------------------------------------------------


def test_bedrock_shapes() -> None:
    secret, hint = normalize_cloud_credential("amazon-bedrock", "ABSKexamplebedrockkey")
    assert secret == "ABSKexamplebedrockkey" and "us-east-1" in hint  # noqa: S105 - test fixture
    secret, hint = normalize_cloud_credential("amazon-bedrock", {
        "access_key_id": FAKE_ACCESS_KEY_ID,
        "secret_access_key": FAKE_SECRET_ACCESS_KEY,
        "region": "eu-west-1",
    })
    assert json.loads(secret)["region"] == "eu-west-1"
    assert FAKE_SECRET_ACCESS_KEY not in hint and "eu-west-1" in hint


@pytest.mark.parametrize("credential", [
    {"region": "us-east-1"},
    {"bedrock_api_key": "k", "access_key_id": FAKE_ACCESS_KEY_ID, "secret_access_key": "x" * 20},
    {"bedrock_api_key": "k", "region": "evil.example.com"},
    {"access_key_id": "nope", "secret_access_key": "x" * 20},
    {"bedrock_api_key": "k", "endpoint": "https://evil.example.com"},
    "two words",
])
def test_bedrock_refusals(credential) -> None:
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("amazon-bedrock", credential)


def test_vertex_drops_the_key_files_token_uri() -> None:
    secret, hint = normalize_cloud_credential("google-vertex", _service_account())
    sealed = json.loads(secret)
    assert "attacker" not in secret
    assert sealed["location"] == "global"
    assert sealed["service_account"]["client_email"] in hint
    secret, _hint = normalize_cloud_credential(
        "google-vertex", {"service_account": _service_account(), "location": "us-east5"},
    )
    assert json.loads(secret)["location"] == "us-east5"


@pytest.mark.parametrize("overrides", [
    {"type": "authorized_user"},
    {"project_id": "BAD/project"},
    {"client_email": "not an email"},
    {"private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"},
])
def test_vertex_refusals(overrides) -> None:
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("google-vertex", _service_account(**overrides))


def test_vertex_refuses_a_small_key_and_a_host_like_location() -> None:
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("google-vertex", _service_account(private_key=_pem(1024)))
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential(
            "google-vertex", {"service_account": _service_account(), "location": "evil.example.com"},
        )


@pytest.mark.parametrize("endpoint", [
    "https://customer.openai.azure.com",
    "https://customer.openai.azure.com/openai/v1",
    "https://customer.services.ai.azure.com/",
])
def test_azure_accepts_resource_roots(endpoint: str) -> None:
    secret, hint = normalize_cloud_credential("azure", {"endpoint": endpoint, "api_key": FAKE_AZURE_KEY})
    assert json.loads(secret)["endpoint"].count("/") == 2
    assert FAKE_AZURE_KEY not in hint


@pytest.mark.parametrize("endpoint", [
    "http://customer.openai.azure.com",
    "https://evil.example.com",
    "https://customer.openai.azure.com.evil.example.com",
    "https://u:p@customer.openai.azure.com",
    "https://customer.openai.azure.com:444",
    "https://customer.openai.azure.com/other",
])
def test_azure_refuses_other_hosts(endpoint: str) -> None:
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("azure", {"endpoint": endpoint, "api_key": FAKE_AZURE_KEY})


# --- API -------------------------------------------------------------------------


def _client() -> TestClient:
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    assert client.post("/v1/keys", headers=USER, json={"name": "cloud"}).status_code == 201
    return client


def test_upload_native_credentials_through_the_key_api() -> None:
    client = _client()
    vertex = client.put(
        "/v1/byok/providers/google-vertex", headers=USER, json={"credential": _service_account()},
    )
    assert vertex.status_code == 201, vertex.text
    assert vertex.json()["data"]["key_hint"].startswith("router@customer-project-1")
    assert vertex.json()["data"]["secret_storage"] == "envelope"  # noqa: S105

    azure = client.put(
        "/v1/byok/providers/azure",
        headers=USER,
        json={"credential": {"endpoint": "https://c.openai.azure.com", "api_key": FAKE_AZURE_KEY}},
    )
    assert azure.status_code == 201, azure.text
    assert azure.json()["data"]["key_hint"].startswith("c.openai.azure.com")

    bedrock = client.put(
        "/v1/byok/providers/amazon-bedrock", headers=USER, json={"api_key": "ABSKexamplebedrockkey"},
    )
    assert bedrock.status_code == 201, bedrock.text
    body = json.dumps(bedrock.json())
    assert "ABSKexamplebedrockkey" not in body


def test_bad_native_credentials_fail_at_upload() -> None:
    client = _client()
    bad = client.put(
        "/v1/byok/providers/azure", headers=USER,
        json={"credential": {"endpoint": "https://evil.example.com", "api_key": FAKE_AZURE_KEY}},
    )
    assert bad.status_code == 400
    assert "Azure endpoint" in bad.json()["error"]["message"]
    not_cloud = client.put(
        "/v1/byok/providers/openai", headers=USER, json={"credential": {"api_key": "sk-x"}},
    )
    assert not_cloud.status_code == 400
    both = client.put(
        "/v1/byok/providers/amazon-bedrock", headers=USER,
        json={"api_key": "ABSKx", "credential": {"bedrock_api_key": "ABSKy"}},
    )
    assert both.status_code == 400


def test_console_validates_native_credentials() -> None:
    from trusted_router.storage import STORE

    client = TestClient(create_app(Settings(environment="local", rate_limit_enabled=False), init_observability=False))
    user = STORE.ensure_user("byok-console-cloud@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    assert client.get("/console/byok").status_code == 200
    workspace_id = STORE.list_workspaces_for_user(user.id)[0].id

    bad = client.post("/console/byok", data={"provider": "azure", "api_key": "not-json"}, follow_redirects=False)
    assert bad.status_code == 303 and bad.headers["location"].endswith("error=credential")
    shown = client.get(bad.headers["location"]).text
    assert "That credential was not saved" in shown and "not-json" not in shown
    assert STORE.get_byok_provider(workspace_id, "azure") is None

    good = client.post(
        "/console/byok",
        data={"provider": "azure", "api_key": json.dumps({"endpoint": "https://c.openai.azure.com/openai/v1", "api_key": FAKE_AZURE_KEY})},
        follow_redirects=False,
    )
    assert good.status_code == 303 and "error" not in good.headers["location"]
    row = STORE.get_byok_provider(workspace_id, "azure")
    assert row is not None and row.key_hint is not None and "c.openai.azure.com" in row.key_hint


@pytest.mark.parametrize("endpoint", ["https://customer.openai.azure.com:abc", "https://customer.openai.azure.com:99999", "https://[::1"])
def test_malformed_azure_urls_are_validation_errors(endpoint: str) -> None:
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("azure", {"endpoint": endpoint, "api_key": FAKE_AZURE_KEY})


def test_console_accepts_a_full_vertex_service_account() -> None:
    from trusted_router.storage import STORE

    client = TestClient(create_app(Settings(environment="local", rate_limit_enabled=False), init_observability=False))
    user = STORE.ensure_user("byok-console-vertex@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    page = client.get("/console/byok")
    assert page.status_code == 200 and 'value="google-vertex"' in page.text
    workspace_id = STORE.list_workspaces_for_user(user.id)[0].id
    saved = client.post(
        "/console/byok",
        data={"provider": "google-vertex", "api_key": json.dumps(_service_account())},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "error" not in saved.headers["location"]
    assert STORE.get_byok_provider(workspace_id, "google-vertex") is not None


def test_an_unsupported_key_algorithm_is_a_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.exceptions import UnsupportedAlgorithm

    from trusted_router import byok_cloud_credentials

    def unsupported(*_args, **_kwargs):
        raise UnsupportedAlgorithm("unknown key type")

    monkeypatch.setattr(byok_cloud_credentials.serialization, "load_pem_private_key", unsupported)
    with pytest.raises(CloudCredentialError):
        normalize_cloud_credential("google-vertex", _service_account())


@pytest.mark.parametrize("region", ["us-central1", "aws-eu-west-1"])
def test_a_cloud_byok_route_is_offered_in_every_region(monkeypatch: pytest.MonkeyPatch, region: str) -> None:
    # Every region gets the same routes; an AWS gateway refuses the BYOK route
    # itself as its own limit and falls over to the next route.
    from tests.fixture_routes import drop_routes, serve_on_fixture_route
    from trusted_router.storage import STORE

    model_id = "openai/gpt-5-mini"
    drop_routes(monkeypatch, model_id)
    for usage_type in ("Credits", "BYOK"):
        serve_on_fixture_route(
            monkeypatch, model_id, "azure", author="openai", usage_type=usage_type, upstream_id="gpt-5-mini",
        )
    settings = Settings(environment="test", regions="us-central1,aws-eu-west-1")
    client = TestClient(create_app(settings, init_observability=False))
    key = client.post("/v1/keys", headers=USER, json={"name": "cloud byok"}).json()["data"]
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"], provider="azure", secret_ref="env://AZURE", key_hint=None,  # noqa: S106
    )
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key["hash"], "model": model_id, "route_type": "chat.completions",
        "estimated_input_tokens": 100, "max_output_tokens": 100, "region": region,
    })
    assert response.status_code == 200, response.text
    assert "BYOK" in [c["usage_type"] for c in response.json()["data"]["route_candidates"]]


def test_an_oversized_vertex_key_is_refused_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import byok_cloud_credentials

    def never(*_args, **_kwargs):
        raise AssertionError("an oversized key reached the RSA parser")

    monkeypatch.setattr(byok_cloud_credentials.serialization, "load_pem_private_key", never)
    oversized = "-----BEGIN PRIVATE KEY-----\n" + "A" * 6000 + "\n-----END PRIVATE KEY-----\n"
    with pytest.raises(CloudCredentialError, match="too large"):
        normalize_cloud_credential("google-vertex", _service_account(private_key=oversized))
