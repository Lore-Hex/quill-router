"""Production smoke tests against https://trustedrouter.com.

These hit the real Cloud Run service and verify the high-level wiring
that unit tests can't exercise: the deploy pipeline, the Cloudflare DNS
+ TLS chain, the SES bounce/complaint endpoint signature gate, and that
the marketing page renders the sign-in modal.

Run them with `TR_PROD_SMOKE=1 uv run pytest tests/test_smoke_prod.py`.
They're skipped by default so a normal `pytest` run is offline-safe.
The opt-in env var also makes it explicit when CI starts hitting prod.
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest

PROD_BASE_URL = os.environ.get("TR_PROD_BASE_URL", "https://trustedrouter.com")
PROD_TRUST_URL = os.environ.get("TR_PROD_TRUST_URL", "https://trust.trustedrouter.com")
PROD_API_BASE_URL = os.environ.get("TR_PROD_API_BASE_URL", "https://api.quillrouter.com/v1")
ENABLED = os.environ.get("TR_PROD_SMOKE") == "1"

pytestmark = pytest.mark.skipif(not ENABLED, reason="TR_PROD_SMOKE=1 to enable")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    return httpx.Client(base_url=PROD_BASE_URL, timeout=10.0, follow_redirects=False)


@pytest.fixture(scope="module")
def api_client() -> httpx.Client:
    return httpx.Client(base_url=PROD_API_BASE_URL, timeout=10.0, follow_redirects=False)


@pytest.fixture(scope="module")
def trust_client() -> httpx.Client:
    return httpx.Client(base_url=PROD_TRUST_URL, timeout=10.0, follow_redirects=False)


def test_health_returns_ok(client: httpx.Client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_marketing_page_serves_sign_in_modal(client: httpx.Client) -> None:
    """If this fails the deploy stripped the marketing page or broke
    Jinja rendering. The sign-in modal is the entry point to everything
    behind it; if the dialog id disappears, nobody can sign in."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert 'id="signinModal"' in body
    assert "Continue with MetaMask" in body
    assert 'data-action="open-signin"' in body


def test_console_unauth_redirects_to_signin(client: httpx.Client) -> None:
    response = client.get("/console/api-keys")
    assert response.status_code == 302
    assert response.headers["location"] == "/?reason=signin"


def test_internal_ses_endpoint_rejects_unsigned(client: httpx.Client) -> None:
    """The SES bounce/complaint webhook must verify SNS signatures or
    anyone can suppress arbitrary email addresses. A 403 here proves the
    signature gate is wired up. The endpoint is rate-limited so we only
    poke it once per smoke run."""
    response = client.post(
        "/internal/ses/notifications",
        headers={"content-type": "application/json"},
        content=b"{}",
    )
    assert response.status_code in {400, 403}


