"""Several BYOK keys per provider, each with a model filter, a priority, and
a choice of whether a request may fall back to Credits.

BYOK routes a key admits run ahead of Credits routes by default."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from tests.fixture_routes import drop_routes, serve_on_fixture_route
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE
from trusted_router.storage_models import (
    MAX_BYOK_KEYS_PER_PROVIDER,
    ByokKey,
    ByokProviderConfig,
    EncryptedSecretEnvelope,
    byok_key_admits_model,
)

USER = {"x-trustedrouter-user": "alice@example.com"}
# Idempotency values for the replay tests (request ids, not credentials).
REPLAY_REPLAY_NO_CREDITS_1 = "replay-no-credits-1"
REPLAY_REPLAY_BEFORE_ANY_KEY = "replay-before-any-key"
MODEL = "moonshotai/kimi-k2.6"


def _client_and_key() -> tuple[TestClient, dict]:
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    created = client.post("/v1/keys", headers=USER, json={"name": "byok multi"})
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def _serve_kimi(monkeypatch: pytest.MonkeyPatch) -> None:
    drop_routes(monkeypatch, MODEL)
    for usage_type in ("Credits", "BYOK"):
        serve_on_fixture_route(
            monkeypatch, MODEL, "kimi", author="kimi", usage_type=usage_type, upstream_id="kimi-k2.6",
        )


def _store_kimi_keys(workspace_id: str, *keys: ByokKey, **default_policy) -> None:
    config = ByokProviderConfig(
        workspace_id=workspace_id,
        provider="kimi",
        secret_ref="env://KIMI_DEFAULT",  # noqa: S106 - a secret reference, not a secret.
        key_hint="default-hint",
        additional_keys=list(keys),
        **default_policy,
    )
    STORE.save_byok_provider(config)


def _authorize(client: TestClient, key: dict, **extra) -> dict:
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": MODEL,
            "route_type": "chat.completions",
            "estimated_input_tokens": 1_000,
            "max_output_tokens": 1_000,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _route_summary(data: dict) -> list[tuple[str, str | None]]:
    return [(c["usage_type"], c["byok_key_hint"]) for c in data["route_candidates"]]


# --- Routing -----------------------------------------------------------------


def test_byok_runs_ahead_of_credits_and_credits_stays_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"])

    data = _authorize(client, key)

    assert data["usage_type"] == "BYOK"
    assert data["byok_key_hint"] == "default-hint"
    assert _route_summary(data) == [("BYOK", "default-hint"), ("Credits", None)]
    # A Credits fallback remains, so the request still holds credit for it.
    assert data["credit_reservation_id"] is not None


def test_fallback_to_credits_off_drops_credits_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"], fallback_to_credits=False)

    data = _authorize(client, key)

    assert _route_summary(data) == [("BYOK", "default-hint")]
    assert data["credit_reservation_id"] is None
    assert data["limit_usage_type"] == "BYOK"


def test_model_filter_keeps_a_key_off_other_models(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"], models=["anthropic/*"], fallback_to_credits=False)

    data = _authorize(client, key)

    # The only key does not serve this model, so its no-fallback choice does
    # not apply either: the request runs on Credits.
    assert _route_summary(data) == [("Credits", None)]


def test_several_keys_become_routes_in_priority_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(
        key["workspace_id"],
        ByokKey(id="k_first", secret_ref="env://KIMI_FIRST", key_hint="first", priority=-5),  # noqa: S106
        ByokKey(id="k_off", secret_ref="env://KIMI_OFF", key_hint="off", priority=-9, disabled=True),  # noqa: S106
        ByokKey(id="k_other", secret_ref="env://KIMI_OTHER", key_hint="other", models=["openai/*"]),  # noqa: S106
        priority=3,
    )

    data = _authorize(client, key)

    assert _route_summary(data) == [("BYOK", "first"), ("BYOK", "default-hint"), ("Credits", None)]
    assert data["byok_key_hint"] == "first"
    assert data["route_candidates"][0]["byok_secret_ref"] == "env://KIMI_FIRST"  # noqa: S105 - a secret reference.


def test_explicit_provider_order_is_not_reordered(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"])

    data = _authorize(client, key, provider={"order": ["kimi"]})

    assert ("Credits", None) in _route_summary(data)
    assert _route_summary(data)[0] == ("Credits", None)


def test_explicit_credits_usage_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"], fallback_to_credits=False)

    data = _authorize(client, key, provider={"usage": "credits"})

    assert _route_summary(data) == [("Credits", None)]


# --- Key management API ------------------------------------------------------


def _put_default(client: TestClient, **body) -> dict:
    response = client.put(
        "/v1/byok/providers/kimi",
        headers=USER,
        json={"secret_ref": "env://KIMI_DEFAULT", **body},  # noqa: S106
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def test_key_api_adds_lists_patches_and_deletes_keys() -> None:
    client, _key = _client_and_key()
    provider = _put_default(client, label="main", models=["moonshotai/*"], priority=2)
    # A row created through the API gets a unique id; "default" is only the
    # id of rows written before several keys existed.
    default_id = provider["keys"][0]["id"]
    assert len(provider["keys"]) == 1 and default_id.startswith("k_")
    assert provider["keys"][0]["models"] == ["moonshotai/*"]

    added = client.post(
        "/v1/byok/providers/kimi/keys",
        headers=USER,
        json={"secret_ref": "env://KIMI_SECOND", "label": "burst", "priority": 1, "fallback_to_credits": False},  # noqa: S106
    )
    assert added.status_code == 201, added.text
    second = added.json()["data"]
    assert second["id"].startswith("k_")
    assert second["fallback_to_credits"] is False

    listed = client.get("/v1/byok/providers/kimi/keys", headers=USER).json()["data"]
    assert [k["label"] for k in listed] == ["burst", "main"]

    patched = client.patch(
        f"/v1/byok/providers/kimi/keys/{second['id']}",
        headers=USER,
        json={"priority": 9, "disabled": True},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["disabled"] is True
    listed = client.get("/v1/byok/providers/kimi/keys", headers=USER).json()["data"]
    assert [k["label"] for k in listed] == ["main", "burst"]

    # Re-setting the default key keeps the others.
    provider = _put_default(client)
    assert len(provider["keys"]) == 2

    # Deleting the default promotes the next key into its slot.
    deleted = client.delete(f"/v1/byok/providers/kimi/keys/{default_id}", headers=USER)
    assert deleted.status_code == 200, deleted.text
    listed = client.get("/v1/byok/providers/kimi/keys", headers=USER).json()["data"]
    # The promoted key keeps its own id: ids never move between keys.
    assert [(k["id"], k["label"], k["disabled"]) for k in listed] == [(second["id"], "burst", True)]
    # So a retried delete of the deleted key is a 404, not the next key gone.
    assert client.delete(f"/v1/byok/providers/kimi/keys/{default_id}", headers=USER).status_code == 404

    # Deleting the last key removes the provider.
    assert client.delete(f"/v1/byok/providers/kimi/keys/{second['id']}", headers=USER).status_code == 200
    assert client.get("/v1/byok/providers/kimi", headers=USER).status_code == 404


def test_key_api_refuses_unknown_keys_and_bad_policy() -> None:
    client, _key = _client_and_key()
    _put_default(client)
    assert client.patch("/v1/byok/providers/kimi/keys/k_missing", headers=USER, json={"priority": 1}).status_code == 404
    assert client.delete("/v1/byok/providers/kimi/keys/k_missing", headers=USER).status_code == 404
    bad = client.post(
        "/v1/byok/providers/kimi/keys",
        headers=USER,
        json={"secret_ref": "env://KIMI_X", "models": [""]},  # noqa: S106
    )
    assert bad.status_code in (400, 422), bad.text
    unknown_field = client.post(
        "/v1/byok/providers/kimi/keys",
        headers=USER,
        json={"secret_ref": "env://KIMI_X", "always_use": True},  # noqa: S106
    )
    assert unknown_field.status_code in (400, 422), unknown_field.text


def test_a_provider_holds_a_bounded_number_of_keys() -> None:
    client, _key = _client_and_key()
    _put_default(client)
    for index in range(MAX_BYOK_KEYS_PER_PROVIDER - 1):
        response = client.post(
            "/v1/byok/providers/kimi/keys", headers=USER, json={"secret_ref": f"env://KIMI_{index}"},
        )
        assert response.status_code == 201, response.text
    over = client.post("/v1/byok/providers/kimi/keys", headers=USER, json={"secret_ref": "env://KIMI_OVER"})  # noqa: S106
    assert over.status_code == 400, over.text


def test_key_shapes_never_return_secret_material() -> None:
    client, _key = _client_and_key()
    provider = _put_default(client)
    added = client.post(
        "/v1/byok/providers/kimi/keys", headers=USER, json={"secret_ref": "env://KIMI_SECOND"},  # noqa: S106
    ).json()["data"]
    for shape in (*provider["keys"], added):
        assert "encrypted_secret" not in shape
        assert "secret_ref" not in shape


# --- Storage -----------------------------------------------------------------


def test_additional_keys_survive_the_spanner_entity_codec() -> None:
    store, _db = make_fake_store()
    envelope = EncryptedSecretEnvelope(
        algorithm="a", key_ref="k", encrypted_dek="d", dek_nonce="n", ciphertext="c", nonce="o",
    )
    store.save_byok_provider(ByokProviderConfig(
        workspace_id="ws", provider="kimi", secret_ref="byok://x", fallback_to_credits=False,  # noqa: S106
        additional_keys=[ByokKey(id="k_1", secret_ref="byok://y", encrypted_secret=envelope, models=["a/*"])],  # noqa: S106
    ))

    reread = store.get_byok_provider("ws", "kimi")

    assert reread is not None
    assert reread.fallback_to_credits is False
    assert reread.additional_keys[0].encrypted_secret == envelope
    assert reread.additional_keys[0].models == ["a/*"]
    batched = store.get_byok_providers("ws", ["kimi"])["kimi"]
    assert batched is not None and batched.additional_keys[0].id == "k_1"


def test_rows_written_before_several_keys_read_as_one_default_key() -> None:
    legacy = ByokProviderConfig(workspace_id="ws", provider="kimi", secret_ref="env://X")  # noqa: S106
    keys = legacy.all_keys()
    assert [(k.id, k.models, k.fallback_to_credits, k.disabled) for k in keys] == [("default", [], True, False)]


@pytest.mark.parametrize(
    ("patterns", "model", "admitted"),
    [
        ([], "any/model", True),
        (["moonshotai/kimi-k2.6"], "moonshotai/kimi-k2.6", True),
        (["MoonshotAI/*"], "moonshotai/kimi-k2.6", True),
        (["moonshotai/*-k2.5"], "moonshotai/kimi-k2.6", False),
        (["*kimi*"], "moonshotai/kimi-k2.6", True),
        (["moonshotai/kimi-k2.?"], "moonshotai/kimi-k2.6", False),
    ],
)
def test_model_patterns(patterns: list[str], model: str, admitted: bool) -> None:
    assert byok_key_admits_model(patterns, (model,)) is admitted


def test_legacy_disabled_flags_do_not_switch_a_key_off() -> None:
    # Old rows carry "enabled"/"disabled" fields that never meant anything.
    legacy = ByokProviderConfig(
        **{"workspace_id": "ws", "provider": "kimi", "secret_ref": "env://X"},  # noqa: S106
    )
    record = {**legacy.__dict__, "disabled": True, "enabled": False}
    import json

    from trusted_router.storage_models import decode_auth_record

    decoded = decode_auth_record(json.dumps(record), ByokProviderConfig)
    assert decoded.usable_keys(("any/model",))


def test_byok_first_never_outranks_an_earlier_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    drop_routes(monkeypatch, "mistralai/fixture-primary")
    serve_on_fixture_route(monkeypatch, "mistralai/fixture-primary", "mistral", author="mistral")
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"])

    data = _authorize(client, key, model="mistralai/fixture-primary", models=[MODEL])

    assert data["model"] == "mistralai/fixture-primary"
    models = [c["model"] for c in data["route_candidates"]]
    assert models[0] == "mistralai/fixture-primary"
    kimi = [c["usage_type"] for c in data["route_candidates"] if c["model"] == MODEL]
    assert kimi == ["BYOK", "Credits"]


# --- Review findings -----------------------------------------------------------


def test_allow_fallbacks_false_keeps_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(
        key["workspace_id"],
        ByokKey(id="k_first", secret_ref="env://KIMI_FIRST", key_hint="first", priority=-5),  # noqa: S106
    )

    data = _authorize(client, key, provider={"allow_fallbacks": False})

    assert _route_summary(data) == [("BYOK", "first")]


def test_a_key_that_no_longer_admits_the_model_is_never_sent() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routes.internal import gateway as gateway_routes

    endpoint = next(e for e in MODEL_ENDPOINTS.values() if e.is_byok)
    model = MODELS[endpoint.id.split("@")[0]] if endpoint.id.split("@")[0] in MODELS else next(iter(MODELS.values()))
    config = ByokProviderConfig(
        workspace_id="ws", provider=endpoint.provider, secret_ref="env://X", key_disabled=True,  # noqa: S106
    )
    configs = {endpoint.provider: config}
    assert gateway_routes._gateway_candidate_payloads(model, endpoint, "ws", "us-central1", byok_configs=configs) == []
    payload = gateway_routes._gateway_byok_payload(config, "ws", model_id=model.id)
    assert payload["byok_secret_ref"] is None and payload["byok_encrypted_secret"] is None


def test_keys_added_to_a_legacy_gemini_row_stay_with_it_and_decrypt() -> None:
    from trusted_router.byok_crypto import decrypt_byok_secret

    client, key = _client_and_key()
    workspace_id = key["workspace_id"]
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="gemini", secret_ref="env://GEMINI_KEY", key_hint="legacy",  # noqa: S106
    )

    added = client.post(
        "/v1/byok/providers/google-ai-studio/keys", headers=USER, json={"api_key": "AIzaSecondKeyValue000000"},
    )
    assert added.status_code == 201, added.text
    rotated = client.put(
        "/v1/byok/providers/google-ai-studio", headers=USER, json={"api_key": "AIzaRotatedDefault000000"},
    )
    assert rotated.status_code == 200, rotated.text

    assert STORE.get_byok_provider(workspace_id, "google-ai-studio") is None
    legacy = STORE.get_byok_provider(workspace_id, "gemini")
    assert legacy is not None and len(legacy.all_keys()) == 2
    settings = Settings(environment="test")
    for stored in legacy.all_keys():
        assert stored.encrypted_secret is not None
        assert decrypt_byok_secret(
            stored.encrypted_secret, settings, workspace_id=workspace_id, provider="gemini",
        ).startswith("AIza")


def test_deleting_the_last_key_removes_legacy_alias_rows() -> None:
    client, key = _client_and_key()
    workspace_id = key["workspace_id"]
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="gemini", secret_ref="env://GEMINI_OLD", key_hint="old",  # noqa: S106
    )
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="google-ai-studio", secret_ref="env://AISTUDIO", key_hint="new",  # noqa: S106
    )

    deleted = client.delete("/v1/byok/providers/google-ai-studio/keys/default", headers=USER)

    assert deleted.status_code == 200, deleted.text
    assert STORE.get_byok_provider(workspace_id, "gemini") is None
    assert client.get("/v1/byok/providers/google-ai-studio", headers=USER).status_code == 404


def test_spanner_row_mutation_is_one_transaction() -> None:
    store, _db = make_fake_store()
    store.mutate_byok_provider(
        "ws", "kimi", lambda current: ByokProviderConfig(workspace_id="ws", provider="kimi", secret_ref="env://A"),  # noqa: S106
    )

    def add(current: ByokProviderConfig | None) -> ByokProviderConfig:
        assert current is not None
        current.additional_keys.append(ByokKey(id="k_2", secret_ref="env://B"))  # noqa: S106
        return current

    store.mutate_byok_provider("ws", "kimi", add)
    reread = store.get_byok_provider("ws", "kimi")
    assert reread is not None and [k.id for k in reread.all_keys()] == ["default", "k_2"]
    assert store.mutate_byok_provider("ws", "kimi", lambda current: None) is None
    assert store.get_byok_provider("ws", "kimi") is None


def test_a_retried_last_key_deletion_keeps_a_key_added_meanwhile(monkeypatch: pytest.MonkeyPatch) -> None:
    client, key = _client_and_key()
    workspace_id = key["workspace_id"]
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="kimi", secret_ref="env://KIMI_ONLY", key_hint="only",  # noqa: S106
    )
    from trusted_router.storage import InMemoryStore

    real_mutate = InMemoryStore.mutate_byok_provider

    def mutate_with_a_retry(self, ws, provider, change):
        # The first attempt sees the lone key; a concurrent POST then adds one,
        # and the retried transaction sees both.
        change(self.get_byok_provider(ws, provider))
        self.save_byok_provider(ByokProviderConfig(
            workspace_id=ws, provider=provider, secret_ref="env://KIMI_ONLY",  # noqa: S106
            additional_keys=[ByokKey(id="k_new", secret_ref="env://KIMI_NEW")],  # noqa: S106
        ))
        return real_mutate(self, ws, provider, change)

    # The backend class, not the STORE proxy (AGENTS.md, issue #333).
    monkeypatch.setattr(InMemoryStore, "mutate_byok_provider", mutate_with_a_retry)
    deleted = client.delete("/v1/byok/providers/kimi/keys/default", headers=USER)
    assert deleted.status_code == 200, deleted.text
    survivor = STORE.get_byok_provider(workspace_id, "kimi")
    assert survivor is not None and [k.id for k in survivor.all_keys()] == ["k_new"]
    assert survivor.secret_ref == "env://KIMI_NEW"  # noqa: S105


def test_requested_single_route_reads_the_request_alone() -> None:
    from types import SimpleNamespace

    from trusted_router.routes.internal.gateway import _requested_single_route

    assert _requested_single_route(SimpleNamespace(provider={"allow_fallbacks": False})) is True
    assert _requested_single_route(SimpleNamespace(provider=None, allow_fallbacks=False)) is True
    assert _requested_single_route(SimpleNamespace(provider={"order": ["a"]})) is False
    assert _requested_single_route(SimpleNamespace()) is False


def test_byok_promotion_keeps_a_global_sort_across_models() -> None:
    from types import SimpleNamespace

    from trusted_router.routes.internal.gateway import byok_route_order

    def candidate(model_id: str, usage: str) -> tuple:
        return (SimpleNamespace(id=model_id), SimpleNamespace(usage_type=usage, provider="kimi", is_byok=usage == "BYOK"))

    a_credits, b_credits, a_byok = candidate("a", "Credits"), candidate("b", "Credits"), candidate("a", "BYOK")
    config = ByokProviderConfig(workspace_id="ws", provider="kimi", secret_ref="env://X")  # noqa: S106
    order = byok_route_order({"kimi": config}, "ws")
    ranked = order([a_credits, b_credits, a_byok], SimpleNamespace(order=None))
    assert ranked == [a_byok, b_credits, a_credits]


def test_replay_applies_a_no_credits_key_added_since(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"])
    first = _authorize(client, key, idempotency_key=REPLAY_REPLAY_NO_CREDITS_1)
    assert ("Credits", None) in _route_summary(first)

    _store_kimi_keys(
        key["workspace_id"],
        ByokKey(id="k_strict", secret_ref="env://KIMI_STRICT", key_hint="strict",  # noqa: S106
                priority=-1, fallback_to_credits=False),
    )
    replayed = _authorize(client, key, idempotency_key=REPLAY_REPLAY_NO_CREDITS_1)

    assert ("Credits", None) not in _route_summary(replayed)
    assert _route_summary(replayed)[0] == ("BYOK", "strict")


def test_console_rotation_keeps_a_legacy_rows_other_keys() -> None:
    client = TestClient(create_app(Settings(environment="local", rate_limit_enabled=False), init_observability=False))
    user = STORE.ensure_user("byok-console-legacy@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    assert client.get("/console/byok").status_code == 200
    workspace_id = STORE.list_workspaces_for_user(user.id)[0].id
    STORE.save_byok_provider(ByokProviderConfig(
        workspace_id=workspace_id, provider="gemini", secret_ref="env://GEMINI_OLD",  # noqa: S106
        additional_keys=[ByokKey(id="k_extra", secret_ref="env://GEMINI_EXTRA")],  # noqa: S106
    ))
    saved = client.post(
        "/console/byok",
        data={"provider": "google-ai-studio", "secret_ref": "env://GEMINI_NEW"},  # noqa: S106
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    assert "error" not in saved.headers["location"]
    assert STORE.get_byok_provider(workspace_id, "google-ai-studio") is None
    legacy = STORE.get_byok_provider(workspace_id, "gemini")
    assert legacy is not None and legacy.secret_ref == "env://GEMINI_NEW"  # noqa: S105
    assert [k.id for k in legacy.additional_keys] == ["k_extra"]


def test_byok_first_applies_to_ranked_text_routes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    _store_kimi_keys(key["workspace_id"])

    data = _authorize(client, key, route_type=None)

    # Without a text-generation route type nothing is ranked, so catalog order
    # (Credits first) holds, and the BYOK route is still there as a fallback.
    assert _route_summary(data) == [("Credits", None), ("BYOK", "default-hint")]


def test_alias_cleanup_keeps_a_key_added_to_the_legacy_row_meanwhile(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.storage import InMemoryStore

    client, key = _client_and_key()
    workspace_id = key["workspace_id"]
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="gemini", secret_ref="env://GEMINI_OLD", key_hint="old",  # noqa: S106
    )
    STORE.upsert_byok_provider(
        workspace_id=workspace_id, provider="google-ai-studio", secret_ref="env://AISTUDIO", key_hint="new",  # noqa: S106
    )
    real_mutate = InMemoryStore.mutate_byok_provider

    def mutate_with_a_concurrent_add(self, ws, provider, change):
        if provider == "gemini":
            # A concurrent POST resolved the surviving legacy row and added a key.
            row = self.get_byok_provider(ws, provider)
            row.additional_keys.append(ByokKey(id="k_raced", secret_ref="env://RACED"))  # noqa: S106
            self.save_byok_provider(row)
        return real_mutate(self, ws, provider, change)

    monkeypatch.setattr(InMemoryStore, "mutate_byok_provider", mutate_with_a_concurrent_add)
    deleted = client.delete("/v1/byok/providers/google-ai-studio/keys/default", headers=USER)

    assert deleted.status_code == 200, deleted.text
    legacy = STORE.get_byok_provider(workspace_id, "gemini")
    assert legacy is not None and [k.id for k in legacy.additional_keys] == ["k_raced"]


def test_a_credits_only_replay_returns_the_authorization_admitted_then(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replay is the same admitted authorization (one hold, one settlement),
    # not new spending, so a key added since does not refuse it.
    _serve_kimi(monkeypatch)
    client, key = _client_and_key()
    first = _authorize(client, key, idempotency_key=REPLAY_REPLAY_BEFORE_ANY_KEY)
    _store_kimi_keys(key["workspace_id"], fallback_to_credits=False)

    replay = _authorize(client, key, idempotency_key=REPLAY_REPLAY_BEFORE_ANY_KEY)

    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["credit_reservation_id"] == first["credit_reservation_id"]


def test_a_recreated_first_key_gets_a_new_id() -> None:
    client, _key = _client_and_key()
    first = client.post("/v1/byok/providers/kimi/keys", headers=USER, json={"secret_ref": "env://KIMI_A"}).json()["data"]  # noqa: S106
    assert client.delete(f"/v1/byok/providers/kimi/keys/{first['id']}", headers=USER).status_code == 200
    second = client.post("/v1/byok/providers/kimi/keys", headers=USER, json={"secret_ref": "env://KIMI_B"}).json()["data"]  # noqa: S106

    assert second["id"] != first["id"]
    assert client.delete(f"/v1/byok/providers/kimi/keys/{first['id']}", headers=USER).status_code == 404
    assert client.get("/v1/byok/providers/kimi/keys", headers=USER).json()["data"][0]["id"] == second["id"]


def test_a_console_created_first_key_gets_a_fresh_id() -> None:
    client = TestClient(create_app(Settings(environment="local", rate_limit_enabled=False), init_observability=False))
    user = STORE.ensure_user("byok-console-ids@example.com")
    raw_token, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    assert client.get("/console/byok").status_code == 200
    workspace_id = STORE.list_workspaces_for_user(user.id)[0].id
    saved = client.post(
        "/console/byok", data={"provider": "kimi", "secret_ref": "env://KIMI_CONSOLE"}, follow_redirects=False,  # noqa: S106
    )
    assert saved.status_code == 303, saved.text
    row = STORE.get_byok_provider(workspace_id, "kimi")
    assert row is not None and row.default_key_id.startswith("k_")
