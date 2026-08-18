from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import DEFAULT_SIGNUP_CREDIT_MICRODOLLARS
from trusted_router.og import (
    OG_DESCRIPTION,
    OG_TITLE,
    og_image_svg,
    pricing_og_image_svg,
)
from trusted_router.secrets import LocalKeyFile
from trusted_router.sentry_config import before_send
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.typed_balance import live_credit_summary

TEST_BYOK_KMS_KEY_NAME = (
    "projects/test/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"
)


def test_signup_credit_defaults_stay_aligned() -> None:
    assert DEFAULT_SIGNUP_CREDIT_MICRODOLLARS == 300_000
    assert Settings(environment="test").signup_trial_credit_microdollars == 300_000
TEST_SES_SETTINGS = {
    "aws_access_key_id": "test-access-key",
    "aws_secret_access_key": "test-secret-key",
    "ses_from_email": "noreply@example.com",
}


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
    # Marketing page hero + key surfaces stay real. Redesigned 2026-06: a
    # static routing-diagram hero + reliability stat cards replace the
    # animated orbital scene and the world map.
    assert "Get API key" in dashboard.text
    assert "End-to-End Encrypted AI gateway" in dashboard.text
    assert "ATTESTED GATEWAY" in dashboard.text  # routing-diagram hero
    assert "Live regions" in dashboard.text  # reliability stats
    assert "7 live regions across 3 clouds and 4 continents" in dashboard.text
    assert "North America, South America, Europe, and Australia" in dashboard.text
    assert "trustedrouter/auto" in dashboard.text  # routing model
    assert "$25 USDC" not in dashboard.text
    assert "Stripe Crypto" not in dashboard.text
    assert "Quill Feather" not in dashboard.text
    assert "https://quill.lorehex.co" not in dashboard.text
    assert 'href="/status"' in dashboard.text
    assert "https://github.com/Lore-Hex/trusted-router-py" in dashboard.text
    assert 'href="/providers"' in dashboard.text
    # The homepage features the open-weight leaders (the main use) by name.
    assert "DeepSeek" in dashboard.text
    assert "Qwen" in dashboard.text
    assert "GLM" in dashboard.text
    assert "Gemma" in dashboard.text
    assert "Google" in dashboard.text
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
    # Static assets are release-cache-busted so a redeployed page does not
    # render with a day-old browser-cached CSS file.
    assert '<script src="/static/dashboard.js?v=' in dashboard.text
    assert 'href="/static/dashboard.css?v=' in dashboard.text

    providers_page = client.get("/providers", headers={"accept": "text/html"})
    assert providers_page.status_code == 200
    assert "Provider transparency" in providers_page.text
    assert "Provider compute" in providers_page.text
    assert "Phala" in providers_page.text
    assert "Tinfoil" in providers_page.text
    assert "No provider claim" in providers_page.text
    assert "Unknown stays unknown" in providers_page.text

    providers_json = client.get("/providers", headers={"accept": "application/json"})
    assert providers_json.status_code == 200
    assert providers_json.headers["content-type"].startswith("application/json")
    provider_rows = providers_json.json()["data"]
    assert [item["id"] for item in provider_rows[:2]] == ["tinfoil", "trustedrouter"]
    tinfoil = next(item for item in provider_rows if item["id"] == "tinfoil")
    assert tinfoil["provider_e2ee"] is True
    openai = next(item for item in provider_rows if item["id"] == "openai")
    assert openai["provider_zero_data_retention"] is False
    assert openai["provider_confidential_compute"] is None
    google_ai_studio = next(
        item for item in provider_rows if item["id"] == "google-ai-studio"
    )
    google_vertex = next(item for item in provider_rows if item["id"] == "google-vertex")
    assert google_ai_studio["provider_zero_data_retention"] is False
    assert google_ai_studio["supports_byok"] is True
    assert google_vertex["provider_zero_data_retention"] is False
    assert google_vertex["supports_byok"] is False
    anthropic = next(item for item in provider_rows if item["id"] == "anthropic")
    assert anthropic["provider_zero_data_retention"] is False
    together = next(item for item in provider_rows if item["id"] == "together")
    assert together["provider_zero_data_retention"] is True
    nebius = next(item for item in provider_rows if item["id"] == "nebius")
    assert nebius["provider_zero_data_retention"] is True
    deepseek = next(item for item in provider_rows if item["id"] == "deepseek")
    assert deepseek["provider_zero_data_retention"] is False

    js = client.get("/static/dashboard.js")
    assert js.status_code == 200
    assert "moneyFromMicrodollars" in js.text
    # Marketing-side JS now drives the wallet flow but no longer talks to /v1/signup.
    assert "/v1/auth/wallet/challenge" in js.text
    assert "/v1/auth/wallet/verify" in js.text
    assert "eth_requestAccounts" in js.text
    assert 'trackFunnelEvent("landing_engaged")' in js.text
    assert 'document.visibilityState !== "visible"' in js.text
    assert 'fetch("/analytics/events"' in js.text
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
    assert data["trial_credit_microdollars"] == 300_000
    assert live_credit_summary(data["workspace_id"])["total_credits"] == 300_000

    headers = {"authorization": f"Bearer {data['key']}"}
    workspaces = client.get("/v1/workspaces", headers=headers)
    assert workspaces.status_code == 200
    assert workspaces.json()["data"][0]["id"] == data["workspace_id"]

    duplicate = client.post("/v1/signup", json={"email": "alpha@example.com"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["type"] == "already_registered"
    assert live_credit_summary(data["workspace_id"])["total_credits"] == 300_000


def test_signup_validates_email(client: TestClient) -> None:
    resp = client.post("/v1/signup", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "bad_request"


def test_email_signup_closed_when_flag_off(test_settings: Settings) -> None:
    # Default posture in prod: plain-email signup is closed to stop
    # credit-farming via disposable addresses. Google/GitHub/wallet unaffected.
    closed = test_settings.model_copy(update={"email_signup_enabled": False})
    closed_client = TestClient(create_app(closed, init_observability=False))
    resp = closed_client.post("/v1/signup", json={"email": "farm@gonebox.email"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["type"] == "forbidden"


def test_signup_credit_can_be_disabled_explicitly() -> None:
    app = create_app(
        Settings(
            environment="test",
            signup_trial_credit_microdollars=0,
            email_signup_enabled=True,
        ),
        init_observability=False,
    )
    with TestClient(app) as zero_credit_client:
        created = zero_credit_client.post(
            "/v1/signup",
            json={"email": "no-starter@example.com"},
        )

    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["trial_credit_microdollars"] == 0
    assert live_credit_summary(data["workspace_id"])["total_credits"] == 0


def test_negative_signup_credit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot be negative"):
        Settings(signup_trial_credit_microdollars=-1)


def test_secondary_workspace_does_not_repeat_signup_credit(
    client: TestClient,
) -> None:
    signup = client.post(
        "/v1/signup",
        json={"email": "secondary-workspace@example.com"},
    )
    assert signup.status_code == 201, signup.text
    signup_data = signup.json()["data"]
    headers = {"x-trustedrouter-user": signup_data["email"]}

    created = client.post(
        "/v1/workspaces",
        headers=headers,
        json={"name": "Second workspace"},
    )

    assert created.status_code == 201, created.text
    second_id = created.json()["data"]["id"]
    assert live_credit_summary(signup_data["workspace_id"])["total_credits"] == 300_000
    assert live_credit_summary(second_id)["total_credits"] == 0


def test_production_dashboard_does_not_default_to_dev_user_header() -> None:
    from trusted_router.dashboard import dashboard_html

    html = dashboard_html(
        Settings(
            **TEST_SES_SETTINGS,
            environment="production",
            internal_gateway_token="internal-prod-token",  # noqa: S106
            stripe_webhook_secret="whsec_test",  # noqa: S106
            stripe_secret_key="sk_test",  # noqa: S106
            sentry_dsn="https://example@example.ingest.sentry.io/1",
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
            byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
        )
    )

    assert '"environment": "production"' in html
    assert '"defaultDevUser": ""' in html
    assert "alpha@trustedrouter.local" not in html


def test_dashboard_emits_open_graph_and_twitter_card(client: TestClient) -> None:
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert '<link rel="icon" href="/favicon.ico" sizes="any">' in dashboard.text
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in dashboard.text
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in dashboard.text
    assert 'property="og:type" content="website"' in dashboard.text
    assert f'property="og:title" content="{OG_TITLE}"' in dashboard.text
    assert f'property="og:description" content="{OG_DESCRIPTION}"' in dashboard.text
    assert 'property="og:url" content="https://trustedrouter.com/"' in dashboard.text
    assert 'property="og:image" content="https://trustedrouter.com/og.png"' in dashboard.text
    assert 'property="og:image:type" content="image/png"' in dashboard.text
    assert 'property="og:image:width" content="1200"' in dashboard.text
    assert 'property="og:image:height" content="630"' in dashboard.text
    assert 'name="twitter:card" content="summary_large_image"' in dashboard.text
    assert f'name="twitter:title" content="{OG_TITLE}"' in dashboard.text
    assert f'name="twitter:description" content="{OG_DESCRIPTION}"' in dashboard.text
    assert 'name="twitter:image" content="https://trustedrouter.com/og.png"' in dashboard.text
    assert '<meta name="description"' in dashboard.text
    assert "<title>TrustedRouter" in dashboard.text


def test_og_svg_copy_matches_current_positioning() -> None:
    svg = og_image_svg(Settings())

    assert "End-to-End Encrypted Router" in svg
    assert "Hundreds of models. One verifiable prompt path." in svg
    assert "base_url=https://api.quillrouter.com/v1" in svg
    assert "api.trustedrouter.com" not in svg


def test_pricing_og_svg_matches_five_point_five_percent_policy() -> None:
    svg = pricing_og_image_svg(Settings())

    assert "5.5% markup" in svg
    assert "on prepaid model cost" in svg
    assert "OpenRouter credit fee" in svg
    assert "5.5%" in svg
    assert "10%" not in svg


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
    assert "https://github.com/Lore-Hex/trustedrouter-provider-check" in trust.text


def test_favicon_assets_are_served(client: TestClient) -> None:
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/x-icon"
    assert favicon.headers["cache-control"] == "max-age=86400, public"
    assert favicon.content.startswith(b"\x00\x00\x01\x00")

    favicon_head = client.head("/favicon.ico")
    assert favicon_head.status_code == 200
    assert favicon_head.headers["content-type"] == "image/x-icon"
    assert favicon_head.headers["cache-control"] == "max-age=86400, public"

    svg = client.get("/static/favicon.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in svg.content

    apple = client.get("/static/apple-touch-icon.png")
    assert apple.status_code == 200
    assert apple.headers["content-type"] == "image/png"
    assert apple.content.startswith(b"\x89PNG\r\n\x1a\n")

    release = client.get("/trust/gcp-release.json")
    assert release.status_code == 200
    assert release.json()["platform"] == "gcp-confidential-space"
    assert release.json()["source_repositories"]["control_plane"] == (
        "https://github.com/Lore-Hex/quill-router"
    )
    assert release.json()["source_repositories"]["attested_gateway"] == (
        "https://github.com/Lore-Hex/quill-cloud-proxy"
    )
    assert release.json()["source_repositories"]["provider_check"] == (
        "https://github.com/Lore-Hex/trustedrouter-provider-check"
    )


def test_static_fonts_force_woff2_media_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Some minimal Linux images do not register WOFF2 in /etc/mime.types.
    # Simulate that production environment so the application owns the
    # browser-facing contract instead of relying on the host image.
    monkeypatch.setattr(
        "starlette.responses.guess_type", lambda _path: ("text/plain", None)
    )

    font = client.get("/static/fonts/archivo-latin.woff2")

    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"


def test_read_only_blocks_writes_but_lets_reads_through() -> None:
    """Operational read-only flag (Stage 1 Spanner cutover prerequisite):
    POST/PUT/PATCH/DELETE return 503 with `Retry-After`; GET/HEAD/OPTIONS
    plus health checks pass through unchanged."""
    locked_app = create_app(Settings(environment="test", read_only=True))
    locked_client = TestClient(locked_app)

    # Reads pass through.
    models = locked_client.get("/v1/models")
    assert models.status_code == 200

    # Health checks bypass read-only too — the LB and watchdog need to keep
    # seeing the service as up during the cutover so the region doesn't get
    # ripped out of rotation while we're just doing maintenance.
    assert locked_client.get("/health").status_code == 200

    # Writes are blocked with a retry hint.
    blocked = locked_client.post("/v1/signup", json={})
    assert blocked.status_code == 503
    assert blocked.json()["error"]["type"] == "service_unavailable"
    assert blocked.headers["retry-after"] == "1800"

    # CORS preflight (OPTIONS) is always allowed so browsers don't fail
    # their preflight before they even try the real request.
    preflight = locked_client.options(
        "/v1/signup",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code != 503


def test_read_only_default_off_lets_writes_through() -> None:
    """Production default is read_only=False; writes proceed normally."""
    app = create_app(Settings(environment="test"))
    client = TestClient(app)
    # Validation error from empty body, NOT a 503 — proves the middleware
    # didn't intercept the write.
    resp = client.post("/v1/signup", json={})
    assert resp.status_code != 503


def test_read_only_bypasses_rate_limit_writes() -> None:
    """Read-only mode must short-circuit rate-limiting too.

    `STORE.hit_rate_limit` does a windowed-counter Spanner write on
    every allowed request. During a Stage-1 cutover (Phase B-D
    window) we need ALL writes silent so the source snapshot we
    exported and imported into nam6 doesn't drift before Phase D
    flips the env var. The 2026-05-10 cutover surfaced this: ~9
    rate_limit rows landed on source after Phase B set TR_READ_ONLY
    because the rate-limit middleware writes regardless of method.

    With read_only=True, even an aggressive rate limit (1 per window)
    must NOT 429 — every request is just allowed through. Limits
    resume the moment Phase E drops the flag.
    """
    locked_app = create_app(
        Settings(
            environment="test",
            read_only=True,
            rate_limit_ip_per_window=1,
            rate_limit_window_seconds=60,
        )
    )
    locked_client = TestClient(locked_app)
    # Two GETs in the same window. Without the bypass, the second would
    # be 429 (since limit=1). With the bypass, both pass — the
    # underlying Spanner write was skipped on each.
    first = locked_client.get("/v1/models")
    second = locked_client.get("/v1/models")
    assert first.status_code == 200
    assert second.status_code == 200, (
        f"second GET should not be 429 in read-only mode (got "
        f"{second.status_code}); rate-limit middleware leaked a write"
    )


def test_rate_limit_returns_stable_openrouter_style_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_rate_limits

    # Keep both requests in the same bucket. Without a fixed clock this test
    # flakes when the first request lands just before a wall-clock minute and
    # the second just after it, which correctly starts a fresh rate-limit
    # window and returns 200.
    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: dt.datetime(2026, 7, 14, 20, 48, 30, tzinfo=dt.UTC),
    )
    STORE.rate_limit_store.reset()
    limited_app = create_app(
        Settings(
            environment="test",
            read_only=False,
            rate_limit_enabled=True,
            rate_limit_ip_per_window=1,
            rate_limit_window_seconds=60,
        )
    )
    limited_client = TestClient(limited_app)
    headers = {"x-forwarded-for": "203.0.113.9"}
    assert limited_client.post("/v1/signup", headers=headers, json={}).status_code == 400
    second = limited_client.post("/v1/signup", headers=headers, json={})
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"
    assert second.headers["retry-after"]


def test_unauthenticated_public_reads_do_not_write_rate_limit_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crawler must not turn cacheable catalog reads into a Spanner hot row."""
    calls: list[dict[str, object]] = []
    original_hit_rate_limit = InMemoryStore.hit_rate_limit

    def track_write(self, *_args, **kwargs):
        calls.append(kwargs)
        return original_hit_rate_limit(self, *_args, **kwargs)

    app = create_app(Settings(environment="test", rate_limit_enabled=True))
    client = TestClient(app)
    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", track_write)
    headers = {"x-forwarded-for": "199.203.99.122"}

    for path in (
        "/",
        "/models",
        "/providers",
        "/compare/models",
        "/models/openai/gpt-5.2",
        "/docs",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
    assert calls == []


def test_unauthenticated_public_reads_remain_locally_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_rate_limits

    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: dt.datetime(2026, 7, 30, 0, 18, 30, tzinfo=dt.UTC),
    )
    app = create_app(
        Settings(
            environment="test",
            rate_limit_enabled=True,
            rate_limit_ip_per_window=1,
            rate_limit_window_seconds=60,
        )
    )
    client = TestClient(app)
    headers = {"x-forwarded-for": "199.203.99.122"}

    assert client.get("/models", headers=headers).status_code == 200
    second = client.get("/providers", headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"


def test_internal_rate_limit_never_touches_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fleet-internal billing traffic must not write a shared counter row."""
    internal_token = "internal-rate-limit-token"  # noqa: S105 - test fixture token.
    app = create_app(
        Settings(
            environment="test",
            internal_gateway_token=internal_token,
            rate_limit_enabled=True,
        )
    )
    client = TestClient(app)
    user = STORE.ensure_user("internal-rate-limit@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="internal rate limit",
        creator_user_id=user.id,
    )
    store_calls = 0

    def reject_store_rate_limit(self, *_args, **_kwargs):
        del self
        nonlocal store_calls
        store_calls += 1
        raise AssertionError("internal rate limiting touched the backing store")

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", reject_store_rate_limit)
    response = client.post(
        "/v1/internal/gateway/key",
        headers={"x-trustedrouter-internal-token": internal_token},
        json={"api_key_lookup_hash": key.lookup_hash},
    )

    assert response.status_code == 200, response.text
    assert store_calls == 0


def test_internal_rate_limit_is_enforced_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_rate_limits

    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: dt.datetime(2026, 8, 1, 18, 20, 30, tzinfo=dt.UTC),
    )
    internal_token = "internal-local-limit-token"  # noqa: S105 - test fixture token.
    settings = Settings(
        environment="test",
        internal_gateway_token=internal_token,
        rate_limit_enabled=True,
        rate_limit_internal_per_window=2,
        rate_limit_window_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)
    user = STORE.ensure_user("internal-local-limit@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="internal local limit",
        creator_user_id=user.id,
    )
    responses = [
        client.post(
            "/v1/internal/gateway/key",
            headers={"x-trustedrouter-internal-token": internal_token},
            json={"api_key_lookup_hash": key.lookup_hash},
        )
        for _ in range(settings.rate_limit_internal_per_window + 1)
    ]

    assert [response.status_code for response in responses[:-1]] == [200, 200]
    limited = responses[-1]
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "rate_limited"
    assert limited.headers["retry-after"]
    assert limited.headers["x-ratelimit-limit"] == "2"
    assert limited.headers["x-ratelimit-remaining"] == "0"
    assert limited.headers["x-ratelimit-reset"]


def test_authenticated_key_rate_limit_still_uses_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    original = InMemoryStore.hit_rate_limit

    def track_store_rate_limit(self, *_args, **kwargs):
        calls.append(kwargs)
        return original(self, *_args, **kwargs)

    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", track_store_rate_limit)
    app = create_app(Settings(environment="test", rate_limit_enabled=True))
    client = TestClient(app)

    response = client.get(
        "/v1/models",
        headers={"authorization": "Bearer rate-limit-routing-key"},
    )

    assert response.status_code == 200
    assert [call["namespace"] for call in calls] == ["key"]


def test_rate_limit_fails_open_on_store_error(monkeypatch) -> None:
    """A Spanner abort/deadlock in the rate-limit counter must NEVER 500 a
    request. Rate limiting is a best-effort guard, so a contended/unavailable
    store fails OPEN (allow). Regression for the 2026-06-08 production
    "Aborted: Deadlock with higher priority transaction" that surfaced as an
    unhandled 500 on bot scanner traffic hammering one IP's counter row."""
    def boom(self, *_args, **_kwargs):
        del self
        raise RuntimeError("Aborted: Deadlock with higher priority transaction.")

    app = create_app(
        Settings(
            environment="test",
            rate_limit_ip_per_window=1,
            rate_limit_window_seconds=60,
        )
    )
    client = TestClient(app)
    monkeypatch.setattr(InMemoryStore, "hit_rate_limit", boom)
    # Even an aggressive limit + a raising store: both requests pass through
    # (fail-open) — not 429, and crucially not 500.
    first = client.post("/v1/signup", json={})
    second = client.post("/v1/signup", json={})
    assert first.status_code == 400
    assert second.status_code == 400


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
            byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
        )
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn=sentry_dsn,
            storage_backend="spanner-bigtable",
            spanner_instance_id=None,
            spanner_database_id=None,
            bigtable_instance_id=None,
            byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
        )


def test_production_config_requires_ses_delivery_credentials() -> None:
    values = {
        "environment": "production",
        "internal_gateway_token": "tok" + "en",
        "stripe_webhook_secret": "whsec_" + "test",
        "stripe_secret_key": "sk_" + "test_secret",
        "sentry_dsn": "https://example@example.ingest.sentry.io/1",
        "storage_backend": "spanner-bigtable",
        "spanner_instance_id": "trusted-router",
        "spanner_database_id": "trusted-router",
        "bigtable_instance_id": "trusted-router",
        "byok_kms_key_name": TEST_BYOK_KMS_KEY_NAME,
    }

    with pytest.raises(ValidationError, match="TR_AWS_ACCESS_KEY_ID"):
        Settings(**values)
    with pytest.raises(ValidationError, match="TR_AWS_SECRET_ACCESS_KEY"):
        Settings(**{**values, "aws_access_key_id": "access-key"})
    with pytest.raises(ValidationError, match="TR_SES_FROM_EMAIL"):
        Settings(
            **{
                **values,
                "aws_access_key_id": "access-key",
                "aws_secret_access_key": "secret-key",
                "ses_from_email": None,
            }
        )


def test_production_spanner_clickhouse_config_is_explicit_and_bigtable_free() -> None:
    values = {
        "environment": "production",
        "internal_gateway_token": "tok" + "en",
        "stripe_webhook_secret": "whsec_" + "test",
        "stripe_secret_key": "sk_" + "test_secret",
        "sentry_dsn": "https://example@example.ingest.sentry.io/1",
        "storage_backend": "spanner-clickhouse",
        "spanner_instance_id": "trusted-router",
        "spanner_database_id": "trusted-router",
        "byok_kms_key_name": TEST_BYOK_KMS_KEY_NAME,
        "analytics_read_mode": "clickhouse-only",
        "generation_records_enabled": True,
        "operational_analytics_outbox_enabled": True,
        "analytics_outbox_enabled": True,
        "bigtable_mirror_writes_enabled": False,
        "request_record_write_mode": "typed",
        "settle_outbox_enabled": True,
        "operational_analytics_clickhouse_url": "http://10.0.0.1:8123",
        "operational_analytics_clickhouse_password": "pass" + "word",
        **TEST_SES_SETTINGS,
    }

    settings = Settings(**values)
    assert settings.storage_backend == "spanner-clickhouse"
    assert settings.bigtable_mirror_writes_enabled is False

    with pytest.raises(ValidationError, match="BIGTABLE_MIRROR"):
        Settings(**{**values, "bigtable_mirror_writes_enabled": True})
    with pytest.raises(ValidationError, match="clickhouse-only"):
        Settings(**{**values, "analytics_read_mode": "clickhouse"})


def test_production_control_plane_does_not_register_inference_routes() -> None:
    internal_token = "tok" + "en"
    webhook_secret = "whsec_" + "test"
    stripe_key = "sk_" + "test_secret"
    sentry_dsn = "https://example@example.ingest.sentry.io/1"
    prod_app = create_app(
        Settings(
            **TEST_SES_SETTINGS,
            environment="production",
            internal_gateway_token=internal_token,
            stripe_webhook_secret=webhook_secret,
            stripe_secret_key=stripe_key,
            sentry_dsn=sentry_dsn,
            storage_backend="spanner-bigtable",
            spanner_instance_id="trusted-router",
            spanner_database_id="trusted-router",
            bigtable_instance_id="trusted-router-logs",
            byok_kms_key_name=TEST_BYOK_KMS_KEY_NAME,
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
        json={"model": "anthropic/claude-sonnet-4.6", "messages": [{"role": "user", "content": prompt}]},
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


def test_internal_rate_limit_guessed_tokens_share_the_ip_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attacker varying an INVALID internal token must not mint a fresh
    bucket per guess (cardinality bound) nor escape the per-subject limit
    (review finding on #400): unverified credentials collapse to the caller's
    IP, the same bounded identity the anonymous namespace uses."""
    import datetime as _dt

    from trusted_router import storage_rate_limits

    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: _dt.datetime(2026, 8, 1, 18, 21, 30, tzinfo=_dt.UTC),
    )
    internal_token = "internal-real-token"  # noqa: S105 - test fixture token.
    settings = Settings(
        environment="test",
        internal_gateway_token=internal_token,
        rate_limit_enabled=True,
        rate_limit_internal_per_window=3,
        rate_limit_window_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)

    responses = [
        client.post(
            "/v1/internal/gateway/key",
            headers={"x-trustedrouter-internal-token": f"guess-{n}"},
            json={"api_key_lookup_hash": "irrelevant"},
        )
        for n in range(settings.rate_limit_internal_per_window + 1)
    ]

    # Unique wrong tokens still consume ONE shared (per-IP) bucket: the
    # requests inside the limit are 401 (bad token), and the one past the
    # limit is 429 -- the guesses could not escape the limiter by varying.
    assert [r.status_code for r in responses[:-1]] == [401, 401, 401]
    assert responses[-1].status_code == 429

    # And the valid fleet token is NOT throttled by the attacker's bucket:
    # it authenticates under its own subject (the token fingerprint).
    user = STORE.ensure_user("internal-bucket-isolation@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="bucket isolation",
        creator_user_id=user.id,
    )
    fleet = client.post(
        "/v1/internal/gateway/key",
        headers={"x-trustedrouter-internal-token": internal_token},
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert fleet.status_code == 200, fleet.text


def test_internal_rate_limit_precedence_matches_route_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Middleware credential precedence must mirror require_internal_gateway
    (bearer first, then header). A valid BEARER internal token plus a stale
    x-trustedrouter-internal-token header authenticates at the route, so it
    must land in the fleet bucket -- not the per-IP bucket an attacker can
    exhaust (review finding on #400, round 2)."""
    import datetime as _dt

    from trusted_router import storage_rate_limits

    monkeypatch.setattr(
        storage_rate_limits,
        "utcnow",
        lambda: _dt.datetime(2026, 8, 1, 18, 22, 30, tzinfo=_dt.UTC),
    )
    internal_token = "internal-precedence-token"  # noqa: S105 - test fixture token.
    settings = Settings(
        environment="test",
        internal_gateway_token=internal_token,
        rate_limit_enabled=True,
        rate_limit_internal_per_window=2,
        rate_limit_window_seconds=60,
    )
    app = create_app(settings)
    client = TestClient(app)
    user = STORE.ensure_user("internal-precedence@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="precedence",
        creator_user_id=user.id,
    )

    # Exhaust the per-IP bucket with wrong-token guesses (401 inside the
    # limit, then 429).
    guesses = [
        client.post(
            "/v1/internal/gateway/key",
            headers={"x-trustedrouter-internal-token": f"stale-{n}"},
            json={"api_key_lookup_hash": key.lookup_hash},
        )
        for n in range(settings.rate_limit_internal_per_window + 1)
    ]
    assert [r.status_code for r in guesses] == [401, 401, 429]

    # Valid bearer + stale header: route auth accepts the bearer, so the
    # middleware must too -- fleet bucket, NOT the exhausted IP bucket.
    mixed = client.post(
        "/v1/internal/gateway/key",
        headers={
            "Authorization": f"Bearer {internal_token}",
            "x-trustedrouter-internal-token": "stale-header",
        },
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert mixed.status_code == 200, mixed.text


def test_in_memory_rate_limit_window_is_tumbling_not_sliding() -> None:
    """The window is a TUMBLING bucket keyed on `epoch // window_seconds`.

    This is the documented behaviour, not a defect -- but it means a burst that
    crosses a wall-clock boundary gets a fresh allowance, and it is why tests
    that assert "the Nth request is refused" must pin the clock. Two of them did
    not, and one failed on CI (2026-08-17) with 202 instead of 429 for exactly
    this reason.

    If someone later changes this to a sliding window, this test should fail and
    the clock pins in those tests can then be removed.
    """
    import datetime as _dt
    import threading as _threading

    from trusted_router.storage_rate_limits import InMemoryRateLimits

    limits = InMemoryRateLimits(lock=_threading.RLock())

    def nth_request_allowed(start: _dt.datetime) -> bool:
        limits.reset()
        hit = None
        for index in range(61):
            hit = limits.hit(
                namespace="ce",
                subject="k",
                limit=60,
                window_seconds=60,
                now=start + _dt.timedelta(milliseconds=20 * index),
            )
        assert hit is not None
        return hit.allowed

    # Entirely inside one bucket: the 61st request is over the limit of 60.
    assert nth_request_allowed(_dt.datetime(2026, 1, 1, 0, 0, 10, tzinfo=_dt.UTC)) is False
    # Straddling :00 starts a new bucket, so the count restarts and the same
    # 61st request is allowed.
    assert nth_request_allowed(_dt.datetime(2026, 1, 1, 0, 0, 59, tzinfo=_dt.UTC)) is True


def test_in_memory_rate_limit_bucket_cardinality_is_capped() -> None:
    """Attacker-fabricated identities (rotated tokens, spoofed XFF) must not
    grow the process map without bound (review finding on #400, round 3).
    At the cap, new subjects fold into a shared per-namespace overflow bucket:
    memory stays bounded and fabricated identities throttle collectively."""
    import datetime as _dt
    import threading as _threading

    from trusted_router.storage_rate_limits import InMemoryRateLimits

    limits = InMemoryRateLimits(lock=_threading.RLock(), max_buckets=50)
    # The bucket key includes `epoch // window_seconds`, so a run that crosses a
    # real minute boundary starts a SECOND generation of keys and can exceed the
    # cap+1 bound below for a reason unrelated to cardinality. Pinning `now`
    # keeps this a test of the cap.
    fixed_now = _dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=_dt.UTC)
    for n in range(200):
        hit = limits.hit(
            namespace="internal",
            subject=f"fabricated-{n}",
            limit=3,
            window_seconds=60,
            now=fixed_now,
        )
    assert len(limits.buckets) <= 51  # cap + at most the one overflow bucket
    # Identities past the cap share the overflow bucket, so a rotation attack
    # is throttled collectively instead of resetting per identity.
    assert hit.allowed is False
    # Distinct subjects below the cap keep their own buckets untouched.
    early = limits.hit(
        namespace="internal", subject="fabricated-1", limit=3, window_seconds=60
    )
    assert early.allowed is True
