"""Privacy-bounded Google Ads conversion records and CSV formatting.

Google Ads receives only its own click identifier plus an event name, time,
integer-derived value, currency, and an opaque order ID. Prompt/output data,
emails, API keys, workspace IDs, and request bodies never enter this module.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from decimal import Decimal

from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage_models import AcquisitionAttribution, GoogleAdsConversion

GOOGLE_ADS_SIGNUP_ACTION = "TrustedRouter Signup"
GOOGLE_ADS_ACTIVATED_ACTION = "TrustedRouter Activated API User"
GOOGLE_ADS_RETAINED_ACTION = "TrustedRouter Retained API User 7d"
GOOGLE_ADS_PURCHASE_ACTION = "TrustedRouter Credit Purchase"

GOOGLE_ADS_ACTION_BY_EVENT = {
    "signup_completed": GOOGLE_ADS_SIGNUP_ACTION,
    "first_successful_api_call": GOOGLE_ADS_ACTIVATED_ACTION,
    "retained_api_usage_7d": GOOGLE_ADS_RETAINED_ACTION,
    "credit_purchase_completed": GOOGLE_ADS_PURCHASE_ACTION,
}

GOOGLE_ADS_DIRECT_DELIVERY_ACTIONS = frozenset(
    {
        GOOGLE_ADS_SIGNUP_ACTION,
        GOOGLE_ADS_PURCHASE_ACTION,
    }
)
LEGACY_GOOGLE_DATA_MANAGER_403_ERROR = (
    "Google Data Manager returned HTTP 403"
)

GOOGLE_ADS_CSV_COLUMNS = (
    "conversion_action",
    "gclid",
    "gbraid",
    "wbraid",
    "conversion_datetime",
    "conversion_value",
    "currency_code",
    "order_id",
)

_CLICK_ID_FIELDS = ("gclid", "gbraid", "wbraid")
_ENTITY_KIND_PREFIX = "google_ads_conversion_"


def build_google_ads_conversion(
    record: AcquisitionAttribution,
    event: str,
    *,
    occurred_at: str,
    value_microdollars: int = 0,
    ordinal: int = 0,
) -> GoogleAdsConversion | None:
    """Create a deterministic conversion for a Google-attributed account."""
    action = GOOGLE_ADS_ACTION_BY_EVENT.get(event)
    if action is None:
        return None
    touch = _google_touch(record)
    if touch is None:
        return None
    if value_microdollars < 0:
        raise ValueError("Google Ads conversion value cannot be negative")
    seed = "\0".join(
        (
            record.anonymous_id,
            event,
            occurred_at,
            str(ordinal),
        )
    )
    order_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    conversion = GoogleAdsConversion(
        order_id=order_id,
        conversion_action=action,
        occurred_at=occurred_at,
        gclid=touch.get("gclid"),
        gbraid=touch.get("gbraid"),
        wbraid=touch.get("wbraid"),
        value_microdollars=value_microdollars,
    )
    if is_google_ads_direct_delivery(conversion):
        conversion.delivery_status = "pending"
        conversion.next_attempt_at = conversion.created_at
    return conversion


def is_google_ads_direct_delivery(conversion: GoogleAdsConversion) -> bool:
    return conversion.conversion_action in GOOGLE_ADS_DIRECT_DELIVERY_ACTIONS


def should_repair_legacy_google_data_manager_403(
    conversion: GoogleAdsConversion,
) -> bool:
    """Repair only rows dead-lettered by the first API-propagation smoke."""
    return (
        is_google_ads_direct_delivery(conversion)
        and conversion.delivery_status == "dead"
        and conversion.delivery_attempts == 1
        and conversion.last_error == LEGACY_GOOGLE_DATA_MANAGER_403_ERROR
    )


def google_ads_conversion_kind(occurred_at: str) -> str:
    timestamp = parse_utc_timestamp(occurred_at)
    return f"{_ENTITY_KIND_PREFIX}{timestamp:%Y%m}"


def google_ads_conversion_entity_id(conversion: GoogleAdsConversion) -> str:
    timestamp = parse_utc_timestamp(conversion.occurred_at)
    return f"{timestamp:%Y%m%dT%H%M%SZ}#{conversion.order_id}"


def google_ads_conversion_kinds_since(
    since: dt.datetime,
    *,
    now: dt.datetime | None = None,
) -> list[str]:
    """Return month-partitioned entity kinds covering ``since`` through now."""
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
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return kinds


def google_ads_conversions_csv(conversions: list[GoogleAdsConversion]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=GOOGLE_ADS_CSV_COLUMNS,
        lineterminator="\n",
    )
    writer.writeheader()
    for conversion in conversions:
        writer.writerow(
            {
                "conversion_action": conversion.conversion_action,
                "gclid": conversion.gclid or "",
                "gbraid": conversion.gbraid or "",
                "wbraid": conversion.wbraid or "",
                "conversion_datetime": conversion.occurred_at,
                "conversion_value": _microdollars_decimal(conversion.value_microdollars),
                "currency_code": conversion.currency_code,
                "order_id": conversion.order_id,
            }
        )
    return output.getvalue()


def parse_utc_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _google_touch(record: AcquisitionAttribution) -> dict[str, str] | None:
    for touch in (record.last_touch, record.first_touch):
        if any(touch.get(field) for field in _CLICK_ID_FIELDS):
            return touch
    return None


def _microdollars_decimal(value: int) -> str:
    amount = Decimal(value) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"{amount:.6f}"