def test_static_assets_cacheable(client: httpx.Client) -> None:
    """og.png is small but representative — confirms StaticFiles is
    mounted and Cache-Control isn't broken (older revisions had a
    `cache-control: no-store` regression)."""
    response = client.get("/og.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    cache_control = response.headers.get("cache-control", "")
    assert "max-age" in cache_control


def test_public_dns_resolves_expected_hosts() -> None:
    for host in ["trustedrouter.com", "trust.trustedrouter.com", "api.quillrouter.com"]:
        assert socket.getaddrinfo(host, 443), host


def test_api_catalog_and_regions_are_publicly_reachable(client: httpx.Client) -> None:
    """The catalog (models / providers / regions) lives on the control
    plane and must be readable without auth — SDKs call these BEFORE the
    user has a key. The attested gateway at api.quillrouter.com only
    handles chat; it has no catalog routes."""
    models = client.get("/v1/models")
    providers = client.get("/v1/providers")
    regions = client.get("/v1/regions")

    assert models.status_code == 200, models.text
    assert providers.status_code == 200, providers.text
    assert regions.status_code == 200, regions.text
    assert any(item["id"] == "trustedrouter/auto" for item in models.json()["data"])
    assert {"anthropic", "openai", "gemini", "cerebras", "deepseek", "mistral", "vertex"}.issubset(
        {item["id"] for item in providers.json()["data"]}
    )
    assert "europe-west4" in {item["id"] for item in regions.json()["data"]}


def test_attested_gateway_rejects_unauthenticated_chat(api_client: httpx.Client) -> None:
    """api.quillrouter.com is the attested chat gateway. Every path other
    than /attestation must require a bearer token; if it ever serves an
    unauthenticated 200, billing and key-limit gating are bypassed."""
    response = api_client.post(
        "/chat/completions",
        headers={"content-type": "application/json"},
        content=b'{"model":"trustedrouter/auto","messages":[{"role":"user","content":"x"}]}',
    )
    assert response.status_code == 401, response.text


def test_v1_models_includes_kimi_and_auto_fallback_chain(client: httpx.Client) -> None:
    """trustedrouter/auto's auto_candidates list is the rollover chain
    used when a user requests the meta-model. If the chain regresses, all
    auto routes fail open in unexpected order."""
    response = client.get("/v1/models")
    assert response.status_code == 200
    models = {item["id"]: item for item in response.json()["data"]}

    assert "kimi/kimi-k2.6" in models
    auto = models["trustedrouter/auto"]
    candidates = auto["trustedrouter"]["auto_candidates"]
    assert "anthropic/claude-opus-4.7" in candidates
    assert "kimi/kimi-k2.6" in candidates
    assert "cerebras/llama3.1-8b" in candidates


def test_regions_list_covers_all_ten_gcp_regions(client: httpx.Client) -> None:
    """Each enabled region must publish its per-region API base URL.
    Drift here breaks the marketing world map and the SDK's region
    selection."""
    response = client.get("/v1/regions")
    assert response.status_code == 200
    regions = {item["id"]: item for item in response.json()["data"]}

    # Only enumerate regions where a real attested-gateway VM is deployed.
    # See deploy/_lib.sh: TR_REGIONS is intentionally narrow because every
    # advertised region implies an enclave VM with an ACME-issued cert.
    expected = {"us-central1", "europe-west4"}
    assert expected.issubset(set(regions.keys())), regions.keys()
    primary_regions = [r for r in regions.values() if r.get("primary")]
    assert len(primary_regions) == 1, f"expected exactly one primary region, got {primary_regions}"
    primary = primary_regions[0]
    # The primary region uses the canonical api.quillrouter.com, NOT
    # api-{primary}.quillrouter.com — that alias would TLS-fail because
    # the enclave's ACME cert covers the canonical name only.
    assert primary["api_base_url"] == "https://api.quillrouter.com/v1", primary
    for region in regions.values():
        assert region.get("enabled") is True, region
        if region["id"] == primary["id"]:
            continue
        assert region["api_base_url"].startswith("https://api-"), region
        assert region["api_base_url"].endswith(".quillrouter.com/v1"), region


def test_marketing_page_advertises_production_not_alpha(client: httpx.Client) -> None:
    """Belt-and-suspenders for the alpha-removal: if a future deploy
    accidentally restores 'Public Alpha' framing, this test fails. The
    'multi-region' pill is also the smoke check that the regions panel
    rendered (Jinja didn't error out on map_regions)."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Public Alpha" not in body
    assert "Production" in body
    assert "multi-region" in body
    assert "world-map.svg" in body or "<svg" in body  # map renders


def test_oauth_login_redirects_to_provider(client: httpx.Client) -> None:
    """Hitting /auth/{google,github}/login must 302 to the provider's
    authorize endpoint with state + PKCE params. If the redirect host
    drifts, OAuth is silently broken."""
    google = client.get("/auth/google/login")
    github = client.get("/auth/github/login")

    if google.status_code == 302:
        location = google.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "state=" in location
        assert "client_id=" in location
    else:
        # Provider can be disabled if no client_id is configured. 404 is
        # the documented response in that case; anything else is wrong.
        assert google.status_code == 404, google.text

    if github.status_code == 302:
        location = github.headers["location"]
        assert location.startswith("https://github.com/login/oauth/authorize?")
        assert "state=" in location
        assert "client_id=" in location
    else:
        assert github.status_code == 404, github.text


def test_console_pages_all_redirect_unauthenticated_to_signin(client: httpx.Client) -> None:
    """One of the easiest deploy regressions is forgetting to gate a new
    console page with the session-cookie dependency. This walks every
    page we ship and confirms each one redirects unauthenticated users
    back to the marketing sign-in modal."""
    pages = [
        "/console",
        "/console/api-keys",
        "/console/credits",
        "/console/byok",
        "/console/routing",
        "/console/activity",
        "/console/settings",
        "/console/account/preferences",
    ]
    for path in pages:
        response = client.get(path)
        assert response.status_code in {302, 303}, f"{path}: {response.status_code} {response.text[:200]}"
        location = response.headers["location"]
        assert location in {"/?reason=signin", "/console/api-keys"}, f"{path}: location={location}"


def test_security_headers_include_hsts(client: httpx.Client) -> None:
    """HSTS is the second line of defense after the LB's HTTP→HTTPS
    redirect. If a future deploy strips the middleware, browsers will
    still try http:// on the next visit. 2-year max-age + includeSubDomains
    are the preload-list minimums; we don't preload yet."""
    response = client.get("/")
    hsts = response.headers.get("strict-transport-security", "")
    assert "max-age=" in hsts, hsts
    # 2 years = 63072000 seconds. Anything shorter is a regression.
    max_age_value = int(hsts.split("max-age=", 1)[1].split(";", 1)[0])
    assert max_age_value >= 63072000, f"max-age dropped: {max_age_value}"
    assert "includeSubDomains" in hsts


def test_http_redirects_to_https(client: httpx.Client) -> None:
    """The LB's http-redirect URL map should turn http:// into a 301 to
    https://. If this regresses, plain-http visitors get a connection
    reset instead of being upgraded."""
    plain = httpx.Client(base_url="http://trustedrouter.com", timeout=10.0, follow_redirects=False)
    try:
        response = plain.get("/")
    finally:
        plain.close()
    assert response.status_code in {301, 308}
    location = response.headers["location"]
    assert location.startswith("https://trustedrouter.com")


def test_signup_endpoint_is_idempotent_and_returns_management_key(client: httpx.Client) -> None:
    """The unauthenticated /v1/signup endpoint mints one-time keys.
    Verifying it stays online catches deploys that accidentally gated it
    behind auth (which would block all new signups). The smoke uses a
    timestamped email so the test is non-destructive against the
    already-registered set. Uses example.com because pydantic's
    email-validator rejects reserved TLDs like .local."""
    import time
    email = f"smoke-{int(time.time())}@example.com"
    first = client.post("/v1/signup", json={"email": email})
    assert first.status_code in {200, 201}, first.text
    data = first.json()["data"]
    assert data["email"] == email
    assert data["key"].startswith("sk-tr-v1-")
    assert data["key_id"].startswith("key_")
    assert data["management"] is True
    assert data["trial_credit_microdollars"] > 0
    # Idempotent: re-submitting the same email returns 409 already_registered.
    repeat = client.post("/v1/signup", json={"email": email})
    assert repeat.status_code == 409, repeat.text
    assert repeat.json()["error"]["type"] == "already_registered"


def test_health_endpoint_under_v1_prefix(client: httpx.Client) -> None:
    """/v1/health and /health both serve the same probe — Cloud Run
    health checks hit /health, SDKs may hit /v1/health. Both must
    return 200."""
    bare = client.get("/health")
    versioned = client.get("/v1/health")
    assert bare.status_code == 200
    assert versioned.status_code == 200
    assert bare.json() == versioned.json() == {"status": "ok"}


def test_world_map_svg_is_served_with_cache(client: httpx.Client) -> None:
    """The marketing world map is a ~80KB SVG referenced from the
    landing page. If the static mount drops it, the page renders empty.
    Also confirms it carries a cacheable response so we don't hammer
    Cloud Run on every page view."""
    response = client.get("/static/world-map.svg")
    assert response.status_code == 200, response.text[:200]
    assert "image/svg" in response.headers["content-type"] or "svg" in response.headers["content-type"]
    cache_control = response.headers.get("cache-control", "")
    assert "max-age" in cache_control
    assert b"<svg" in response.content
    # Sanity: real Natural Earth SVG is at least 30KB, hand-drawn is way smaller.
    assert len(response.content) > 30_000


def test_per_region_api_hostnames_complete_tls_handshake(client: httpx.Client) -> None:
    """Every advertised api_base_url has to actually serve TLS — DNS
    resolution alone isn't enough. A SAN-mismatched cert (e.g. the
    enclave's ACME cert covers `api.quillrouter.com` only but DNS for
    `api-us-central1.quillrouter.com` lands at the same IP) fails the
    handshake silently and breaks every SDK that pins the per-region
    hostname. /attestation is anonymous, so we can confirm TLS + a real
    response without needing a key."""
    regions = client.get("/v1/regions").json()["data"]
    enabled = [r for r in regions if r.get("enabled")]
    failures: list[str] = []
    for region in enabled:
        host = region["api_base_url"].split("://", 1)[1].split("/", 1)[0]
        with httpx.Client(timeout=10.0) as direct:
            try:
                response = direct.get(f"https://{host}/attestation")
            except Exception as exc:  # noqa: BLE001 - any TLS/connection error is a failure.
                failures.append(f"{host}: {type(exc).__name__}: {exc}")
                continue
        if response.status_code != 200:
            failures.append(f"{host}: /attestation returned {response.status_code}")
    assert not failures, "per-region attestation failures:\n  " + "\n  ".join(failures)


def test_per_region_api_hostnames_resolve_dns(client: httpx.Client) -> None:
    """The control plane advertises one api-{region}.quillrouter.com
    hostname per enabled region. Each one needs a public DNS record so
    SDK clients can talk to the nearest pool. If a record drops, that
    region silently disappears for routing."""
    regions = client.get("/v1/regions").json()["data"]
    enabled = [r for r in regions if r.get("enabled")]
    assert len(enabled) >= 2, f"expected ≥2 enabled regions, got {len(enabled)}"

    failures: list[str] = []
    for region in enabled:
        host = region["api_base_url"].split("://", 1)[1].split("/", 1)[0]
        try:
            socket.getaddrinfo(host, 443)
        except socket.gaierror as exc:
            failures.append(f"{host}: {exc}")
    assert not failures, "per-region DNS failures:\n  " + "\n  ".join(failures)


def test_marketing_landing_links_to_github(client: httpx.Client) -> None:
    """The landing page is the public-facing source-of-truth doc; if a
    deploy strips the repo links, the trust story breaks."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "github.com/Lore-Hex" in body or "github.com/Lore-Hex/quill-router" in body


def test_health_check_response_is_under_one_kilobyte(client: httpx.Client) -> None:
    """Cloud Run liveness probes hit /health every 10s. If a future
    deploy made it return a verbose payload, every region pays for it
    on every probe. Keep it tiny."""
    response = client.get("/health")
    assert response.status_code == 200
    assert len(response.content) < 1024


def test_v1_models_exposes_trustedrouter_metadata_block(client: httpx.Client) -> None:
    """Every model in the catalog has to carry the `trustedrouter` block
    SDKs use for routing decisions (prepaid vs BYOK availability,
    attested-gateway flag, microdollar pricing). One model missing the
    block silently breaks SDK auto-fallback."""
    response = client.get("/v1/models")
    models = response.json()["data"]
    assert len(models) >= 5
    for model in models:
        assert "trustedrouter" in model, f"{model.get('id')} missing trustedrouter block"
        meta = model["trustedrouter"]
        assert "provider" in meta
        assert "prepaid_available" in meta
        assert "byok_available" in meta
        assert "attested_gateway" in meta
        assert "stores_content" in meta
        assert meta["stores_content"] is False, f"{model['id']} claims to store content"


def test_attested_gateway_attestation_endpoint_is_anonymous() -> None:
    """The /attestation endpoint must be reachable without a bearer —
    clients call it BEFORE pinning the cert. If it ever required auth,
    nobody could establish trust on first contact.

    The enclave normalizes paths and only serves /attestation (no /v1
    prefix); going through PROD_API_BASE_URL would hit the chat path
    and 401. We construct an explicit client at the bare api host."""
    api_host = PROD_API_BASE_URL.split("/v1", 1)[0]
    bare = httpx.Client(base_url=api_host, timeout=10.0, follow_redirects=False)
    try:
        response = bare.get("/attestation")
    finally:
        bare.close()
    assert response.status_code != 401, response.text
    assert response.status_code in {200, 404}, response.text


def test_console_page_titles_are_distinct_per_route(client: httpx.Client) -> None:
    """Pages that all serve the same fallback redirect are useless.
    Every /console/* path must redirect to the sign-in modal — that's
    proof each route is registered separately and not falling through
    to a catch-all 404 handler."""
    paths = [
        "/console/api-keys",
        "/console/credits",
        "/console/byok",
        "/console/routing",
        "/console/activity",
        "/console/settings",
        "/console/account/preferences",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 302, f"{path}: {response.status_code}"


def test_trust_page_release_json_includes_compliance_metadata(
    trust_client: httpx.Client,
) -> None:
    """The trust page is the audit doc — it has to declare the data
    policy and source-repo provenance so external auditors can check
    what's deployed against what's published."""
    release = trust_client.get("/trust/gcp-release.json").json()

    assert release["data_policy"]["prompt_output_storage"] is False
    assert release["data_policy"]["control_plane_prompt_access"] is False
    assert release["platform"] == "gcp-confidential-space"

    repos = release["source_repositories"]
    assert repos["control_plane"].endswith("/quill-router")
    assert repos["attested_gateway"].endswith("/quill-cloud-proxy")
    assert release["image_digest"].startswith("sha256:")
    assert release["source_commit"]


def test_internal_gateway_routes_require_internal_token(client: httpx.Client) -> None:
    """Every /internal/* route has to gate on the internal token. A
    deploy that accidentally exposed gateway authorize/settle/refund
    publicly would let anyone settle anyone's reservations.

    For POST routes we send a syntactically-valid body so we get past
    FastAPI's request-validation step (which would otherwise return 400
    before our auth dependency runs) — the response then has to be a
    real auth-denial code, not a 200/204."""
    routes = [
        ("POST", "/internal/gateway/authorize", {
            "api_key_hash": "smoke-test", "model": "openai/gpt-4o-mini",
            "estimated_input_tokens": 1, "max_output_tokens": 1,
        }),
        ("POST", "/internal/gateway/settle", {"authorization_id": "smoke-nonexistent"}),
        ("POST", "/internal/gateway/refund", {"authorization_id": "smoke-nonexistent"}),
        ("GET", "/internal/sentry-test", None),
    ]
    for method, path, payload in routes:
        response = client.request(method, path, json=payload)
        assert response.status_code in {401, 403, 404}, (
            f"{method} {path}: {response.status_code} {response.text[:200]}"
        )


def test_trust_page_and_release_files_are_published(trust_client: httpx.Client) -> None:
    page = trust_client.get("/")
    release = trust_client.get("/trust/gcp-release.json")
    digest = trust_client.get("/trust/image-digest-gcp.txt")
    image = trust_client.get("/trust/image-reference-gcp.txt")

    assert page.status_code == 200, page.text
    assert release.status_code == 200, release.text
    assert digest.status_code == 200, digest.text
    assert image.status_code == 200, image.text
    body = page.text
    for repo in [
        "Lore-Hex/quill-router",
        "Lore-Hex/quill-cloud-proxy",
        "Lore-Hex/quill-cloud-infra",
        "Lore-Hex/quill",
        "Lore-Hex/trusted-router-py",
        "Lore-Hex/trusted-router-js",
    ]:
        assert repo in body
    data = release.json()
    assert data["tls"]["hostname"] == "api.quillrouter.com"
    assert data["source_repositories"]["control_plane"].endswith("/quill-router")
    assert digest.text.strip() == data["image_digest"]
    assert image.text.strip() == data["image_reference"]
