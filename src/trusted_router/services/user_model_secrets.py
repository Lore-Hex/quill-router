from __future__ import annotations

from trusted_router.byok_crypto import encrypt_control_secret
from trusted_router.key_management import KeyWrapperSettings
from trusted_router.storage_models import EncryptedSecretEnvelope

USER_MODEL_ENDPOINT_KEY_PURPOSE = "user_model_endpoint_key"
USER_MODEL_SIGNING_PURPOSE = "user_model_signing"


def encrypt_user_model_endpoint_key(
    raw_secret: str,
    settings: KeyWrapperSettings,
    *,
    workspace_id: str,
) -> EncryptedSecretEnvelope:
    encrypted_endpoint_api_key = encrypt_control_secret(
        raw_secret,
        settings,
        workspace_id=workspace_id,
        purpose=USER_MODEL_ENDPOINT_KEY_PURPOSE,
    )
    return encrypted_endpoint_api_key


def encrypt_user_model_signing_secret(
    raw_secret: str,
    settings: KeyWrapperSettings,
    *,
    workspace_id: str,
) -> EncryptedSecretEnvelope:
    encrypted_signing_secret = encrypt_control_secret(
        raw_secret,
        settings,
        workspace_id=workspace_id,
        purpose=USER_MODEL_SIGNING_PURPOSE,
    )
    return encrypted_signing_secret
