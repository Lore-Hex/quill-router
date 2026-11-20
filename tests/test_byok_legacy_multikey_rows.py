"""BYOK rows written while multi-key BYOK was live still read after its removal.

Between #2732 and its removal, every BYOK row was stored with the multi-key
fields (default_key_id, additional_keys and per-key policy). The single-key
model must read those rows, by point read and by workspace list, as their
default key alone."""
from __future__ import annotations

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_codec import byok_id

MULTI_KEY_ROW = {
    "workspace_id": "ws_multi",
    "provider": "openai",
    "secret_ref": "env://OPENAI_DEFAULT",
    "key_hint": "sk-...abcd",
    "encrypted_secret": None,
    "created_at": "2026-11-18T17:00:00Z",
    "updated_at": "2026-11-19T09:00:00Z",
    # Fields only the multi-key release wrote.
    "label": "production",
    "models": ["openai/*"],
    "priority": 0,
    "fallback_to_credits": False,
    "key_disabled": False,
    "default_key_id": "k_0123456789ab",
    "additional_keys": [{"id": "k_ba9876543210", "secret_ref": "env://OPENAI_SECOND", "key_hint": "sk-...wxyz"}],
}


def test_a_multi_key_row_reads_and_lists_as_its_default_key() -> None:
    store, _db = make_fake_store()
    store._write_entity("byok", byok_id("ws_multi", "openai"), MULTI_KEY_ROW)

    config = store.get_byok_provider("ws_multi", "openai")
    assert config is not None
    assert (config.secret_ref, config.key_hint) == ("env://OPENAI_DEFAULT", "sk-...abcd")

    listed = store.list_byok_providers("ws_multi")
    assert [(c.provider, c.secret_ref) for c in listed] == [("openai", "env://OPENAI_DEFAULT")]
    assert not hasattr(listed[0], "additional_keys")


def test_a_legacy_gemini_alias_row_lists_under_its_public_provider() -> None:
    from fastapi.testclient import TestClient

    from trusted_router.config import Settings
    from trusted_router.main import create_app
    from trusted_router.storage import STORE

    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    headers = {"x-trustedrouter-user": "legacy-gemini@example.com"}
    key = client.post("/v1/keys", headers=headers, json={"name": "gemini"}).json()["data"]
    STORE.upsert_byok_provider(
        workspace_id=key["workspace_id"], provider="gemini", secret_ref="env://GEMINI", key_hint=None,  # noqa: S106
    )
    listed = client.get("/v1/byok/providers", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["provider"] for row in listed.json()["data"]] == ["google-ai-studio"]
    one = client.get("/v1/byok/providers/google-ai-studio", headers=headers)
    assert one.status_code == 200, one.text
