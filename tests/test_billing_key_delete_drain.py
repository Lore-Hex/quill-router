"""Deleting an API key drops its typed tr_key_limit row. If a typed hold is still
in flight, the settle's key release would match 0 rows ("release row-count != 1")
and strand the hold. So key deletion is refused (503 + Retry-After) while the key
has an open typed hold; the client retries after it drains (seconds).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router import storage
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE


def test_key_has_open_typed_hold_unit() -> None:
    store, db, _ = make_fake_store()
    _raw, key = store.api_keys.create(
        workspace_id="w", name="k", creator_user_id=None, limit_microdollars=None
    )
    assert store.key_has_open_typed_hold(key.hash) is False  # no reservations

    db.reservations["r1"] = {"reservation_id": "r1", "key_hash": key.hash, "settled": False}
    assert store.key_has_open_typed_hold(key.hash) is True  # open hold

    db.reservations["r1"]["settled"] = True
    assert store.key_has_open_typed_hold(key.hash) is False  # settled → drained


def _client_and_key(email: str) -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"}
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def test_delete_key_refused_while_typed_hold_in_flight(monkeypatch) -> None:
    monkeypatch.setattr(storage.InMemoryStore, "key_has_open_typed_hold", lambda self, h: True)
    email = "kdel@example.com"
    client, key = _client_and_key(email)
    r = client.delete(f"/v1/keys/{key['hash']}", headers={"x-trustedrouter-user": email})
    assert r.status_code == 503, r.text
    assert r.headers.get("retry-after") == "5"
    # key NOT deleted (still listed)
    assert STORE.get_key_by_hash(key["hash"]) is not None


def test_delete_key_succeeds_when_no_hold() -> None:
    email = "kdel2@example.com"
    client, key = _client_and_key(email)
    r = client.delete(f"/v1/keys/{key['hash']}", headers={"x-trustedrouter-user": email})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted"] is True
