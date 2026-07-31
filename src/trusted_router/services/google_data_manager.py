"""Direct, metadata-only Google Ads Data Manager conversion delivery.

This module deliberately uses raw HTTPS instead of a Google SDK. Uploads carry
only Google's click identifier, event time, integer-derived dollar value,
currency, and TrustedRouter's opaque transaction ID. They never carry email,
workspace, account, prompt, output, API-key, or inference data.
"""

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
    is_google_ads_direct_delivery,
)
from trusted_router.storage_models import GoogleAdsConversion

log = logging.getLogger(__name__)

DATA_MANAGER_INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"
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
                "activated conversion action ID",
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
    request_id: str | None = None


class GoogleDataManagerUploadError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class GoogleAdsDeliveryStore(Protocol):
    """Narrow storage surface available to the conversion worker."""

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
    """Fetch and cache a scoped Cloud Run service-identity access token."""

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
                params={
                    "scopes": DATA_MANAGER_SCOPE,
                    "enforce_scopes": "true",
                },
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
        client: httpx.Client,
        token_provider: Callable[[], str],
        ingest_url: str = DATA_MANAGER_INGEST_URL,
    ) -> None:
        self._config = config
        self._client = client
        self._token_provider = token_provider
        self._ingest_url = ingest_url

    def ingest(
        self,
        conversions: list[GoogleAdsConversion],
    ) -> GoogleDataManagerIngestResult:
        if not conversions:
            raise ValueError("at least one Google Ads conversion is required")
        if len(conversions) > MAX_EVENTS_PER_REQUEST:
            raise ValueError(
                f"Google Data Manager accepts at most {MAX_EVENTS_PER_REQUEST} events"
            )
        body = encode_google_data_manager_request(
            conversions,
            config=self._config,
        )
        try:
            response = self._client.post(
                self._ingest_url,
                content=body,
                headers={
                    "Authorization": f"Bearer {self._token_provider()}",
                    "Content-Type": "application/json",
                    "User-Agent": "TrustedRouter-DataManager/1",
                    "x-goog-user-project": self._config.project_id,
                },
            )
        except GoogleDataManagerUploadError:
            raise
        except httpx.HTTPError as exc:
            raise GoogleDataManagerUploadError(
                "Google Data Manager request failed",
                retryable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            detail = _google_error_detail(response)
            suffix = f" ({detail})" if detail else ""
            raise GoogleDataManagerUploadError(
                f"Google Data Manager returned HTTP {response.status_code}{suffix}",
                retryable=_is_retryable_status(response.status_code),
            )
        try:
            payload = response.json()
            request_id = str(payload["requestId"]).strip()
            warnings = payload.get("fieldWarnings", [])
            warning_count = len(warnings) if isinstance(warnings, list) else 0
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
        return GoogleDataManagerIngestResult(
            request_id=request_id,
            warning_count=warning_count,
        )


def encode_google_data_manager_request(
    conversions: list[GoogleAdsConversion],
    *,
    config: GoogleDataManagerConfig,
) -> bytes:
    """Encode exact microdollar values as JSON numbers without binary floats."""
    if not conversions:
        raise ValueError("at least one Google Ads conversion is required")
    if len(conversions) > MAX_EVENTS_PER_REQUEST:
        raise ValueError(
            f"Google Data Manager accepts at most {MAX_EVENTS_PER_REQUEST} events"
        )
    for conversion in conversions:
        if not is_google_ads_direct_delivery(conversion):
            raise ValueError(
                f"conversion action is not eligible for direct delivery: "
                f"{conversion.conversion_action}"
            )

    references = {
        _destination_reference(conversion)
        for conversion in conversions
    }
    destinations = [
        _destination(reference, config=config)
        for reference in ("signup", "activation", "purchase")
        if reference in references
    ]
    event_json = [
        _encode_event(conversion)
        for conversion in conversions
    ]
    destination_json = json.dumps(
        destinations,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        '{"destinations":'
        + destination_json
        + ',"events":['
        + ",".join(event_json)
        + "]}"
    ).encode("utf-8")


def run_google_data_manager_once(
    *,
    store: GoogleAdsDeliveryStore,
    settings: Settings,
    client: GoogleDataManagerClient,
) -> GoogleDataManagerRunResult:
    if not settings.google_data_manager_enabled:
        return GoogleDataManagerRunResult(
            claimed=0,
            submitted=0,
            failed=0,
            repaired=0,
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
        return GoogleDataManagerRunResult(
            claimed=0,
            submitted=0,
            failed=0,
            repaired=repaired,
        )

    try:
        result = client.ingest(conversions)
    except GoogleDataManagerUploadError as exc:
        failed = _mark_batch_failed(
            store,
            conversions,
            error=str(exc),
            retryable=exc.retryable,
            max_attempts=settings.google_data_manager_max_attempts,
        )
        log.warning(
            "google_data_manager.upload_failed error=%s "
            "conversion_count=%d retryable=%s marked_failed=%d",
            exc,
            len(conversions),
            exc.retryable,
            failed,
        )
        return GoogleDataManagerRunResult(
            claimed=len(conversions),
            submitted=0,
            failed=failed,
            repaired=repaired,
        )

    submitted = 0
    for conversion in conversions:
        lease_owner = conversion.lease_owner
        if not lease_owner:
            continue
        marked = store.mark_google_ads_delivery_submitted(
            order_id=conversion.order_id,
            occurred_at=conversion.occurred_at,
            lease_owner=lease_owner,
            request_id=result.request_id,
        )
        if marked is not None:
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
        claimed=len(conversions),
        submitted=submitted,
        failed=0,
        repaired=repaired,
        request_id=result.request_id,
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
        lease_owner = conversion.lease_owner
        if not lease_owner:
            continue
        marked = store.mark_google_ads_delivery_failed(
            order_id=conversion.order_id,
            occurred_at=conversion.occurred_at,
            lease_owner=lease_owner,
            error=error,
            retryable=retryable,
            max_attempts=max_attempts,
        )
        if marked is not None:
            failed += 1
    return failed


def _destination(
    reference: str,
    *,
    config: GoogleDataManagerConfig,
) -> dict[str, Any]:
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


def _encode_event(conversion: GoogleAdsConversion) -> str:
    click_name, click_value = _one_click_identifier(conversion)
    event = {
        "adIdentifiers": {click_name: click_value},
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


def _one_click_identifier(
    conversion: GoogleAdsConversion,
) -> tuple[str, str]:
    for name in ("gclid", "gbraid", "wbraid"):
        value = getattr(conversion, name)
        if value:
            return name, value
    raise ValueError("Google Ads conversion has no click identifier")


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
    if not fractional:
        return str(whole)
    return f"{whole}.{fractional:06d}".rstrip("0")


def _numeric_id(value: str | None, label: str) -> str:
    normalized = (value or "").replace("-", "").strip()
    if not _NUMERIC_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must contain only digits")
    return normalized


def _google_error_detail(response: httpx.Response) -> str:
    """Extract safe diagnostic codes without logging Google's raw response."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""

    details: list[str] = []
    status = error.get("status")
    if isinstance(status, str) and status:
        details.append(f"status={status[:80]}")

    raw_details = error.get("details")
    if isinstance(raw_details, list):
        for item in raw_details:
            if not isinstance(item, dict):
                continue
            detail_type = str(item.get("@type", ""))
            if detail_type.endswith("ErrorInfo"):
                reason = item.get("reason")
                if isinstance(reason, str) and reason:
                    details.append(f"reason={reason[:80]}")
            elif detail_type.endswith("RequestInfo"):
                request_id = item.get("requestId")
                if isinstance(request_id, str) and request_id:
                    details.append(f"request_id={request_id[:128]}")
    return " ".join(details)


def _is_retryable_status(status_code: int) -> bool:
    # Google documents propagation-time SERVICE_DISABLED as a 403. Retry is
    # still bounded by google_data_manager_max_attempts.
    return status_code in {403, 408, 409, 425, 429} or status_code >= 500
