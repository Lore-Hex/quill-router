"""First-party paid-acquisition funnel reporting.

The event stream is emitted by :mod:`trusted_router.acquisition`. This module
only aggregates its metadata-safe fields; it never reads request bodies,
prompts, outputs, account emails, API keys, or payment credentials.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from trusted_router.money import MICRODOLLARS_PER_DOLLAR

if TYPE_CHECKING:
    from trusted_router.google_ads_reporting import GoogleAdsSpendReport

FUNNEL_EVENTS = (
    "acquisition.landing_engaged",
    "acquisition.sign_in_opened",
    "acquisition.signup_completed",
    "acquisition.first_successful_api_call",
    "acquisition.free_credit_exhausted",
    "acquisition.checkout_started",
    "acquisition.payment_method_saved",
    "acquisition.credit_purchase_completed",
    "acquisition.retained_api_usage_7d",
)

_DATASET_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_EVENT_FIELD_BY_NAME = {
    "acquisition.landing_engaged": "engaged_visitors",
    "acquisition.sign_in_opened": "sign_in_visitors",
    "acquisition.signup_completed": "signups",
    "acquisition.first_successful_api_call": "activated_users",
    "acquisition.free_credit_exhausted": "free_credit_exhausted_users",
    "acquisition.checkout_started": "checkout_started_users",
    "acquisition.payment_method_saved": "payment_method_saved_users",
    "acquisition.credit_purchase_completed": "purchasers",
    "acquisition.retained_api_usage_7d": "retained_users_7d",
}
_GOOGLE_CLICK_FIELD_BY_NAME = {
    event: f"google_ads_click_{field}" for event, field in _EVENT_FIELD_BY_NAME.items()
}
_GOOGLE_PERSISTED_FIELD_BY_NAME = {
    event: f"google_ads_persisted_{field}" for event, field in _EVENT_FIELD_BY_NAME.items()
}


@dataclass(frozen=True, order=True)
class FunnelKey:
    source: str
    medium: str
    campaign: str
    creative: str
    landing_path: str


@dataclass
class FunnelRow:
    source: str
    medium: str
    campaign: str
    creative: str
    landing_path: str
    engaged_visitors: int = 0
    sign_in_visitors: int = 0
    signups: int = 0
    activated_users: int = 0
    free_credit_exhausted_users: int = 0
    checkout_started_users: int = 0
    payment_method_saved_users: int = 0
    purchasers: int = 0
    purchase_events: int = 0
    retained_users_7d: int = 0
    revenue_microdollars: int = 0
    google_ads_click_engaged_visitors: int = 0
    google_ads_click_sign_in_visitors: int = 0
    google_ads_click_signups: int = 0
    google_ads_click_activated_users: int = 0
    google_ads_click_free_credit_exhausted_users: int = 0
    google_ads_click_checkout_started_users: int = 0
    google_ads_click_payment_method_saved_users: int = 0
    google_ads_click_purchasers: int = 0
    google_ads_click_retained_users_7d: int = 0
    google_ads_persisted_engaged_visitors: int = 0
    google_ads_persisted_sign_in_visitors: int = 0
    google_ads_persisted_signups: int = 0
    google_ads_persisted_activated_users: int = 0
    google_ads_persisted_free_credit_exhausted_users: int = 0
    google_ads_persisted_checkout_started_users: int = 0
    google_ads_persisted_payment_method_saved_users: int = 0
    google_ads_persisted_purchasers: int = 0
    google_ads_persisted_retained_users_7d: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "medium": self.medium,
            "campaign": self.campaign,
            "creative": self.creative,
            "landing_path": self.landing_path,
            "engaged_visitors": self.engaged_visitors,
            "sign_in_visitors": self.sign_in_visitors,
            "signups": self.signups,
            "activated_users": self.activated_users,
            "free_credit_exhausted_users": self.free_credit_exhausted_users,
            "checkout_started_users": self.checkout_started_users,
            "payment_method_saved_users": self.payment_method_saved_users,
            "purchasers": self.purchasers,
            "purchase_events": self.purchase_events,
            "retained_users_7d": self.retained_users_7d,
            "revenue_microdollars": self.revenue_microdollars,
            "revenue_usd": microdollars_to_usd(self.revenue_microdollars),
            "google_ads_click_engaged_visitors": self.google_ads_click_engaged_visitors,
            "google_ads_click_signups": self.google_ads_click_signups,
            "google_ads_click_activated_users": self.google_ads_click_activated_users,
            "google_ads_click_purchasers": self.google_ads_click_purchasers,
            "google_ads_persisted_signups": self.google_ads_persisted_signups,
            "google_ads_persisted_activated_users": self.google_ads_persisted_activated_users,
            "google_ads_persisted_purchasers": self.google_ads_persisted_purchasers,
            "sign_in_rate": percentage(self.sign_in_visitors, self.engaged_visitors),
            "signup_rate": percentage(self.signups, self.engaged_visitors),
            "activation_rate": percentage(self.activated_users, self.signups),
            "activation_per_engaged_rate": percentage(
                self.activated_users,
                self.engaged_visitors,
            ),
            "checkout_rate": percentage(self.checkout_started_users, self.signups),
            "payment_method_rate": percentage(
                self.payment_method_saved_users,
                self.signups,
            ),
            "purchase_rate": percentage(self.purchasers, self.signups),
            "purchase_per_engaged_rate": percentage(
                self.purchasers,
                self.engaged_visitors,
            ),
            "retention_7d_rate": percentage(self.retained_users_7d, self.signups),
        }


@dataclass(frozen=True)
class AcquisitionMeasurementSummary:
    engaged_visitors: int
    signups: int
    activated_users: int
    checkout_started_users: int
    payment_method_saved_users: int
    purchasers: int
    revenue_microdollars: int
    google_ads_click_engaged_visitors: int
    google_ads_click_signups: int
    google_ads_click_activated_users: int
    google_ads_click_purchasers: int
    google_ads_persisted_signups: int
    google_ads_persisted_activated_users: int
    google_ads_persisted_purchasers: int
    spend_microdollars: int | None
    spend_currency_code: str | None
    ad_impressions: int | None
    ad_clicks: int | None
    blockers: tuple[str, ...]

    @property
    def hold_scale(self) -> bool:
        return bool(self.blockers)

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": "hold" if self.hold_scale else "ready",
            "blockers": list(self.blockers),
            "engaged_visitors": self.engaged_visitors,
            "signups": self.signups,
            "activated_users": self.activated_users,
            "checkout_started_users": self.checkout_started_users,
            "payment_method_saved_users": self.payment_method_saved_users,
            "purchasers": self.purchasers,
            "revenue_microdollars": self.revenue_microdollars,
            "google_ads_click_engaged_visitors": self.google_ads_click_engaged_visitors,
            "google_ads_click_signups": self.google_ads_click_signups,
            "google_ads_click_activated_users": self.google_ads_click_activated_users,
            "google_ads_click_purchasers": self.google_ads_click_purchasers,
            "google_ads_persisted_signups": self.google_ads_persisted_signups,
            "google_ads_persisted_activated_users": self.google_ads_persisted_activated_users,
            "google_ads_persisted_purchasers": self.google_ads_persisted_purchasers,
            "spend_microdollars": self.spend_microdollars,
            "spend_currency_code": self.spend_currency_code,
            "ad_impressions": self.ad_impressions,
            "ad_clicks": self.ad_clicks,
            "signup_cac_microdollars": _cost_per_outcome(
                self.spend_microdollars,
                self.signups,
            ),
            "activation_cac_microdollars": _cost_per_outcome(
                self.spend_microdollars,
                self.activated_users,
            ),
            "purchase_cac_microdollars": _cost_per_outcome(
                self.spend_microdollars,
                self.purchasers,
            ),
            "roas": _ratio(self.revenue_microdollars, self.spend_microdollars),
        }


def build_axiom_funnel_query(dataset: str) -> str:
    """Return the stable APL contract consumed by the operator report."""
    if not _DATASET_RE.fullmatch(dataset):
        raise ValueError("Axiom dataset contains unsupported characters")
    event_values = ", ".join(f"'{event}'" for event in FUNNEL_EVENTS)
    return (
        f"['{dataset}'] "
        f"| where event in ({event_values}) "
        "| summarize people=dcount(anonymous_fingerprint), "
        "google_ads_click_people=dcountif(anonymous_fingerprint, "
        "has_gclid == true or has_gbraid == true or has_wbraid == true), "
        "google_ads_persisted_people=dcountif(anonymous_fingerprint, "
        "column_ifexists('google_ads_click_persisted', false) == true), "
        "events=count(), revenue_microdollars=sum(amount_microdollars) "
        "by event, utm_source, utm_medium, utm_campaign, utm_content, landing_path "
        "| sort by utm_source asc, utm_campaign asc, utm_content asc, "
        "landing_path asc, event asc"
    )


def parse_axiom_json_lines(payload: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Axiom JSON on line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Axiom row {line_number} is not an object")
        rows.append(value)
    return rows


def aggregate_funnel_rows(
    records: Iterable[dict[str, object]],
    *,
    source: str | None = None,
    campaign: str | None = None,
    creative: str | None = None,
    landing_path: str | None = None,
) -> list[FunnelRow]:
    rows: dict[FunnelKey, FunnelRow] = {}
    seen_event_rows: set[tuple[FunnelKey, str]] = set()
    for record in records:
        event = _text(record.get("event"))
        field = _EVENT_FIELD_BY_NAME.get(event)
        if field is None:
            continue
        key = FunnelKey(
            source=_dimension(record.get("utm_source"), "(direct)"),
            medium=_dimension(record.get("utm_medium"), "(none)"),
            campaign=_dimension(record.get("utm_campaign"), "(none)"),
            creative=_dimension(record.get("utm_content"), "(none)"),
            landing_path=_dimension(record.get("landing_path"), "(unknown)"),
        )
        if source is not None and key.source != source:
            continue
        if campaign is not None and key.campaign != campaign:
            continue
        if creative is not None and key.creative != creative:
            continue
        if landing_path is not None and key.landing_path != landing_path:
            continue
        event_key = (key, event)
        if event_key in seen_event_rows:
            raise ValueError(f"Duplicate Axiom summary row for {key!r} and {event}")
        seen_event_rows.add(event_key)
        row = rows.setdefault(
            key,
            FunnelRow(
                source=key.source,
                medium=key.medium,
                campaign=key.campaign,
                creative=key.creative,
                landing_path=key.landing_path,
            ),
        )
        people = _nonnegative_int(record.get("people"))
        setattr(row, field, people)
        google_click_field = _GOOGLE_CLICK_FIELD_BY_NAME[event]
        setattr(
            row,
            google_click_field,
            _nonnegative_int(record.get("google_ads_click_people")),
        )
        google_persisted_field = _GOOGLE_PERSISTED_FIELD_BY_NAME[event]
        setattr(
            row,
            google_persisted_field,
            _nonnegative_int(record.get("google_ads_persisted_people")),
        )
        if event == "acquisition.credit_purchase_completed":
            row.purchase_events = _nonnegative_int(record.get("events"))
            row.revenue_microdollars = _nonnegative_int(record.get("revenue_microdollars"))
    return sorted(
        rows.values(),
        key=lambda row: (
            -row.activated_users,
            -row.purchasers,
            -row.signups,
            -row.engaged_visitors,
            row.source,
            row.campaign,
            row.creative,
            row.landing_path,
        ),
    )


def summarize_measurement(
    rows: Iterable[FunnelRow],
    *,
    source: str | None,
    spend: GoogleAdsSpendReport | None,
    spend_error: str | None = None,
) -> AcquisitionMeasurementSummary:
    materialized = tuple(rows)
    totals = {
        field: sum(getattr(row, field) for row in materialized)
        for field in (
            "engaged_visitors",
            "signups",
            "activated_users",
            "checkout_started_users",
            "payment_method_saved_users",
            "purchasers",
            "revenue_microdollars",
            "google_ads_click_engaged_visitors",
            "google_ads_click_signups",
            "google_ads_click_activated_users",
            "google_ads_click_purchasers",
            "google_ads_persisted_signups",
            "google_ads_persisted_activated_users",
            "google_ads_persisted_purchasers",
        )
    }
    blockers: list[str] = []
    is_google = (source or "").casefold() == "google"
    if is_google and totals["engaged_visitors"] > 0:
        if totals["google_ads_click_engaged_visitors"] == 0:
            blockers.append("google_click_ids_missing")
        if totals["google_ads_click_signups"] > totals["google_ads_persisted_signups"]:
            blockers.append("google_click_ids_not_persisted")
        if spend is None:
            blockers.append(spend_error or "native_spend_unavailable")
        elif spend.currency_code != "USD":
            blockers.append("spend_currency_not_usd")
        elif spend.spend_microdollars > 0 and totals["purchasers"] == 0:
            blockers.append("no_purchases_in_window")
    return AcquisitionMeasurementSummary(
        **totals,
        spend_microdollars=spend.spend_microdollars if spend else None,
        spend_currency_code=spend.currency_code if spend else None,
        ad_impressions=spend.impressions if spend else None,
        ad_clicks=spend.clicks if spend else None,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def render_measurement_markdown(summary: AcquisitionMeasurementSummary) -> str:
    decision = "HOLD" if summary.hold_scale else "READY"
    spend = (
        f"${microdollars_to_usd(summary.spend_microdollars)}"
        if summary.spend_microdollars is not None
        else "unavailable"
    )
    blockers = ", ".join(summary.blockers) if summary.blockers else "none"
    return (
        f"**Scale decision:** {decision}\n\n"
        f"**Funnel:** {summary.engaged_visitors} engaged -> {summary.signups} signup -> "
        f"{summary.activated_users} first call -> {summary.checkout_started_users} checkout "
        f"-> {summary.payment_method_saved_users} saved payment method -> "
        f"{summary.purchasers} purchase\n\n"
        f"**Google click evidence:** {summary.google_ads_click_engaged_visitors} of "
        f"{summary.engaged_visitors} engaged visitors; "
        f"{summary.google_ads_persisted_signups} of {summary.google_ads_click_signups} "
        f"click-backed signups persisted | **Spend:** {spend} | "
        f"**Blockers:** {blockers}\n\n"
    )


def render_markdown(rows: Iterable[FunnelRow]) -> str:
    header = (
        "| Source | Campaign | Creative | Landing | Engaged | Sign in | Signups | "
        "Activated | Free used | Checkout | Card saved | Buyers | Revenue | "
        "Signup / engaged | Activated / engaged | Buyers / engaged |\n"
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    body = [
        "| "
        + " | ".join(
            (
                _markdown_cell(row.source),
                _markdown_cell(row.campaign),
                _markdown_cell(row.creative),
                _markdown_cell(row.landing_path),
                str(row.engaged_visitors),
                str(row.sign_in_visitors),
                str(row.signups),
                str(row.activated_users),
                str(row.free_credit_exhausted_users),
                str(row.checkout_started_users),
                str(row.payment_method_saved_users),
                str(row.purchasers),
                f"${microdollars_to_usd(row.revenue_microdollars)}",
                percentage(row.signups, row.engaged_visitors),
                percentage(row.activated_users, row.engaged_visitors),
                percentage(row.purchasers, row.engaged_visitors),
            )
        )
        + " |"
        for row in rows
    ]
    return "\n".join((header, *body)) + "\n"


def percentage(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "n/a"
    value = Decimal(numerator) * Decimal(100) / Decimal(denominator)
    return f"{value.quantize(Decimal('0.1'))}%"


def microdollars_to_usd(value: int) -> str:
    amount = Decimal(value) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"{amount.quantize(Decimal('0.000001'))}"


def _cost_per_outcome(spend_microdollars: int | None, outcomes: int) -> int | None:
    if spend_microdollars is None or outcomes <= 0:
        return None
    return (spend_microdollars + outcomes // 2) // outcomes


def _ratio(numerator: int, denominator: int | None) -> str | None:
    if denominator is None or denominator <= 0:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001")))


def _dimension(value: object, fallback: str) -> str:
    text = _text(value)
    return text if text else fallback


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _nonnegative_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid funnel count")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    else:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid funnel count: {value!r}") from exc
    if parsed < 0:
        raise ValueError("Funnel counts cannot be negative")
    return parsed


def _markdown_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")
