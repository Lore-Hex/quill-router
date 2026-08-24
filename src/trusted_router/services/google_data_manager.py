"""Direct, metadata-only Google Ads Data Manager conversion delivery."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from trusted_router.config import Settings
from trusted_router.google_ads_conversions import (
    GOOGLE_ADS_ACTIVATED_ACTION,
    GOOGLE_ADS_PURCHASE_ACTION,
    GOOGLE_ADS_SIGNUP_ACTION,
    decrypt_google_ads_click_id,
)
from trusted_router.storage_models import GoogleAdsConversion

log = logging.getLogger(__name__)

DATA_MANAGER_INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"
DATA_MANAGER_STATUS_URL = (
    "https://datamanager.googleapis.com/v1/requestStatus:retrieve"
)
DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
MAX_EVENTS_PER_REQUEST = 2_000

_DESTINATION_REFERENCE_BY_ACTION = {
    GOOGLE_ADS_SIGNUP_ACTION: "signup",
    GOOGLE_ADS_ACTIVATED_ACTION: "activation",
    GOOGLE_ADS_PURCHASE_ACTION: "purchase",
}
_NUMERIC_ID_RE = re.compile(r"^[0-9]+$")
_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")


@dataclass(frozen=True)
class GoogleDataManagerConfig:
    project_id: str
    account_id: str
    signup_action_id: str
    activated_action_id: str
    purchase_action_id: str
    login_account_id: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleDataManagerConfig:
        return cls(
            project_id=settings.gcp_project_id.strip(),
            account_id=_numeric_id(
                settings.google_data_manager_account_id,
                "Google Ads account ID",
            ),
            signup_action_id=_numeric_id(
                settings.google_data_manager_signup_action_id,
                "signup conversion action ID",
            ),
            activated_action_id=_numeric_id(
                settings.google_data_manager_activated_action_id,
                "activation conversion action ID",
            ),
            purchase_action_id=_numeric_id(
                settings.google_data_manager_purchase_action_id,
                "purchase conversion action ID",
            ),
            login_account_id=(
                _numeric_id(
                    settings.google_data_manager_login_account_id,
                    "login account ID",
                )
                if settings.google_data_manager_login_account_id
                else None
            ),
        )


@dataclass(frozen=True)
class GoogleDataManagerIngestResult:
    request_id: str
    warning_count: int


@dataclass(frozen=True)
class GoogleDataManagerRunResult:
    claimed: int
    submitted: int
    failed: int
    repaired: int
    purged: int
    request_id: str | None = None


class GoogleDataManagerUploadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.request_id = request_id


class GoogleAdsDeliveryStore(Protocol):
    def purge_expired_google_ads_click_ids(self, *, before: str, limit: int) -> int: ...

    def repair_google_ads_delivery_queue(self, *, since: str, limit: int) -> int: ...

    def claim_google_ads_deliveries(
        self,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[GoogleAdsConversion]: ...

    def mark_google_ads_delivery_submitted(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        request_id: str,
    ) -> GoogleAdsConversion | None: ...

    def mark_google_ads_delivery_failed(
        self,
        *,
        order_id: str,
        occurred_at: str,
        lease_owner: str,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> GoogleAdsConversion | None: ...


class MetadataAccessTokenProvider:
    """Fetch and cache a scoped Cloud Run service-identity token."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        try:
            response = self._client.get(
                METADATA_IDENTITY_URL,
                params={"scopes": DATA_MANAGER_SCOPE, "enforce_scopes": "true"},
                headers={"Metadata-Flavor": "Google"},
            )
        except httpx.HTTPError as exc:
            raise GoogleDataManagerUploadError(
                "Cloud Run identity token request failed",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise GoogleDataManagerUploadError(
                f"Cloud Run identity token request returned HTTP {response.status_code}",
                retryable=_is_retryable_status(response.status_code),
            )
        try:
            payload = response.json()
            token = str(payload["access_token"])
            expires_in = max(int(payload.get("expires_in", 300)), 1)
        except (KeyError, TypeError, ValueError) as exc:
            raise GoogleDataManagerUploadError(
                "Cloud Run identity token response was invalid",
                retryable=True,
            ) from exc
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - 60, 1)
        return token


