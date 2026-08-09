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

from trusted_router.money import MICRODOLLARS_PER_DOLLAR

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


@dataclass(frozen=True, order=True)
class FunnelKey:
    source: str
    medium: str
    campaign: str
    creative: str


@dataclass
class FunnelRow:
    source: str
    medium: str
    campaign: str
    creative: str
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

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "medium": self.medium,
            "campaign": self.campaign,
            "creative": self.creative,
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
            "sign_in_rate": percentage(self.sign_in_visitors, self.engaged_visitors),
            "signup_rate": percentage(self.signups, self.engaged_visitors),
            "activation_rate": percentage(self.activated_users, self.signups),
            "checkout_rate": percentage(self.checkout_started_users, self.signups),
            "payment_method_rate": percentage(
                self.payment_method_saved_users,
                self.signups,
            ),
            "purchase_rate": percentage(self.purchasers, self.signups),
            "retention_7d_rate": percentage(self.retained_users_7d, self.signups),
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
        "events=count(), revenue_microdollars=sum(amount_microdollars) "
        "by event, utm_source, utm_medium, utm_campaign, utm_content "
        "| sort by utm_source asc, utm_campaign asc, utm_content asc, event asc"
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
        )
        if source is not None and key.source != source:
            continue
        if campaign is not None and key.campaign != campaign:
            continue
        if creative is not None and key.creative != creative:
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
            ),
        )
        people = _nonnegative_int(record.get("people"))
        setattr(row, field, people)
        if event == "acquisition.credit_purchase_completed":
            row.purchase_events = _nonnegative_int(record.get("events"))
            row.revenue_microdollars = _nonnegative_int(
                record.get("revenue_microdollars")
            )
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
        ),
    )


def render_markdown(rows: Iterable[FunnelRow]) -> str:
    header = (
        "| Source | Campaign | Creative | Engaged | Sign in | Signups | "
        "Activated | Free used | Checkout | Card saved | Buyers | Revenue | "
        "Signup / engaged | Activated / signup |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    body = [
        "| "
        + " | ".join(
            (
                _markdown_cell(row.source),
                _markdown_cell(row.campaign),
                _markdown_cell(row.creative),
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
                percentage(row.activated_users, row.signups),
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
