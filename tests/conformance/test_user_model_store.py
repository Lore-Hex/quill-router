from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import EncryptedSecretEnvelope, UserProvidedModel
from trusted_router.store_protocol import Store
from trusted_router.types import UsageType


def _memory_store() -> Store:
    return InMemoryStore()


def _spanner_store() -> Store:
    store, _database, _bigtable = make_fake_store()
    return store


@pytest.fixture(params=(_memory_store, _spanner_store), ids=("memory", "spanner"))
def user_model_store(request: pytest.FixtureRequest) -> Store:
    factory: Callable[[], Store] = request.param
    return factory()


def _create(
    store: Store,
    slug: str,
    *,
    owner_user_id: str = "owner",
    kind: str = "machine",
) -> UserProvidedModel:
    return store.create_user_model(
        owner_user_id=owner_user_id,
        owner_workspace_id=f"workspace-{owner_user_id}",
        name=f"Model {slug}",
        kind=kind,
        display_name=f"operator-{owner_user_id}",
        endpoint_url="https://owner.example/api",
        heartbeat_interval_seconds=30,
        slug=slug,
    )


def test_shared_slug_namespace_rejects_both_creation_orders(
    user_model_store: Store,
) -> None:
    store = user_model_store
    store.create_custom_model(
        owner_user_id="wrapper-owner",
        owner_workspace_id="wrapper-workspace",
        name="Wrapper",
        base_model_id="openai/gpt-4o-mini",
        hidden_prompt="hidden",
        slug="shared-a",
    )
    with pytest.raises(ValueError, match="^custom_model_slug_taken$"):
        _create(store, "shared-a")

    _create(store, "shared-b")
    with pytest.raises(ValueError, match="^custom_model_slug_taken$"):
        store.create_custom_model(
            owner_user_id="wrapper-owner",
            owner_workspace_id="wrapper-workspace",
            name="Wrapper",
            base_model_id="openai/gpt-4o-mini",
            hidden_prompt="hidden",
            slug="shared-b",
        )


def test_shared_slug_namespace_rejects_renames(user_model_store: Store) -> None:
    store = user_model_store
    user_model = _create(store, "rename-user")
    wrapper = store.create_custom_model(
        owner_user_id="wrapper-owner",
        owner_workspace_id="wrapper-workspace",
        name="Wrapper",
        base_model_id="openai/gpt-4o-mini",
        hidden_prompt="hidden",
        slug="rename-wrapper",
    )

    with pytest.raises(ValueError, match="^custom_model_slug_taken$"):
        store.update_user_model(
            user_model.id,
            owner_user_id=user_model.owner_user_id,
            patch={"slug": "rename-wrapper"},
        )
    with pytest.raises(ValueError, match="^custom_model_slug_taken$"):
        store.update_custom_model(
            wrapper.id,
            owner_user_id=wrapper.owner_user_id,
            patch={"slug": "rename-user"},
        )


def test_only_edit_operations_bump_revision(user_model_store: Store) -> None:
    store = user_model_store
    model = _create(store, "revision")
    assert model.revision == 1

    model = store.update_user_model(
        model.id,
        owner_user_id=model.owner_user_id,
        patch={"name": "Edited"},
    )
    assert model.revision == 2

    assert store.set_user_model_online(
        model.id, owner_user_id=model.owner_user_id, online=True
    ).revision == 2
    assert store.record_user_model_heartbeat(
        model.id, expires_at="2030-01-01T00:00:00Z"
    ).revision == 2
    assert store.record_user_model_probe(
        model.id,
        status="ok",
        checked_at="2030-01-01T00:00:01Z",
    ).revision == 2
    assert store.record_user_model_dispatch_result(model.id, success=False).revision == 2
    assert store.record_user_model_dispatch_result(model.id, success=True).revision == 2


def test_three_dispatch_strikes_clock_model_out_and_success_resets(
    user_model_store: Store,
) -> None:
    store = user_model_store
    model = _create(store, "strikes")
    store.set_user_model_online(model.id, owner_user_id=model.owner_user_id, online=True)

    assert store.record_user_model_dispatch_result(
        model.id, success=False
    ).consecutive_dispatch_failures == 1
    reset = store.record_user_model_dispatch_result(model.id, success=True)
    assert reset.consecutive_dispatch_failures == 0
    assert reset.online is True

    store.record_user_model_dispatch_result(model.id, success=False)
    store.record_user_model_dispatch_result(model.id, success=False)
    struck_out = store.record_user_model_dispatch_result(model.id, success=False)
    assert struck_out.consecutive_dispatch_failures == 3
    assert struck_out.online is False


