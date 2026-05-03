from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.secrets import LocalKeyFile
from trusted_router.sentry_config import before_send
from trusted_router.storage import STORE


def test_stubbed_endpoints_are_explicit(client: TestClient) -> None:
    cases = [
        ("POST", "/v1/rerank", 501, "endpoint_not_supported"),
        ("POST", "/v1/audio/speech", 501, "endpoint_not_supported"),
        ("POST", "/v1/videos", 501, "endpoint_not_supported"),
        ("GET", "/v1/guardrails", 501, "endpoint_not_supported"),
        ("POST", "/v1/credits/coinbase", 410, "deprecated"),
        ("GET", "/v1/private/models/foo/bar", 404, "private_models_not_supported"),
    ]
    for method, path, status, type_ in cases:
        resp = client.request(method, path)
        assert resp.status_code == status, path
        assert resp.json()["error"]["type"] == type_


def test_content_storage_cannot_be_enabled(client: TestClient, user_headers: dict[str, str]) -> None:
    workspaces = client.get("/v1/workspaces", headers=user_headers).json()["data"]
    workspace_id = workspaces[0]["id"]
    resp = client.patch(
        f"/v1/workspaces/{workspace_id}",
        headers=user_headers,
        json={"content_storage_enabled": True},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "content_storage_disabled"


def test_content_storage_rejection_does_not_partially_rename_workspace(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    workspace = client.get("/v1/workspaces", headers=user_headers).json()["data"][0]

    resp = client.patch(
        f"/v1/workspaces/{workspace['id']}",
        headers=user_headers,
        json={"name": "Should Not Stick", "content_storage_enabled": True},
    )

    assert resp.status_code == 400
    unchanged = client.get(f"/v1/workspaces/{workspace['id']}", headers=user_headers).json()["data"]
    assert unchanged["name"] == workspace["name"]


def test_users_cannot_select_another_users_workspace(client: TestClient) -> None:
    alice_headers = {"x-trustedrouter-user": "alice@example.com"}
    bob_headers = {"x-trustedrouter-user": "bob@example.com"}
    workspace_id = client.get("/v1/workspaces", headers=alice_headers).json()["data"][0]["id"]

    resp = client.get(
        f"/v1/workspaces/{workspace_id}",
        headers={**bob_headers, "x-trustedrouter-workspace": workspace_id},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_management_keys_are_pinned_to_their_workspace(client: TestClient, user_headers: dict[str, str]) -> None:
    personal_key = client.post("/v1/keys", headers=user_headers, json={"name": "personal"}).json()
    personal_workspace_id = personal_key["data"]["workspace_id"]
    org = client.post("/v1/workspaces", headers=user_headers, json={"name": "Org"}).json()["data"]
    org_headers = {**user_headers, "x-trustedrouter-workspace": org["id"]}
    org_management_key = client.post(
        "/v1/keys",
        headers=org_headers,
        json={"name": "org management", "management": True},
    ).json()["key"]
    management_headers = {"authorization": f"Bearer {org_management_key}"}

    workspace_resp = client.get(f"/v1/workspaces/{personal_workspace_id}", headers=management_headers)
    assert workspace_resp.status_code == 403
    assert workspace_resp.json()["error"]["type"] == "forbidden"

    key_resp = client.get(f"/v1/keys/{personal_key['data']['hash']}", headers=management_headers)
    assert key_resp.status_code == 404
    assert key_resp.json()["error"]["type"] == "not_found"

    create_resp = client.post(
        "/v1/keys",
        headers=management_headers,
        json={"name": "cross workspace", "workspace_id": personal_workspace_id},
    )
    assert create_resp.status_code == 403
    assert create_resp.json()["error"]["type"] == "forbidden"

    checkout_resp = client.post(
        "/v1/billing/checkout",
        headers=management_headers,
        json={"workspace_id": personal_workspace_id, "amount": 25},
    )
    assert checkout_resp.status_code == 403
    assert checkout_resp.json()["error"]["type"] == "forbidden"


def test_users_have_uuid_ids_not_email_identifiers(client: TestClient, user_headers: dict[str, str]) -> None:
    org = client.post("/v1/workspaces", headers=user_headers, json={"name": "Org"}).json()["data"]
    org_headers = {**user_headers, "x-trustedrouter-workspace": org["id"]}
    add = client.post(
        f"/v1/workspaces/{org['id']}/members/add",
        headers=org_headers,
        json={"emails": ["bob@example.com"], "role": "member"},
    )
    assert add.status_code == 200
    member = add.json()["data"][0]
    assert member["email"] == "bob@example.com"
    assert member["user_id"] != "bob@example.com"

    remove = client.post(
        f"/v1/workspaces/{org['id']}/members/remove",
        headers=org_headers,
        json={"members": ["bob@example.com"]},
    )
    assert remove.status_code == 200
    members = client.get("/v1/organization/members", headers=org_headers).json()["data"]
    assert all(item["email"] != "bob@example.com" for item in members)


def test_api_key_secrets_are_salted(client: TestClient, user_headers: dict[str, str]) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "salted"}).json()
    key_id = created["data"]["hash"]
    api_key = STORE.api_keys.keys[key_id]
    assert api_key.salt
    assert api_key.secret_hash
    assert api_key.lookup_hash
    assert api_key.secret_hash != key_id
    assert api_key.lookup_hash != api_key.secret_hash
    assert STORE.get_key_by_raw(created["key"]) is api_key


def test_local_key_file_accepts_operator_aliases(tmp_path: Path) -> None:
    key_file = tmp_path / "keys.private"
    key_file.write_text(
        "\n".join(
            [
                "CLAUDE_API_KEY=anthropic-value",
                "CHATGPT_API_KEY=openai-value",
                "STRIPE_KEY=stripe-value",
                "GOOGLE_CLOUD_PROJECT=vertex-project",
                "GOOGLE_CLOUD_REGION=europe-west4",
            ]
        ),
        encoding="utf-8",
    )
    keys = LocalKeyFile(key_file)
    assert keys.get("ANTHROPIC_API_KEY") == "anthropic-value"
    assert keys.get("OPENAI_API_KEY") == "openai-value"
    assert keys.get("STRIPE_SECRET_KEY") == "stripe-value"
    assert keys.get("VERTEX_PROJECT_ID") == "vertex-project"
    assert keys.get("VERTEX_LOCATION") == "europe-west4"


def test_dashboard_and_trust_pages_are_real_surfaces(client: TestClient) -> None:
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    # Marketing page hero copy stays.
    assert "Get an API key" in dashboard.text
    assert "$25 USDC" in dashboard.text
    assert "multi-region" in dashboard.text  # pill copy or section header
    assert "regions-map-svg" in dashboard.text  # the new world map renders
    assert "https://quill.lorehex.co" in dashboard.text
    assert "https://github.com/Lore-Hex/trusted-router-py" in dashboard.text
    assert "api.quillrouter.com" in dashboard.text
    # The model catalog mentions the providers we serve.
    assert "DeepSeek" in dashboard.text
    assert "Mistral" in dashboard.text
    assert "Google Vertex" in dashboard.text
    # Inline console is gone — these used to be rendered server-side here.
    assert "Workspace Console" not in dashboard.text
    assert 'id="signupForm"' not in dashboard.text
    assert 'id="workspaceSelect"' not in dashboard.text
    assert 'id="keyReveal"' not in dashboard.text
    assert "Model Marketplace" not in dashboard.text
    # Sign-in modal is present (MetaMask is always available; OAuth providers
    # are conditional on settings, so we don't assert their buttons by default).
    assert 'id="signinModal"' in dashboard.text
    assert "Continue with MetaMask" in dashboard.text
    assert 'data-action="open-signin"' in dashboard.text
    # Asset references unchanged for cache-busting compatibility.
    assert '<script src="/static/dashboard.js">' in dashboard.text
    assert 'href="/static/dashboard.css"' in dashboard.text

    js = client.get("/static/dashboard.js")
    assert js.status_code == 200
    assert "moneyFromMicrodollars" in js.text
    # Marketing-side JS now drives the wallet flow but no longer talks to /v1/signup.
    assert "/v1/auth/wallet/challenge" in js.text
    assert "/v1/auth/wallet/verify" in js.text
    assert "eth_requestAccounts" in js.text
    assert "alert(" not in js.text

    css = client.get("/static/dashboard.css")
    assert css.status_code == 200
    assert ".quill-ad" in css.text
    assert ".signin-modal" in css.text


def test_signup_creates_management_key_and_rejects_duplicate_email(client: TestClient) -> None:
    created = client.post("/v1/signup", json={"email": "Alpha@Example.com"})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["key"].startswith("sk-tr-v1-")
    assert data["email"] == "alpha@example.com"
    assert data["management"] is True
    assert data["user_id"] != "alpha@example.com"
    assert isinstance(data["trial_credit_microdollars"], int)

    headers = {"authorization": f"Bearer {data['key']}"}
    workspaces = client.get("/v1/workspaces", headers=headers)
    assert workspaces.status_code == 200
    assert workspaces.json()["data"][0]["id"] == data["workspace_id"]

    duplicate = client.post("/v1/signup", json={"email": "alpha@example.com"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["type"] == "already_registered"


def test_signup_validates_email(client: TestClient) -> None:
    resp = client.post("/v1/signup", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "bad_request"


def test_production_dashboard_does_not_default_to_dev_user_header() -> None:
    from trusted_router.dashboard import dashboard_html

    html = dashboard_html(
        Settings(
            environment="production",
            internal_gateway_token="internal-prod-token",  # noqa: S106
            stripe_webhook_secret="whsec_test",  # noqa: S106
            stripe_secret_key="sk_test",  # noqa: S106
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
        )
    )

    assert '"environment": "production"' in html
    assert '"defaultDevUser": ""' in html
    assert "alpha@trustedrouter.local" not in html


def test_dashboard_emits_open_graph_and_twitter_card(client: TestClient) -> None:
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'property="og:type" content="website"' in dashboard.text
    assert 'property="og:title"' in dashboard.text
    assert 'property="og:description"' in dashboard.text
    assert 'property="og:url" content="https://trustedrouter.com/"' in dashboard.text
    assert 'property="og:image" content="https://trustedrouter.com/og.png"' in dashboard.text
    assert 'property="og:image:type" content="image/png"' in dashboard.text
    assert 'property="og:image:width" content="1200"' in dashboard.text
    assert 'property="og:image:height" content="630"' in dashboard.text
    assert 'name="twitter:card" content="summary_large_image"' in dashboard.text
    assert 'name="twitter:image" content="https://trustedrouter.com/og.png"' in dashboard.text
    assert '<meta name="description"' in dashboard.text
    assert "<title>TrustedRouter" in dashboard.text


def test_og_image_route_serves_png(client: TestClient) -> None:
    response = client.get("/og.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "max-age=3600, public"
    # PNG signature: 89 50 4E 47 0D 0A 1A 0A
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(response.content) > 1000  # real image, not a stub

    trust = client.get("/trust")
    assert trust.status_code == 200
    assert "Trust boundary" in trust.text
    assert "gcp-release.json" in trust.text
    assert "https://github.com/Lore-Hex/quill-router" in trust.text
    assert "https://github.com/Lore-Hex/quill-cloud-proxy" in trust.text
    assert "https://github.com/Lore-Hex/quill-cloud-infra" in trust.text
    assert "https://github.com/Lore-Hex/quill" in trust.text
    assert "https://github.com/Lore-Hex/trusted-router-js" in trust.text

    release = client.get("/trust/gcp-release.json")
    assert release.status_code == 200
    assert release.json()["platform"] == "gcp-confidential-space"
    assert release.json()["source_repositories"]["control_plane"] == "https://github.com/Lore-Hex/quill-router"
    assert release.json()["source_repositories"]["attested_gateway"] == "https://github.com/Lore-Hex/quill-cloud-proxy"


def test_rate_limit_returns_stable_openrouter_style_error() -> None:
    limited_app = create_app(
        Settings(
            environment="test",
            rate_limit_ip_per_window=1,
            rate_limit_window_seconds=60,
        )
    )
    limited_client = TestClient(limited_app)
    assert limited_client.get("/v1/models").status_code == 200
    second = limited_client.get("/v1/models")
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"
    assert second.headers["retry-after"]


def test_production_config_fails_closed() -> None:
    internal_token = "tok" + "en"
    webhook_secret = "whsec_" + "test"
    stripe_key = "sk_" + "test_secret"
    sentry_dsn = "https://example@example.ingest.sentry.io/1"
    with pytest.raises(ValidationError):
        Settings(environment="production")
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn=sentry_dsn,
            storage_backend="memory",
        )
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn=sentry_dsn,
            storage_backend="spanner-bigtable",
        )


def test_production_control_plane_does_not_register_inference_routes() -> None:
    internal_token = "tok" + "en"
    webhook_secret = "whsec_" + "test"
    stripe_key = "sk_" + "test_secret"
    sentry_dsn = "https://example@example.ingest.sentry.io/1"
    prod_app = create_app(
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn=sentry_dsn,
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
        ),
        configure_store_arg=False,
        init_observability=False,
    )
    registered = {
        (route.path_format, method)
        for route in prod_app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/v1/chat/completions", "POST") not in registered
    assert ("/v1/messages", "POST") not in registered
    assert ("/v1/responses", "POST") not in registered
    assert ("/v1/embeddings", "POST") not in registered
    assert ("/v1/internal/gateway/authorize", "POST") in registered


def test_prompt_output_never_enter_metadata_store(client: TestClient, inference_headers: dict[str, str]) -> None:
    prompt = "super private user prompt"
    resp = client.post(
        "/v1/chat/completions",
        headers=inference_headers,
        json={"model": "anthropic/claude-3-5-sonnet", "messages": [{"role": "user", "content": prompt}]},
    )
    assert resp.status_code == 200
    assert prompt not in str(STORE.generation_store.generations)


def test_sentry_scrubs_sensitive_fields() -> None:
    event = {
        "request": {
            "headers": {"authorization": "Bearer sk-tr-v1-secret", "cookie": "session=secret"},
            "data": {"messages": [{"role": "user", "content": "prompt"}]},
        },
        "extra": {
            "OPENAI_API_KEY": "sk-secret",
            "DEEPSEEK_API_KEY": "sk-deepseek-secret",
            "KIMI_API_KEY": "kimi-secret",
            "MISTRAL_API_KEY": "mistral-secret",
            "MOONSHOT_API_KEY": "moonshot-secret",
            "VERTEX_ACCESS_TOKEN": "ya29.vertex-secret",
            "output": "model answer",
            "safe": "ok",
        },
    }
    scrubbed = before_send(event, {})
    assert scrubbed is not None
    as_text = str(scrubbed)
    assert "sk-tr-v1-secret" not in as_text
    assert "sk-deepseek-secret" not in as_text
    assert "kimi-secret" not in as_text
    assert "mistral-secret" not in as_text
    assert "moonshot-secret" not in as_text
    assert "ya29.vertex-secret" not in as_text
    assert "prompt" not in as_text
    assert "model answer" not in as_text
    assert "ok" in as_text


def test_no_sentry_in_enclave_code() -> None:
    root = Path(__file__).resolve().parents[2]
    enclave = root / "quill-cloud-proxy" / "enclave-go"
    if not enclave.exists():
        return
    for path in enclave.rglob("*"):
        if path.is_file() and (path.suffix == ".go" or path.name.startswith("Dockerfile")):
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            assert "sentry" not in text
            assert "58539b11263132bcb70ea30f0b92e0f4" not in text
