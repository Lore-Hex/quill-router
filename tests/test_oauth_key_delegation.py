from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, InMemoryStore


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def _create_code(
    client: TestClient,
    headers: dict[str, str],
    *,
    verifier: str = "a" * 48,
    method: str = "S256",
    **overrides,
) -> tuple[str, dict]:
    body = {
        "callback_url": "https://app.example.com/callback",
        "code_challenge": _pkce_challenge(verifier) if method == "S256" else verifier,
        "code_challenge_method": method,
        "key_label": "Example app",
        "limit": "12.345678",
        "usage_limit_type": "monthly",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    body.update(overrides)
    response = client.post("/v1/auth/keys/code", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"], body


def test_oauth_code_exchange_creates_delegated_inference_key(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    verifier = "delegation-verifier-" + "a" * 43
    code, _ = _create_code(client, user_headers, verifier=verifier)

    exchange = client.post(
        "/v1/auth/keys",
        json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
    )

    assert exchange.status_code == 200, exchange.text
    payload = exchange.json()
    assert payload["key"].startswith("sk-tr-v1-")
    assert payload["user_id"] == STORE.find_user_by_email("alice@example.com").id
    api_key = STORE.get_key_by_raw(payload["key"])
    assert api_key is not None
    assert api_key.management is False
    assert api_key.name == "Example app"
    assert api_key.limit_microdollars == 12_345_678
    assert api_key.limit_reset == "monthly"
    assert api_key.expires_at == "2099-01-01T00:00:00Z"


def test_oauth_code_response_matches_openrouter_compat_shape(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers)

    assert code.startswith("auth_code-")
    created = next(iter(STORE.oauth_code_store.codes.values()))
    assert created.app_id
    assert created.created_at


def test_oauth_authorization_code_raw_secret_is_not_stored(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers)
    stored = next(iter(STORE.oauth_code_store.codes.values()))

    assert code not in json.dumps(stored.__dict__)
    assert stored.lookup_hash == hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert stored.secret_hash != stored.lookup_hash


def test_oauth_code_exchange_is_one_time(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    verifier = "replay-verifier-" + "b" * 43
    code, _ = _create_code(client, user_headers, verifier=verifier)

    first = client.post("/v1/auth/keys", json={"code": code, "code_verifier": verifier})
    second = client.post("/v1/auth/keys", json={"code": code, "code_verifier": verifier})

    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json()["error"]["message"] == "Invalid or expired authorization code"
    assert len(STORE.list_keys(first.json()["data"]["workspace_id"])) == 1


def test_oauth_code_exchange_rejects_wrong_pkce_verifier(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers, verifier="correct-" + "c" * 43)

    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": "wrong-" + "d" * 43})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Invalid code_verifier"
    assert not STORE.list_keys(STORE.find_user_by_email("alice@example.com").id)


def test_oauth_plain_pkce_exchange(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    verifier = "plain-verifier-" + "e" * 43
    code, _ = _create_code(client, user_headers, verifier=verifier, method="plain")

    exchange = client.post(
        "/v1/auth/keys",
        json={"code": code, "code_verifier": verifier, "code_challenge_method": "plain"},
    )

    assert exchange.status_code == 200, exchange.text
    assert STORE.get_key_by_raw(exchange.json()["key"]) is not None


def test_oauth_code_without_pkce_allows_exchange(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(
        client,
        user_headers,
        code_challenge=None,
        code_challenge_method=None,
    )

    exchange = client.post("/v1/auth/keys", json={"code": code})

    assert exchange.status_code == 200, exchange.text


def test_oauth_code_exchange_rejects_method_mismatch(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    verifier = "method-verifier-" + "f" * 43
    code, _ = _create_code(client, user_headers, verifier=verifier, method="plain")

    response = client.post(
        "/v1/auth/keys",
        json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "code_challenge_method does not match authorization code"


def test_oauth_code_exchange_requires_code(client: TestClient) -> None:
    response = client.post("/v1/auth/keys", json={})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "code is required"


def test_oauth_code_exchange_rejects_unknown_code(client: TestClient) -> None:
    response = client.post("/v1/auth/keys", json={"code": "auth_code-missing"})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Invalid or expired authorization code"


def test_oauth_code_exchange_rejects_expired_code(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers, code_challenge=None, code_challenge_method=None)
    stored = next(iter(STORE.oauth_code_store.codes.values()))
    stored.code_expires_at = "2000-01-01T00:00:00Z"

    response = client.post("/v1/auth/keys", json={"code": code})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Invalid or expired authorization code"


def test_oauth_code_exchange_rejects_non_ascii_verifier(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers, verifier="ascii-" + "g" * 43)

    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": "not-ascii-é"})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "code_verifier must be ASCII"


def test_oauth_code_creation_requires_management_auth(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/keys/code",
        json={"callback_url": "https://app.example.com/callback"},
    )

    assert response.status_code == 401


def test_oauth_code_creation_rejects_inference_key(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/auth/keys/code",
        headers=inference_headers,
        json={"callback_url": "https://app.example.com/callback"},
    )

    assert response.status_code == 403


def test_oauth_code_creation_defaults_label_from_callback_host(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(
        client,
        user_headers,
        callback_url="https://my-coding-app.example/cb",
        key_label=None,
        code_challenge=None,
        code_challenge_method=None,
    )

    exchange = client.post("/v1/auth/keys", json={"code": code})

    assert exchange.status_code == 200
    assert STORE.get_key_by_raw(exchange.json()["key"]).name == "my-coding-app.example delegated key"


def test_oauth_code_creation_stores_spawn_telemetry_without_returning_it(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/v1/auth/keys/code",
        headers=user_headers,
        json={
            "callback_url": "https://app.example.com/callback",
            "spawn_agent": "coding-assistant",
            "spawn_cloud": "aws-us-east-1",
        },
    )

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"id", "app_id", "created_at"}
    stored = next(iter(STORE.oauth_code_store.codes.values()))
    assert stored.spawn_agent == "coding-assistant"
    assert stored.spawn_cloud == "aws-us-east-1"


def test_oauth_code_creation_defaults_challenge_method_to_s256(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    verifier = "default-method-" + "h" * 43
    code, _ = _create_code(
        client,
        user_headers,
        verifier=verifier,
        code_challenge=_pkce_challenge(verifier),
        code_challenge_method=None,
    )

    assert next(iter(STORE.oauth_code_store.codes.values())).code_challenge_method == "S256"
    assert client.post("/v1/auth/keys", json={"code": code, "code_verifier": verifier}).status_code == 200


def test_oauth_code_creation_with_management_key_keeps_creator_user(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "mgmt", "management": True}).json()
    management_headers = {"authorization": f"Bearer {created['key']}"}

    code, _ = _create_code(client, management_headers, code_challenge=None, code_challenge_method=None)
    exchange = client.post("/v1/auth/keys", json={"code": code})

    assert exchange.status_code == 200
    assert exchange.json()["user_id"] == STORE.find_user_by_email("alice@example.com").id


def test_oauth_code_creation_unprefixed_openrouter_compat_path(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/auth/keys/code",
        headers=user_headers,
        json={"callback_url": "https://app.example.com/callback"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["id"].startswith("auth_code-")


def test_oauth_key_exchange_unprefixed_openrouter_compat_path(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    code, _ = _create_code(client, user_headers, code_challenge=None, code_challenge_method=None)

    response = client.post("/auth/keys", json={"code": code})

    assert response.status_code == 200
    assert response.json()["key"].startswith("sk-tr-v1-")


def test_oauth_app_id_is_stable_for_callback_url(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    first = client.post(
        "/v1/auth/keys/code",
        headers=user_headers,
        json={"callback_url": "https://app.example.com/callback"},
    ).json()["data"]["app_id"]
    second = client.post(
        "/v1/auth/keys/code",
        headers=user_headers,
        json={"callback_url": "https://app.example.com/callback"},
    ).json()["data"]["app_id"]
    third = client.post(
        "/v1/auth/keys/code",
        headers=user_headers,
        json={"callback_url": "https://other.example.com/callback"},
    ).json()["data"]["app_id"]

    assert first == second
    assert third != first


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://app.example.com/callback",
        "https://app.example.com:443/callback",
        "https://localhost:3000/callback",
        "http://localhost:3000/callback",
        "http://127.0.0.1:3000/callback",
    ],
)
def test_oauth_code_creation_accepts_openrouter_allowed_callback_ports(
    client: TestClient,
    user_headers: dict[str, str],
    callback_url: str,
) -> None:
    response = client.post("/v1/auth/keys/code", headers=user_headers, json={"callback_url": callback_url})

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "callback_url,message",
    [
        ("", "callback_url is required"),
        ("http://app.example.com/callback", "callback_url must be an https URL"),
        ("https://app.example.com:444/callback", "callback_url port must be 443 or 3000"),
        ("https://user:pass@app.example.com/callback", "callback_url cannot contain credentials"),
        ("not-a-url", "callback_url must be an https URL"),
    ],
)
def test_oauth_code_creation_rejects_bad_callback_urls(
    client: TestClient,
    user_headers: dict[str, str],
    callback_url: str,
    message: str,
) -> None:
    response = client.post("/v1/auth/keys/code", headers=user_headers, json={"callback_url": callback_url})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == message


@pytest.mark.parametrize(
    "patch,message",
    [
        ({"code_challenge_method": "S512", "code_challenge": "abc"}, "code_challenge_method must be S256 or plain"),
        ({"code_challenge_method": "S256"}, "code_challenge is required when code_challenge_method is set"),
        ({"limit": "-1"}, "limit must be non-negative"),
        ({"limit": "nan"}, "limit must be a dollar amount"),
        ({"usage_limit_type": "yearly"}, "usage_limit_type must be daily, weekly, or monthly"),
        ({"key_label": "x" * 101}, "key_label must be at most 100 characters"),
        ({"expires_at": "bad-date"}, "expires_at must be an ISO 8601 timestamp"),
        ({"expires_at": "2000-01-01T00:00:00Z"}, "expires_at must be in the future"),
    ],
)
def test_oauth_code_creation_rejects_bad_request_fields(
    client: TestClient,
    user_headers: dict[str, str],
    patch: dict[str, str],
    message: str,
) -> None:
    body = {"callback_url": "https://app.example.com/callback", **patch}

    response = client.post("/v1/auth/keys/code", headers=user_headers, json=body)

    assert response.status_code == 400
    assert response.json()["error"]["message"] == message


@pytest.mark.parametrize("reset", ["daily", "weekly", "monthly"])
def test_oauth_code_exchange_preserves_allowed_limit_reset(
    client: TestClient,
    user_headers: dict[str, str],
    reset: str,
) -> None:
    code, _ = _create_code(client, user_headers, usage_limit_type=reset, code_challenge=None, code_challenge_method=None)

    exchange = client.post("/v1/auth/keys", json={"code": code})

    assert exchange.status_code == 200
    assert STORE.get_key_by_raw(exchange.json()["key"]).limit_reset == reset


def test_oauth_browser_auth_page_requires_signin_and_preserves_next() -> None:
    app = create_app(
        Settings(
            environment="test",
            google_client_id="google-client",
            google_client_secret="google-secret",  # noqa: S106 - test fixture secret.
            github_client_id="github-client",
            github_client_secret="github-secret",  # noqa: S106 - test fixture secret.
        ),
        init_observability=False,
    )
    local_client = TestClient(app)

    response = local_client.get(
        "/auth?callback_url=https://app.example.com/callback&key_label=Example",
    )

    assert response.status_code == 401
    assert "/v1/auth/google/login?next=" in response.text
    assert "/v1/auth/github/login?next=" in response.text
    assert "%2Fauth%3Fcallback_url%3Dhttps%3A%2F%2Fapp.example.com%2Fcallback" in response.text


def test_oauth_browser_auth_page_rejects_invalid_callback_before_signin(client: TestClient) -> None:
    response = client.get("/auth?callback_url=http://app.example.com/callback")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "callback_url must be an https URL"


def test_oauth_browser_auth_page_rejects_non_management_bearer(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    response = client.get(
        "/auth?callback_url=https://app.example.com/callback",
        headers=inference_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "forbidden"


def test_oauth_browser_consent_page_for_active_session(client: TestClient) -> None:
    user = STORE.ensure_user("alice@example.com")
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)

    response = client.get("/auth?callback_url=https://app.example.com/callback&key_label=Example")

    assert response.status_code == 200
    assert "Authorize Example" in response.text
    assert 'action="/auth/approve"' in response.text
    assert 'name="callback_url"' in response.text


def test_oauth_browser_approve_redirects_with_code_and_user_id(client: TestClient) -> None:
    user = STORE.ensure_user("alice@example.com")
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)

    response = client.post(
        "/auth/approve",
        data={
            "callback_url": "https://app.example.com/callback?state=abc",
            "key_label": "Browser app",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "app.example.com"
    assert query["state"] == ["abc"]
    assert query["user_id"] == [user.id]
    exchange = client.post("/v1/auth/keys", json={"code": query["code"][0]})
    assert exchange.status_code == 200
    assert STORE.get_key_by_raw(exchange.json()["key"]).name == "Browser app"


def test_oauth_browser_approve_requires_active_session(client: TestClient) -> None:
    response = client.post(
        "/auth/approve",
        data={"callback_url": "https://app.example.com/callback"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_oauth_browser_approve_rejects_invalid_form_without_creating_code(client: TestClient) -> None:
    user = STORE.ensure_user("alice@example.com")
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_session)

    response = client.post("/auth/approve", data={"callback_url": "http://app.example.com/callback"})

    assert response.status_code == 400
    assert STORE.oauth_code_store.codes == {}


def test_oauth_google_login_stores_safe_next_cookie() -> None:
    app = create_app(
        Settings(environment="test", google_client_id="id", google_client_secret="secret"),  # noqa: S106
        init_observability=False,
    )
    local_client = TestClient(app)

    response = local_client.get(
        "/v1/auth/google/login?next=/auth?callback_url=https://app.example.com/callback",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "tr_oauth_next=" in response.headers["set-cookie"]


def test_oauth_google_login_rejects_external_next_cookie() -> None:
    app = create_app(
        Settings(environment="test", google_client_id="id", google_client_secret="secret"),  # noqa: S106
        init_observability=False,
    )
    local_client = TestClient(app)

    response = local_client.get(
        "/v1/auth/google/login?next=https://evil.example.com/auth",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "tr_oauth_next=" not in response.headers["set-cookie"]


def test_oauth_github_login_stores_safe_next_cookie() -> None:
    app = create_app(
        Settings(environment="test", github_client_id="id", github_client_secret="secret"),  # noqa: S106
        init_observability=False,
    )
    local_client = TestClient(app)

    response = local_client.get(
        "/v1/auth/github/login?next=/auth?callback_url=https://app.example.com/callback",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "tr_oauth_next=" in response.headers["set-cookie"]


def test_oauth_github_login_rejects_external_next_cookie() -> None:
    app = create_app(
        Settings(environment="test", github_client_id="id", github_client_secret="secret"),  # noqa: S106
        init_observability=False,
    )
    local_client = TestClient(app)

    response = local_client.get(
        "/v1/auth/github/login?next=//evil.example.com/auth",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "tr_oauth_next=" not in response.headers["set-cookie"]


def test_in_memory_oauth_code_expiry_removes_lookup() -> None:
    store = InMemoryStore()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="Expired",
        ttl_seconds=60,
        app_id=123,
    )
    code.code_expires_at = "2000-01-01T00:00:00Z"

    assert store.consume_oauth_authorization_code(raw) is None
    assert code.hash not in store.oauth_code_store.codes
    assert code.lookup_hash not in store.oauth_code_store.code_ids_by_lookup_hash


def test_gcp_oauth_code_consume_is_one_time_and_hash_only() -> None:
    store, db, _ = make_fake_store()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    raw, code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="GCP app",
        ttl_seconds=600,
        app_id=123,
        code_challenge="plain-verifier",
        code_challenge_method="plain",
    )

    assert raw not in json.dumps([row.body for row in db.rows.values()])
    consumed = store.consume_oauth_authorization_code(raw)
    replay = store.consume_oauth_authorization_code(raw)
    assert consumed is not None
    assert consumed.hash == code.hash
    assert consumed.consumed_at is not None
    assert replay is None


def test_gcp_oauth_code_expiry_deletes_code_and_lookup() -> None:
    store, db, _ = make_fake_store()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="GCP expired",
        ttl_seconds=600,
        app_id=123,
    )
    code.code_expires_at = "2000-01-01T00:00:00Z"
    store._write_entity("oauth_code", code.hash, code)

    assert store.consume_oauth_authorization_code(raw) is None
    assert ("oauth_code", code.hash) not in db.rows
    assert ("oauth_code_lookup", code.lookup_hash) not in db.rows