def test_user_model_limit_is_three_per_owner(user_model_store: Store) -> None:
    store = user_model_store
    for index in range(3):
        _create(store, f"limit-{index}")
    _create(store, "other-owner", owner_user_id="other")

    with pytest.raises(ValueError, match="^custom_model_limit_exceeded$"):
        _create(store, "limit-3")


def test_gateway_authorization_round_trips_frozen_user_model_fields(
    user_model_store: Store,
) -> None:
    store = user_model_store
    authorization = store.create_gateway_authorization(
        workspace_id="workspace",
        key_hash="key-hash",
        model_id="trustedrouter/user-frozen",
        provider="trustedrouter",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=123,
        credit_reservation_id=None,
        user_provided_model_id="trustedrouter/user-frozen",
        user_provided_model_revision=7,
        user_model_prompt_price_microdollars_per_m=11,
        user_model_completion_price_microdollars_per_m=22,
        user_model_owner_user_id="owner",
    )
    loaded = store.get_gateway_authorization(authorization.id)
    assert loaded is not None
    assert loaded.user_provided_model_id == "trustedrouter/user-frozen"
    assert loaded.user_provided_model_revision == 7
    assert loaded.user_model_prompt_price_microdollars_per_m == 11
    assert loaded.user_model_completion_price_microdollars_per_m == 22
    assert loaded.user_model_owner_user_id == "owner"


def test_user_model_slots_are_idempotent_and_release_capacity(
    user_model_store: Store,
) -> None:
    store = user_model_store
    model_id = "trustedrouter/user-slots"

    assert store.acquire_user_model_slot(model_id, "gwa-first", limit=1, ttl_seconds=600)
    assert store.acquire_user_model_slot(model_id, "gwa-first", limit=1, ttl_seconds=600)
    assert not store.acquire_user_model_slot(model_id, "gwa-second", limit=1, ttl_seconds=600)

    store.release_user_model_slot(model_id, "gwa-first")
    store.release_user_model_slot(model_id, "gwa-first")
    assert store.acquire_user_model_slot(model_id, "gwa-second", limit=1, ttl_seconds=600)


def test_user_model_slot_expires_after_its_ttl(
    user_model_store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enclave that dies between authorize and settle must not black a
    model out for longer than one dispatch budget: the unreleased slot stops
    counting once its own ttl has passed."""
    import datetime as dt
    import time

    store = user_model_store
    model_id = "trustedrouter/user-slot-ttl"
    assert store.acquire_user_model_slot(model_id, "gwa-stuck", limit=1, ttl_seconds=30)
    assert not store.acquire_user_model_slot(model_id, "gwa-next", limit=1, ttl_seconds=30)

    # Advance both clocks the two backends use by more than the ttl.
    real_monotonic = time.monotonic
    real_now = dt.datetime.now
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 31)

    class _Shifted(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real_now(tz) + dt.timedelta(seconds=31)

    monkeypatch.setattr(
        "trusted_router.storage_gcp_user_models.dt.datetime", _Shifted, raising=False
    )
    assert store.acquire_user_model_slot(model_id, "gwa-next", limit=1, ttl_seconds=30)


def test_user_model_coerces_secret_envelope_dicts() -> None:
    raw = {
        "algorithm": "test",
        "key_ref": "key",
        "encrypted_dek": "dek",
        "dek_nonce": "dek-nonce",
        "ciphertext": "ciphertext",
        "nonce": "nonce",
    }
    model = UserProvidedModel(
        id="trustedrouter/user-envelope",
        owner_user_id="owner",
        owner_workspace_id="workspace",
        name="Envelope",
        kind="machine",
        encrypted_endpoint_api_key=raw,  # type: ignore[arg-type]
        encrypted_signing_secret=raw,  # type: ignore[arg-type]
    )
    assert isinstance(model.encrypted_endpoint_api_key, EncryptedSecretEnvelope)
    assert isinstance(model.encrypted_signing_secret, EncryptedSecretEnvelope)
