from __future__ import annotations

import base64
import copy
import secrets
from dataclasses import asdict
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trusted_router.byok_aad_backfill import (
    BackfillRunner,
    EntityRow,
    KmsOperationRateLimiter,
)
from trusted_router.byok_crypto import (
    ALGORITHM,
    ALGORITHM_V2,
    _aad,
    decrypt_byok_secret,
    decrypt_control_secret,
)
from trusted_router.config import Settings
from trusted_router.key_management import KeyWrapperConfig, key_wrapper_for
from trusted_router.storage_models import EncryptedSecretEnvelope


class MemoryEntityStore:
    def __init__(self, rows: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.rows = copy.deepcopy(rows)
        self.force_conflict = False

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        keys = sorted(key for key in self.rows if after is None or key > after)[:limit]
        return [
            EntityRow(
                kind=kind,
                entity_id=entity_id,
                body=copy.deepcopy(self.rows[(kind, entity_id)]),
                original_body=copy.deepcopy(self.rows[(kind, entity_id)]),
            )
            for kind, entity_id in keys
        ]

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool:
        key = (row.kind, row.entity_id)
        if self.force_conflict:
            self.rows[key]["concurrent_rotation"] = True
            self.force_conflict = False
        if self.rows[key] != row.original_body:
            return False
        self.rows[key] = copy.deepcopy(new_body)
        return True


def _settings() -> Settings:
    return Settings(environment="test")


def _v1_envelope(
    plaintext: str,
    settings: Settings,
    *,
    workspace_id: str,
    context: str,
) -> dict[str, str]:
    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    dek_nonce = secrets.token_bytes(12)
    aad = _aad(workspace_id, context)
    wrapper = key_wrapper_for(settings)
    envelope = EncryptedSecretEnvelope(
        algorithm=ALGORITHM,
        key_ref=wrapper.key_ref,
        encrypted_dek=base64.urlsafe_b64encode(
            wrapper.wrap(dek, nonce=dek_nonce, aad=aad)
        ).decode("ascii"),
        dek_nonce=base64.urlsafe_b64encode(dek_nonce).decode("ascii"),
        ciphertext=base64.urlsafe_b64encode(
            AESGCM(dek).encrypt(nonce, plaintext.encode(), aad)
        ).decode("ascii"),
        nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
    )
    return asdict(envelope)


def _apply(store: MemoryEntityStore, settings: Settings, logs: list[str] | None = None):
    return BackfillRunner(
        store,
        settings=settings,
        apply=True,
        kms_operations_per_second=1000,
        reporter=(logs.append if logs is not None else lambda _message: None),
        sleep=lambda _seconds: None,
    ).run(batch_size=1)


def test_operator_key_config_is_kms_only_and_fails_closed() -> None:
    key_name = "projects/example/locations/global/keyRings/ring/cryptoKeys/byok"

    assert key_wrapper_for(KeyWrapperConfig(byok_kms_key_name=key_name)).key_ref == key_name
    with pytest.raises(ValueError, match="required outside local/test"):
        key_wrapper_for(KeyWrapperConfig())


def test_provider_backfill_is_verified_resumable_and_idempotent() -> None:
    settings = _settings()
    token_value = "provider-secret-never-log"  # noqa: S105 - synthetic crypto fixture
    body = {
        "workspace_id": "workspace-1",
        "provider": "anthropic",
        "secret_ref": "encrypted://anthropic",
        "encrypted_secret": _v1_envelope(
            token_value,
            settings,
            workspace_id="workspace-1",
            context="anthropic",
        ),
    }
    store = MemoryEntityStore({("byok", "workspace-1#anthropic"): body})

    audit = BackfillRunner(store, reporter=lambda _message: None).run()
    assert audit.v1_envelopes == 1
    assert audit.rows_updated == 0

    migrated = _apply(store, settings)
    assert migrated.envelopes_migrated == 1
    assert migrated.rows_updated == 1
    raw = store.rows[("byok", "workspace-1#anthropic")]["encrypted_secret"]
    assert raw["algorithm"] == ALGORITHM_V2
    assert (
        decrypt_byok_secret(
            EncryptedSecretEnvelope(**raw),
            settings,
            workspace_id="workspace-1",
            provider="anthropic",
        )
        == token_value
    )

    repeated = _apply(store, settings)
    assert repeated.v1_envelopes == 0
    assert repeated.v2_envelopes == 1
    assert repeated.rows_updated == 0


def test_broadcast_backfill_migrates_both_secret_fields_atomically() -> None:
    settings = _settings()
    destination_id = "bdst_123"
    workspace_id = "workspace-2"
    api_key = "posthog-project-token"
    headers = '{"Authorization":"Bearer private"}'
    body = {
        "id": destination_id,
        "workspace_id": workspace_id,
        "type": "webhook",
        "encrypted_api_key": _v1_envelope(
            api_key,
            settings,
            workspace_id=workspace_id,
            context=f"broadcast:{destination_id}:api_key",
        ),
        "encrypted_headers": _v1_envelope(
            headers,
            settings,
            workspace_id=workspace_id,
            context=f"broadcast:{destination_id}:headers",
        ),
    }
    store = MemoryEntityStore({("broadcast_destination", destination_id): body})

    migrated = _apply(store, settings)
    assert migrated.envelopes_migrated == 2
    assert migrated.rows_updated == 1
    current = store.rows[("broadcast_destination", destination_id)]
    assert current["encrypted_api_key"]["algorithm"] == ALGORITHM_V2
    assert current["encrypted_headers"]["algorithm"] == ALGORITHM_V2
    assert (
        decrypt_control_secret(
            EncryptedSecretEnvelope(**current["encrypted_api_key"]),
            settings,
            workspace_id=workspace_id,
            purpose=f"broadcast:{destination_id}:api_key",
        )
        == api_key
    )
    assert (
        decrypt_control_secret(
            EncryptedSecretEnvelope(**current["encrypted_headers"]),
            settings,
            workspace_id=workspace_id,
            purpose=f"broadcast:{destination_id}:headers",
        )
        == headers
    )


def test_decrypt_failure_never_writes_or_logs_secret_material() -> None:
    settings = _settings()
    token_value = "must-not-appear-anywhere"  # noqa: S105 - synthetic crypto fixture
    envelope = _v1_envelope(
        token_value,
        settings,
        workspace_id="workspace-3",
        context="openai",
    )
    envelope["ciphertext"] = "corrupt"
    body = {
        "workspace_id": "workspace-3",
        "provider": "openai",
        "encrypted_secret": envelope,
    }
    store = MemoryEntityStore({("byok", "workspace-3#openai"): body})
    logs: list[str] = []

    result = _apply(store, settings, logs)

    assert result.failures == 1
    assert result.rows_updated == 0
    assert store.rows[("byok", "workspace-3#openai")] == body
    assert token_value not in "\n".join(logs)
    assert envelope["ciphertext"] not in "\n".join(logs)


def test_concurrent_rotation_wins_compare_and_swap() -> None:
    settings = _settings()
    body = {
        "workspace_id": "workspace-4",
        "provider": "anthropic",
        "encrypted_secret": _v1_envelope(
            "old-key",
            settings,
            workspace_id="workspace-4",
            context="anthropic",
        ),
    }
    store = MemoryEntityStore({("byok", "workspace-4#anthropic"): body})
    store.force_conflict = True

    result = _apply(store, settings)

    assert result.conflicts == 1
    assert result.rows_updated == 0
    assert store.rows[("byok", "workspace-4#anthropic")]["concurrent_rotation"] is True
    assert (
        store.rows[("byok", "workspace-4#anthropic")]["encrypted_secret"]["algorithm"]
        == ALGORITHM
    )


def test_unknown_algorithm_is_reported_and_never_rewritten() -> None:
    body = {
        "workspace_id": "workspace-5",
        "provider": "anthropic",
        "encrypted_secret": {"algorithm": "future-v99"},
    }
    store = MemoryEntityStore({("byok", "workspace-5#anthropic"): body})

    result = BackfillRunner(store, reporter=lambda _message: None).run()

    assert result.unsupported_algorithms == 1
    assert result.rows_updated == 0
    assert store.rows[("byok", "workspace-5#anthropic")] == body


def test_resume_cursor_and_small_batches_do_not_repeat_rows() -> None:
    store = MemoryEntityStore(
        {
            ("broadcast_destination", "a"): {"workspace_id": "w"},
            ("byok", "a"): {"workspace_id": "w", "provider": "a"},
            ("byok", "b"): {"workspace_id": "w", "provider": "b"},
        }
    )

    result = BackfillRunner(store, reporter=lambda _message: None).run(
        batch_size=1,
        after=("broadcast_destination", "a"),
    )

    assert result.rows_scanned == 2
    assert result.missing_envelopes == 2


def test_kms_rate_limiter_accounts_for_multiple_operations() -> None:
    now = [10.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = KmsOperationRateLimiter(
        5.0,
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    limiter.acquire(3)
    limiter.acquire(3)

    assert sleeps == [pytest.approx(0.6)]
