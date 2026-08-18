"""Privacy-bounded Google Ads conversion records.

The durable rows carry an envelope-encrypted Google click identifier, event
time, exact integer money, and an opaque transaction ID. They never carry an
email, user, workspace, API key, model, provider, prompt, or output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Protocol, cast

from trusted_router.byok_crypto import decrypt_control_secret, encrypt_control_secret
from trusted_router.key_management import KeyWrapperSettings
from trusted_router.storage_models import (
    AcquisitionAttribution,
    EncryptedGoogleClickEnvelope,
    EncryptedSecretEnvelope,
    GoogleAdsConversion,
)

GOOGLE_ADS_SIGNUP_ACTION = "TrustedRouter Signup"
GOOGLE_ADS_ACTIVATED_ACTION = "TrustedRouter Activated API User"
GOOGLE_ADS_PURCHASE_ACTION = "TrustedRouter Credit Purchase"

GOOGLE_ADS_ACTION_BY_EVENT = {
    "signup_completed": GOOGLE_ADS_SIGNUP_ACTION,
    "first_successful_api_call": GOOGLE_ADS_ACTIVATED_ACTION,
    "credit_purchase_completed": GOOGLE_ADS_PURCHASE_ACTION,
}

_ENTITY_KIND_PREFIX = "google_ads_conversion_"
_CLICK_ID_KINDS = frozenset({"gclid", "gbraid", "wbraid"})


class GoogleAdsKeySettings(Protocol):
    environment: str
    google_data_manager_kms_key_name: str | None


@dataclass
class _GoogleAdsKeyWrapperConfig:
    environment: str
    byok_kms_key_name: str | None
    byok_envelope_key_b64: str | None = None
    byok_envelope_key_ref: str = "trustedrouter/google-ads-click-envelope/v1"


def google_ads_key_wrapper_config(
    settings: GoogleAdsKeySettings,
) -> KeyWrapperSettings:
    """Use a KMS boundary that cannot unwrap customer BYOK provider keys."""
    return _GoogleAdsKeyWrapperConfig(
        environment=settings.environment,
        byok_kms_key_name=settings.google_data_manager_kms_key_name,
    )


def encrypt_google_ads_click_id(
    raw_click_id: str,
    settings: GoogleAdsKeySettings,
    *,
    attribution_id: str,
) -> EncryptedGoogleClickEnvelope:
    envelope = encrypt_control_secret(
        raw_click_id,
        google_ads_key_wrapper_config(settings),
        workspace_id=attribution_id,
        purpose="google_ads_click_id",
    )
    return EncryptedGoogleClickEnvelope(
        algorithm=envelope.algorithm,
        key_ref=envelope.key_ref,
        encrypted_dek=envelope.encrypted_dek,
        dek_nonce=envelope.dek_nonce,
        ciphertext=envelope.ciphertext,
        nonce=envelope.nonce,
    )


def decrypt_google_ads_click_id(
    envelope: EncryptedGoogleClickEnvelope,
    settings: GoogleAdsKeySettings,
    *,
    attribution_id: str,
) -> str:
    return decrypt_control_secret(
        cast(EncryptedSecretEnvelope, envelope),
        google_ads_key_wrapper_config(settings),
        workspace_id=attribution_id,
        purpose="google_ads_click_id",
    )


def build_google_ads_conversion(
    record: AcquisitionAttribution,
    event: str,
    *,
    occurred_at: str,
    value_microdollars: int = 0,
    ordinal: int = 0,
) -> GoogleAdsConversion | None:
    action = GOOGLE_ADS_ACTION_BY_EVENT.get(event)
    if (
        action is None
        or record.google_click_id_kind not in _CLICK_ID_KINDS
        or record.encrypted_google_click_id is None
        or (
            record.google_click_expires_at is not None
            and parse_utc_timestamp(occurred_at)
            >= parse_utc_timestamp(record.google_click_expires_at)
        )
    ):
        return None
    if value_microdollars < 0:
        raise ValueError("Google Ads conversion value cannot be negative")
    order_id = hashlib.sha256(
        "\0".join(
            (record.anonymous_id, event, occurred_at, str(ordinal))
        ).encode("utf-8")
    ).hexdigest()
    return GoogleAdsConversion(
        order_id=order_id,
        conversion_action=action,
        occurred_at=occurred_at,
        attribution_id=record.anonymous_id,
        click_id_kind=record.google_click_id_kind,
        encrypted_click_id=record.encrypted_google_click_id,
        click_expires_at=record.google_click_expires_at,
        value_microdollars=value_microdollars,
    )


def google_ads_conversion_kind(occurred_at: str) -> str:
    return f"{_ENTITY_KIND_PREFIX}{parse_utc_timestamp(occurred_at):%Y%m}"


def google_ads_conversion_entity_id(conversion: GoogleAdsConversion) -> str:
    timestamp = parse_utc_timestamp(conversion.occurred_at)
    return f"{timestamp:%Y%m%dT%H%M%SZ}#{conversion.order_id}"


def google_ads_conversion_kinds_since(
    since: dt.datetime,
    *,
    now: dt.datetime | None = None,
) -> list[str]:
    start = _as_utc(since).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = _as_utc(now or dt.datetime.now(dt.UTC)).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    kinds: list[str] = []
    cursor = start
    while cursor <= end:
        kinds.append(f"{_ENTITY_KIND_PREFIX}{cursor:%Y%m}")
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    return kinds


def parse_utc_timestamp(value: str) -> dt.datetime:
    return _as_utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


__all__ = [
    "GOOGLE_ADS_ACTIVATED_ACTION",
    "GOOGLE_ADS_PURCHASE_ACTION",
    "GOOGLE_ADS_SIGNUP_ACTION",
    "build_google_ads_conversion",
    "decrypt_google_ads_click_id",
    "encrypt_google_ads_click_id",
    "google_ads_key_wrapper_config",
    "google_ads_conversion_entity_id",
    "google_ads_conversion_kind",
    "google_ads_conversion_kinds_since",
    "parse_utc_timestamp",
]
