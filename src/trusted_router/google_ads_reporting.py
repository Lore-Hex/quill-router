"""Aggregate, metadata-only Google Ads spend reporting."""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from google.auth import default as google_auth_default
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

GOOGLE_ADS_API_VERSION = "v25"
GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
GOOGLE_ADS_API_ROOT = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"

_NUMERIC_ID_RE = re.compile(r"^[0-9]+$")
_DEVELOPER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class GoogleAdsReportingError(RuntimeError):
    """A safe operator-facing Google Ads reporting failure."""


@dataclass(frozen=True)
class GoogleAdsReportingConfig:
    customer_id: str
    developer_token: str
    login_customer_id: str | None = None
    time_zone: str = "America/Los_Angeles"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> GoogleAdsReportingConfig:
        values = os.environ if environ is None else environ
        customer_id = _numeric_id(
            values.get("TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID")
            or values.get("TR_GOOGLE_DATA_MANAGER_ACCOUNT_ID"),
            "Google Ads reporting customer ID",
        )
        login_customer_id = values.get("TR_GOOGLE_ADS_REPORTING_LOGIN_CUSTOMER_ID") or values.get(
            "TR_GOOGLE_DATA_MANAGER_LOGIN_ACCOUNT_ID"
        )
        developer_token = str(values.get("TR_GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip()
        if not _DEVELOPER_TOKEN_RE.fullmatch(developer_token):
            raise ValueError("TR_GOOGLE_ADS_DEVELOPER_TOKEN is missing or invalid")
        time_zone = str(
            values.get("TR_GOOGLE_ADS_REPORTING_TIME_ZONE") or "America/Los_Angeles"
        ).strip()
        try:
            ZoneInfo(time_zone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("TR_GOOGLE_ADS_REPORTING_TIME_ZONE is invalid") from exc
        return cls(
            customer_id=customer_id,
            developer_token=developer_token,
            login_customer_id=(
                _numeric_id(login_customer_id, "Google Ads reporting login customer ID")
                if login_customer_id
                else None
            ),
            time_zone=time_zone,
        )


@dataclass(frozen=True, order=True)
class GoogleAdsSpendRow:
    date: str
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    spend_microdollars: int

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "spend_microdollars": self.spend_microdollars,
        }


@dataclass(frozen=True)
class GoogleAdsSpendReport:
    customer_id: str
    currency_code: str
    time_zone: str
    start_date: str
    end_date: str
    rows: tuple[GoogleAdsSpendRow, ...]

    @property
    def impressions(self) -> int:
        return sum(row.impressions for row in self.rows)

    @property
    def clicks(self) -> int:
        return sum(row.clicks for row in self.rows)

    @property
    def spend_microdollars(self) -> int:
        return sum(row.spend_microdollars for row in self.rows)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "available",
            "customer_id": self.customer_id,
            "currency_code": self.currency_code,
            "time_zone": self.time_zone,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "spend_microdollars": self.spend_microdollars,
            "rows": [row.as_dict() for row in self.rows],
        }


class GoogleAdsAccessTokenProvider:
    """Use ADC locally and the Cloud Run service identity when deployed."""

    def __init__(
        self,
        credential_factory: Callable[..., tuple[Credentials, str | None]] = google_auth_default,
    ) -> None:
        self._credential_factory = credential_factory
        self._credentials: Credentials | None = None

    def __call__(self) -> str:
        if self._credentials is None:
            self._credentials, _ = self._credential_factory(scopes=[GOOGLE_ADS_SCOPE])
        if not self._credentials.valid or not self._credentials.token:
            self._credentials.refresh(GoogleAuthRequest())
        token = str(self._credentials.token or "").strip()
        if not token:
            raise GoogleAdsReportingError("Google Ads OAuth returned an empty access token")
        return token


class GoogleAdsReportingClient:
    def __init__(
        self,
        *,
        config: GoogleAdsReportingConfig,
        client: httpx.Client,
        token_provider: Callable[[], str],
    ) -> None:
        self._config = config
        self._client = client
        self._token_provider = token_provider

    def fetch_spend(
        self,
        *,
        start_date: dt.date,
        end_date: dt.date,
    ) -> GoogleAdsSpendReport:
        query = build_google_ads_spend_query(start_date, end_date)
        url = f"{GOOGLE_ADS_API_ROOT}/customers/{self._config.customer_id}/googleAds:searchStream"
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            "developer-token": self._config.developer_token,
            "User-Agent": "TrustedRouter-MarketingReport/1",
        }
        if self._config.login_customer_id:
            headers["login-customer-id"] = self._config.login_customer_id
        try:
            response = self._client.post(url, headers=headers, json={"query": query})
        except httpx.HTTPError as exc:
            raise GoogleAdsReportingError("Google Ads reporting request failed") from exc
        if not 200 <= response.status_code < 300:
            raise GoogleAdsReportingError(
                f"Google Ads reporting returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleAdsReportingError("Google Ads reporting returned invalid JSON") from exc
        rows, currency_code, response_time_zone = parse_google_ads_search_stream(payload)
        if response_time_zone and response_time_zone != self._config.time_zone:
            raise GoogleAdsReportingError(
                "Google Ads account time zone does not match TR_GOOGLE_ADS_REPORTING_TIME_ZONE"
            )
        return GoogleAdsSpendReport(
            customer_id=self._config.customer_id,
            currency_code=currency_code or "USD",
            time_zone=response_time_zone or self._config.time_zone,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            rows=tuple(rows),
        )


def google_ads_reporting_window(
    *,
    days: int,
    time_zone: str,
    now: dt.datetime | None = None,
) -> tuple[dt.date, dt.date, dt.datetime]:
    if days < 1:
        raise ValueError("days must be positive")
    zone = ZoneInfo(time_zone)
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = current.astimezone(zone)
    end_date = local_now.date()
    start_date = end_date - dt.timedelta(days=days - 1)
    start_at = dt.datetime.combine(start_date, dt.time.min, tzinfo=zone).astimezone(dt.UTC)
    return start_date, end_date, start_at


def build_google_ads_spend_query(start_date: dt.date, end_date: dt.date) -> str:
    if end_date < start_date:
        raise ValueError("Google Ads spend end date precedes start date")
    return (
        "SELECT customer.currency_code, customer.time_zone, campaign.id, campaign.name, "  # noqa: S608 - interpolation is limited to validated date objects.
        "segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}' "
        "AND campaign.status != 'REMOVED' "
        "ORDER BY segments.date ASC, campaign.id ASC"
    )


def parse_google_ads_search_stream(
    payload: object,
) -> tuple[list[GoogleAdsSpendRow], str, str]:
    if not isinstance(payload, list):
        raise GoogleAdsReportingError("Google Ads reporting response was not a list")
    rows: list[GoogleAdsSpendRow] = []
    seen: set[tuple[str, str]] = set()
    currency_codes: set[str] = set()
    time_zones: set[str] = set()
    for chunk in payload:
        if not isinstance(chunk, dict):
            raise GoogleAdsReportingError("Google Ads reporting chunk was invalid")
        results = chunk.get("results", [])
        if not isinstance(results, list):
            raise GoogleAdsReportingError("Google Ads reporting results were invalid")
        for raw in results:
            if not isinstance(raw, dict):
                raise GoogleAdsReportingError("Google Ads reporting row was invalid")
            customer = _object(raw.get("customer"), "customer")
            campaign = _object(raw.get("campaign"), "campaign")
            segments = _object(raw.get("segments"), "segments")
            metrics = _object(raw.get("metrics"), "metrics")
            currency_code = _short_text(customer.get("currencyCode"), "currency code", 8)
            time_zone = _short_text(customer.get("timeZone"), "time zone", 64)
            date = _iso_date(segments.get("date"))
            campaign_id = _numeric_text(campaign.get("id"), "campaign ID")
            key = (date, campaign_id)
            if key in seen:
                raise GoogleAdsReportingError("Google Ads reporting returned a duplicate row")
            seen.add(key)
            currency_codes.add(currency_code)
            time_zones.add(time_zone)
            rows.append(
                GoogleAdsSpendRow(
                    date=date,
                    campaign_id=campaign_id,
                    campaign_name=_short_text(campaign.get("name"), "campaign name", 256),
                    impressions=_nonnegative_int(metrics.get("impressions"), "impressions"),
                    clicks=_nonnegative_int(metrics.get("clicks"), "clicks"),
                    spend_microdollars=_nonnegative_int(
                        metrics.get("costMicros"),
                        "cost micros",
                    ),
                )
            )
    if len(currency_codes) > 1 or len(time_zones) > 1:
        raise GoogleAdsReportingError("Google Ads reporting mixed account metadata")
    return sorted(rows), next(iter(currency_codes), ""), next(iter(time_zones), "")


def _numeric_id(value: str | None, label: str) -> str:
    normalized = str(value or "").replace("-", "").strip()
    if not _NUMERIC_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must contain only digits")
    return normalized


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoogleAdsReportingError(f"Google Ads {label} was missing")
    return value


def _short_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise GoogleAdsReportingError(f"Google Ads {label} was invalid")
    return value.strip()


def _numeric_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _NUMERIC_ID_RE.fullmatch(text):
        raise GoogleAdsReportingError(f"Google Ads {label} was invalid")
    return text


def _iso_date(value: object) -> str:
    text = _short_text(value, "date", 10)
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise GoogleAdsReportingError("Google Ads date was invalid") from exc


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise GoogleAdsReportingError(f"Google Ads {label} was invalid")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise GoogleAdsReportingError(f"Google Ads {label} was invalid") from exc
    if parsed < 0:
        raise GoogleAdsReportingError(f"Google Ads {label} was negative")
    return parsed


__all__ = [
    "GoogleAdsAccessTokenProvider",
    "GoogleAdsReportingClient",
    "GoogleAdsReportingConfig",
    "GoogleAdsReportingError",
    "GoogleAdsSpendReport",
    "GoogleAdsSpendRow",
    "build_google_ads_spend_query",
    "google_ads_reporting_window",
    "parse_google_ads_search_stream",
]