class GoogleDataManagerClient:
    def __init__(
        self,
        *,
        config: GoogleDataManagerConfig,
        settings: Settings,
        client: httpx.Client,
        token_provider: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._settings = settings
        self._client = client
        self._token_provider = token_provider
        self._sleep = sleep

    def ingest(self, conversions: list[GoogleAdsConversion]) -> GoogleDataManagerIngestResult:
        body = encode_google_data_manager_request(
            conversions,
            config=self._config,
            settings=self._settings,
        )
        headers = self._headers()
        try:
            response = self._client.post(
                DATA_MANAGER_INGEST_URL,
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise GoogleDataManagerUploadError(
                "Google Data Manager request failed",
                retryable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise GoogleDataManagerUploadError(
                f"Google Data Manager returned HTTP {response.status_code}"
                + _google_error_suffix(response),
                retryable=_is_retryable_status(response.status_code),
            )
        try:
            payload = response.json()
            request_id = str(payload["requestId"]).strip()
            warnings = payload.get("fieldWarnings", [])
        except (KeyError, TypeError, ValueError) as exc:
            raise GoogleDataManagerUploadError(
                "Google Data Manager response did not contain a request ID",
                retryable=True,
            ) from exc
        if not request_id:
            raise GoogleDataManagerUploadError(
                "Google Data Manager response did not contain a request ID",
                retryable=True,
            )
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        self._wait_for_success(request_id, headers=headers)
        return GoogleDataManagerIngestResult(request_id, warning_count)

    def _wait_for_success(self, request_id: str, *, headers: dict[str, str]) -> None:
        attempts = self._settings.google_data_manager_status_poll_attempts
        for attempt in range(attempts):
            try:
                response = self._client.get(
                    DATA_MANAGER_STATUS_URL,
                    params={"requestId": request_id},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise GoogleDataManagerUploadError(
                    "Google Data Manager status request failed",
                    retryable=True,
                    request_id=request_id,
                ) from exc
            if not 200 <= response.status_code < 300:
                raise GoogleDataManagerUploadError(
                    f"Google Data Manager status returned HTTP {response.status_code}"
                    + _google_error_suffix(response),
                    retryable=_is_retryable_status(response.status_code),
                    request_id=request_id,
                )
            statuses, reasons = _request_statuses(response)
            if statuses and all(status == "SUCCESS" for status in statuses):
                return
            if any(status in {"FAILED", "PARTIAL_SUCCESS"} for status in statuses):
                detail = ",".join(sorted(reasons))[:300]
                suffix = f" reasons={detail}" if detail else ""
                raise GoogleDataManagerUploadError(
                    "Google Data Manager processing failed" + suffix,
                    retryable=True,
                    request_id=request_id,
                )
            if attempt + 1 < attempts:
                self._sleep(self._settings.google_data_manager_status_poll_seconds)
        raise GoogleDataManagerUploadError(
            "Google Data Manager processing did not finish before the polling deadline",
            retryable=True,
            request_id=request_id,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            "User-Agent": "TrustedRouter-DataManager/2",
            "x-goog-user-project": self._config.project_id,
        }


def encode_google_data_manager_request(
    conversions: list[GoogleAdsConversion],
    *,
    config: GoogleDataManagerConfig,
    settings: Settings,
) -> bytes:
    if not conversions:
        raise ValueError("at least one Google Ads conversion is required")
    if len(conversions) > MAX_EVENTS_PER_REQUEST:
        raise ValueError(f"Google Data Manager accepts at most {MAX_EVENTS_PER_REQUEST} events")
    references = {_destination_reference(conversion) for conversion in conversions}
    destinations = [
        _destination(reference, config=config)
        for reference in ("signup", "activation", "purchase")
        if reference in references
    ]
    decrypted: dict[tuple[str, str], str] = {}
    events: list[str] = []
    for conversion in conversions:
        if conversion.encrypted_click_id is None:
            raise ValueError("encrypted Google click identifier is unavailable")
        cache_key = (conversion.attribution_id, conversion.encrypted_click_id.ciphertext)
        click_id = decrypted.get(cache_key)
        if click_id is None:
            click_id = decrypt_google_ads_click_id(
                conversion.encrypted_click_id,
                settings,
                attribution_id=conversion.attribution_id,
            )
            if not _CLICK_ID_RE.fullmatch(click_id):
                raise ValueError("decrypted Google click identifier is invalid")
            decrypted[cache_key] = click_id
        events.append(_encode_event(conversion, click_id=click_id))
    destination_json = json.dumps(destinations, separators=(",", ":"), sort_keys=True)
    return (
        '{"destinations":'
        + destination_json
        + ',"events":['
        + ",".join(events)
        + "]}"
    ).encode("utf-8")


def run_google_data_manager_once(
    *,
    store: GoogleAdsDeliveryStore,
    settings: Settings,
    client: GoogleDataManagerClient,
) -> GoogleDataManagerRunResult:
    if not settings.google_data_manager_enabled:
        return GoogleDataManagerRunResult(0, 0, 0, 0, 0)
    purged = store.purge_expired_google_ads_click_ids(
        before=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        limit=settings.google_data_manager_batch_size,
    )
    since = (
        dt.datetime.now(dt.UTC).replace(microsecond=0)
        - dt.timedelta(days=settings.google_data_manager_repair_lookback_days)
    ).isoformat().replace("+00:00", "Z")
    repaired = store.repair_google_ads_delivery_queue(
        since=since,
        limit=settings.google_data_manager_batch_size,
    )
    conversions = store.claim_google_ads_deliveries(
        limit=settings.google_data_manager_batch_size,
        lease_seconds=settings.google_data_manager_lease_seconds,
    )
    if not conversions:
        return GoogleDataManagerRunResult(0, 0, 0, repaired, purged)
    try:
        result = client.ingest(conversions)
    except (GoogleDataManagerUploadError, ValueError) as exc:
        retryable = not isinstance(exc, ValueError) and exc.retryable
        request_id = exc.request_id if isinstance(exc, GoogleDataManagerUploadError) else None
        error = str(exc)
        if request_id:
            error = f"{error} request_id={request_id[:128]}"
        failed = _mark_batch_failed(
            store,
            conversions,
            error=error,
            retryable=retryable,
            max_attempts=settings.google_data_manager_max_attempts,
        )
        log.warning(
            "google_data_manager.upload_failed",
            extra={
                "conversion_count": len(conversions),
                "retryable": retryable,
                "marked_failed": failed,
                "error_type": type(exc).__name__,
                "google_request_id": request_id,
            },
        )
        return GoogleDataManagerRunResult(
            len(conversions),
            0,
            failed,
            repaired,
            purged,
            request_id,
        )
    submitted = 0
    for conversion in conversions:
        if not conversion.lease_owner:
            continue
        if store.mark_google_ads_delivery_submitted(
            order_id=conversion.order_id,
            occurred_at=conversion.occurred_at,
            lease_owner=conversion.lease_owner,
            request_id=result.request_id,
        ) is not None:
            submitted += 1
    log.info(
        "google_data_manager.upload_submitted",
        extra={
            "conversion_count": len(conversions),
            "submitted_count": submitted,
            "warning_count": result.warning_count,
            "google_request_id": result.request_id,
        },
    )
    return GoogleDataManagerRunResult(
        len(conversions),
        submitted,
        0,
        repaired,
        purged,
        result.request_id,
    )


def _mark_batch_failed(
    store: GoogleAdsDeliveryStore,
    conversions: list[GoogleAdsConversion],
    *,
    error: str,
    retryable: bool,
    max_attempts: int,
) -> int:
    failed = 0
    for conversion in conversions:
        if not conversion.lease_owner:
            continue
        if store.mark_google_ads_delivery_failed(
            order_id=conversion.order_id,
            occurred_at=conversion.occurred_at,
            lease_owner=conversion.lease_owner,
            error=error,
            retryable=retryable,
            max_attempts=max_attempts,
        ) is not None:
            failed += 1
    return failed


def _destination(reference: str, *, config: GoogleDataManagerConfig) -> dict[str, Any]:
    action_id = {
        "signup": config.signup_action_id,
        "activation": config.activated_action_id,
        "purchase": config.purchase_action_id,
    }[reference]
    destination: dict[str, Any] = {
        "reference": reference,
        "operatingAccount": {
            "accountId": config.account_id,
            "accountType": "GOOGLE_ADS",
        },
        "productDestinationId": action_id,
    }
    if config.login_account_id:
        destination["loginAccount"] = {
            "accountId": config.login_account_id,
            "accountType": "GOOGLE_ADS",
        }
    return destination


def _encode_event(conversion: GoogleAdsConversion, *, click_id: str) -> str:
    if conversion.click_id_kind not in {"gclid", "gbraid", "wbraid"}:
        raise ValueError("unsupported Google click identifier kind")
    event = {
        "adIdentifiers": {conversion.click_id_kind: click_id},
        "currency": conversion.currency_code,
        "destinationReferences": [_destination_reference(conversion)],
        "eventSource": "WEB",
        "eventTimestamp": conversion.occurred_at,
        "transactionId": conversion.order_id,
    }
    encoded = json.dumps(event, separators=(",", ":"), sort_keys=True)
    return (
        encoded[:-1]
        + ',"conversionValue":'
        + _microdollars_json_number(conversion.value_microdollars)
        + "}"
    )


def _destination_reference(conversion: GoogleAdsConversion) -> str:
    try:
        return _DESTINATION_REFERENCE_BY_ACTION[conversion.conversion_action]
    except KeyError as exc:
        raise ValueError(
            f"unsupported direct conversion action: {conversion.conversion_action}"
        ) from exc


def _microdollars_json_number(value: int) -> str:
    if value < 0:
        raise ValueError("Google Ads conversion value cannot be negative")
    whole, fractional = divmod(value, 1_000_000)
    return str(whole) if not fractional else f"{whole}.{fractional:06d}".rstrip("0")


def _numeric_id(value: str | None, label: str) -> str:
    normalized = (value or "").replace("-", "").strip()
    if not _NUMERIC_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must contain only digits")
    return normalized


def _request_statuses(response: httpx.Response) -> tuple[list[str], set[str]]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleDataManagerUploadError(
            "Google Data Manager status response was invalid",
            retryable=True,
        ) from exc
    rows = payload.get("requestStatusPerDestination") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise GoogleDataManagerUploadError(
            "Google Data Manager status response contained no destinations",
            retryable=True,
        )
    statuses: list[str] = []
    reasons: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        statuses.append(str(row.get("requestStatus") or "REQUEST_STATUS_UNKNOWN"))
        error_info = row.get("errorInfo")
        if isinstance(error_info, dict):
            counts = error_info.get("errorCounts")
            if isinstance(counts, list):
                for item in counts:
                    if isinstance(item, dict) and item.get("reason"):
                        reasons.add(str(item["reason"])[:120])
    return statuses, reasons


def _google_error_suffix(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return ""
    error = payload["error"]
    status = error.get("status")
    return f" status={str(status)[:80]}" if status else ""


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {403, 408, 409, 425, 429} or status_code >= 500


__all__ = [
    "GoogleDataManagerClient",
    "GoogleDataManagerConfig",
    "GoogleDataManagerRunResult",
    "GoogleDataManagerUploadError",
    "MetadataAccessTokenProvider",
    "encode_google_data_manager_request",
    "run_google_data_manager_once",
]
