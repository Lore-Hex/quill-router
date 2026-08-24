from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    dollars_to_microdollars,
    microdollars_to_decimal,
)
from trusted_router.storage_group_buy import bedrock_group_buy_shard
from trusted_router.storage_models import (
    BedrockGroupBuyAggregate,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
)

BEDROCK_GROUP_BUY_GOAL_MICRODOLLARS = 1_000_000 * MICRODOLLARS_PER_DOLLAR
BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS = 400_000 * MICRODOLLARS_PER_DOLLAR
BEDROCK_GROUP_BUY_FOUNDING_BUYERS = 10
BEDROCK_GROUP_BUY_SAVINGS_BASIS_POINTS = 1_000
BEDROCK_GROUP_BUY_TERM_MONTHS = 12
BEDROCK_GROUP_BUY_MAX_MONTHLY_MICRODOLLARS = 100_000_000 * MICRODOLLARS_PER_DOLLAR
BEDROCK_GROUP_BUY_MAX_TOTAL_LLM_MICRODOLLARS = 1_000_000_000 * MICRODOLLARS_PER_DOLLAR
BEDROCK_GROUP_BUY_PUBLIC_MESSAGE_LIMIT = 50
BEDROCK_GROUP_BUY_PUBLIC_BUCKET_MICRODOLLARS = 25_000 * MICRODOLLARS_PER_DOLLAR
BEDROCK_GROUP_BUY_SPEND_SOURCES = (
    ("bedrock", "Amazon Bedrock"),
    ("anthropic_direct", "Anthropic direct"),
    ("openai_direct", "OpenAI direct"),
    ("google", "Google Cloud or AI Studio"),
    ("azure", "Microsoft Azure"),
    ("another_router", "Another model router"),
    ("other_provider", "Other direct providers"),
)
_BEDROCK_GROUP_BUY_SPEND_SOURCE_IDS = frozenset(
    source_id for source_id, _ in BEDROCK_GROUP_BUY_SPEND_SOURCES
)

_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", re.IGNORECASE)
_URL_RE = re.compile(
    r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|ai|co|dev|app)\b)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?<!\w)@[a-z0-9_]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?\d[\s().-]*){7,}")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class BedrockGroupBuyPublicSnapshot:
    buyer_count: int
    monthly_minimum_microdollars: int
    expected_bedrock_monthly_microdollars: int
    expected_all_llm_monthly_microdollars: int
    annual_minimum_microdollars: int
    annual_savings_microdollars: int
    goal_microdollars: int
    goal_remaining_microdollars: int
    progress_basis_points: int
    messages: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "buyer_count": self.buyer_count,
            "monthly_minimum_microdollars": self.monthly_minimum_microdollars,
            "monthly_minimum_usd": microdollars_to_decimal(self.monthly_minimum_microdollars),
            "expected_bedrock_monthly_microdollars": (self.expected_bedrock_monthly_microdollars),
            "expected_bedrock_monthly_usd": microdollars_to_decimal(
                self.expected_bedrock_monthly_microdollars
            ),
            "expected_all_llm_monthly_microdollars": (self.expected_all_llm_monthly_microdollars),
            "expected_all_llm_monthly_usd": microdollars_to_decimal(
                self.expected_all_llm_monthly_microdollars
            ),
            "annual_minimum_microdollars": self.annual_minimum_microdollars,
            "annual_minimum_usd": microdollars_to_decimal(self.annual_minimum_microdollars),
            "annual_savings_microdollars": self.annual_savings_microdollars,
            "annual_savings_usd": microdollars_to_decimal(self.annual_savings_microdollars),
            "goal_microdollars": self.goal_microdollars,
            "goal_usd": microdollars_to_decimal(self.goal_microdollars),
            "goal_remaining_microdollars": self.goal_remaining_microdollars,
            "goal_remaining_usd": microdollars_to_decimal(self.goal_remaining_microdollars),
            "progress_basis_points": self.progress_basis_points,
            "goal_reached": self.goal_remaining_microdollars == 0,
            # Only the intentionally anonymous projection is public. No ids,
            # timestamps, users, workspaces, names, companies, or amounts per
            # buyer are present here.
            "messages": [{"message": message} for message in self.messages],
        }


def public_snapshot(
    aggregate: BedrockGroupBuyAggregate,
    messages: list[BedrockGroupBuyPublicMessage],
) -> BedrockGroupBuyPublicSnapshot:
    monthly_minimum = _public_total(
        BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS + aggregate.monthly_minimum_microdollars
    )
    expected_bedrock = _public_total(
        BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS + aggregate.expected_bedrock_monthly_microdollars
    )
    expected_all_llm = _public_total(
        BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS + aggregate.expected_all_llm_monthly_microdollars
    )
    annual_minimum = monthly_minimum * BEDROCK_GROUP_BUY_TERM_MONTHS
    annual_savings = (annual_minimum * BEDROCK_GROUP_BUY_SAVINGS_BASIS_POINTS + 5_000) // 10_000
    return BedrockGroupBuyPublicSnapshot(
        buyer_count=BEDROCK_GROUP_BUY_FOUNDING_BUYERS + aggregate.active_pledge_count,
        monthly_minimum_microdollars=monthly_minimum,
        expected_bedrock_monthly_microdollars=expected_bedrock,
        expected_all_llm_monthly_microdollars=expected_all_llm,
        annual_minimum_microdollars=annual_minimum,
        annual_savings_microdollars=annual_savings,
        goal_microdollars=BEDROCK_GROUP_BUY_GOAL_MICRODOLLARS,
        goal_remaining_microdollars=max(
            0,
            BEDROCK_GROUP_BUY_GOAL_MICRODOLLARS - monthly_minimum,
        ),
        progress_basis_points=(monthly_minimum * 10_000 // BEDROCK_GROUP_BUY_GOAL_MICRODOLLARS),
        messages=tuple(item.message for item in messages),
    )


def pledge_from_mapping(
    values: Mapping[str, object],
    *,
    user_id: str,
    workspace_id: str,
) -> BedrockGroupBuyPledge | None:
    monthly_minimum = _money(values, "monthly_minimum")
    if monthly_minimum == 0:
        return None
    if monthly_minimum < 0:
        raise ValueError("Monthly commitment cannot be negative")
    if monthly_minimum > BEDROCK_GROUP_BUY_MAX_MONTHLY_MICRODOLLARS:
        raise ValueError("Monthly commitment is above the supported maximum")

    expected_bedrock = _money(values, "expected_bedrock_monthly")
    expected_all_llm = _money(values, "expected_all_llm_monthly")
    if expected_bedrock < monthly_minimum:
        raise ValueError("Expected Bedrock spend must be at least the committed minimum")
    if expected_bedrock > BEDROCK_GROUP_BUY_MAX_MONTHLY_MICRODOLLARS:
        raise ValueError("Expected Bedrock spend is above the supported maximum")
    if expected_all_llm < expected_bedrock:
        raise ValueError("Expected total LLM spend must be at least expected Bedrock spend")
    if expected_all_llm > BEDROCK_GROUP_BUY_MAX_TOTAL_LLM_MICRODOLLARS:
        raise ValueError("Expected total LLM spend is above the supported maximum")

    last_month_llm_spend = _money(values, "last_month_llm_spend")
    if last_month_llm_spend < 0:
        raise ValueError("Last month's actual LLM spend cannot be negative")
    if last_month_llm_spend > BEDROCK_GROUP_BUY_MAX_TOTAL_LLM_MICRODOLLARS:
        raise ValueError("Last month's actual LLM spend is above the supported maximum")
    last_month_spend_sources = _spend_sources(values)
    if last_month_llm_spend > 0 and not last_month_spend_sources:
        raise ValueError("Select where your team spent its LLM budget last month")
    if last_month_llm_spend == 0:
        last_month_spend_sources = ()

    full_name = _private_text(values, "full_name", minimum=2, maximum=120)
    title = _private_text(values, "title", minimum=2, maximum=120)
    company_name = _private_text(values, "company_name", minimum=2, maximum=160)
    company_url = _company_url(values)
    if not _truthy(values.get("authorized")):
        raise ValueError("Confirm that you are authorized to make this commitment")
    if not _truthy(values.get("terms_accepted")):
        raise ValueError("Accept the 12-month group-buy commitment terms")

    publish_message = _truthy(values.get("publish_message"))
    public_message = ""
    if publish_message:
        if not _truthy(values.get("public_message_confirmed")):
            raise ValueError("Confirm that the public message contains no identifying details")
        public_message = _public_message(
            values,
            private_values=(full_name, company_name, company_url),
        )

    return BedrockGroupBuyPledge(
        user_id=user_id,
        workspace_id=workspace_id,
        full_name=full_name,
        title=title,
        company_name=company_name,
        company_url=company_url,
        monthly_minimum_microdollars=monthly_minimum,
        expected_bedrock_monthly_microdollars=expected_bedrock,
        expected_all_llm_monthly_microdollars=expected_all_llm,
        aggregate_shard=bedrock_group_buy_shard(user_id),
        last_month_llm_spend_microdollars=last_month_llm_spend,
        last_month_spend_sources=last_month_spend_sources,
        public_message=public_message,
        publish_message=publish_message,
    )


def pledge_form_values(
    pledge: BedrockGroupBuyPledge | None,
    values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if values is not None:
        return {
            "full_name": str(values.get("full_name", ""))[:120],
            "title": str(values.get("title", ""))[:120],
            "company_name": str(values.get("company_name", ""))[:160],
            "company_url": str(values.get("company_url", ""))[:2048],
            "monthly_minimum": str(values.get("monthly_minimum", ""))[:32],
            "expected_bedrock_monthly": str(values.get("expected_bedrock_monthly", ""))[:32],
            "expected_all_llm_monthly": str(values.get("expected_all_llm_monthly", ""))[:32],
            "last_month_llm_spend": str(values.get("last_month_llm_spend", ""))[:32],
            "last_month_spend_sources": _spend_sources(values, strict=False),
            "publish_message": _truthy(values.get("publish_message")),
            "public_message": str(values.get("public_message", ""))[:280],
        }
    if pledge is None:
        return {
            "full_name": "",
            "title": "",
            "company_name": "",
            "company_url": "",
            "monthly_minimum": "",
            "expected_bedrock_monthly": "",
            "expected_all_llm_monthly": "",
            "last_month_llm_spend": "",
            "last_month_spend_sources": (),
            "publish_message": False,
            "public_message": "",
        }
    return {
        "full_name": pledge.full_name,
        "title": pledge.title,
        "company_name": pledge.company_name,
        "company_url": pledge.company_url,
        "monthly_minimum": microdollars_to_decimal(pledge.monthly_minimum_microdollars),
        "expected_bedrock_monthly": microdollars_to_decimal(
            pledge.expected_bedrock_monthly_microdollars
        ),
        "expected_all_llm_monthly": microdollars_to_decimal(
            pledge.expected_all_llm_monthly_microdollars
        ),
        "last_month_llm_spend": microdollars_to_decimal(pledge.last_month_llm_spend_microdollars),
        "last_month_spend_sources": tuple(pledge.last_month_spend_sources),
        "publish_message": pledge.publish_message,
        "public_message": pledge.public_message,
    }


def _money(values: Mapping[str, object], name: str) -> int:
    raw = str(values.get(name, "")).strip().replace(",", "")
    if raw.startswith("$"):
        raw = raw[1:].strip()
    if not raw:
        raise ValueError("Complete every monthly spend field")
    try:
        return dollars_to_microdollars(raw)
    except ValueError as exc:
        raise ValueError("Enter monthly amounts in US dollars") from exc


def _spend_sources(
    values: Mapping[str, object],
    *,
    strict: bool = True,
) -> tuple[str, ...]:
    selected: set[str] = set()
    raw = values.get("last_month_spend_sources")
    if isinstance(raw, (list, tuple, set, frozenset)):
        selected.update(str(item).strip() for item in raw)
    elif raw:
        selected.update(part.strip() for part in str(raw).split(","))
    for source_id, _ in BEDROCK_GROUP_BUY_SPEND_SOURCES:
        if _truthy(values.get(f"last_month_spend_source_{source_id}")):
            selected.add(source_id)
    selected.discard("")
    unknown = selected - _BEDROCK_GROUP_BUY_SPEND_SOURCE_IDS
    if unknown and strict:
        raise ValueError("Select only the listed last-month spend sources")
    return tuple(
        source_id for source_id, _ in BEDROCK_GROUP_BUY_SPEND_SOURCES if source_id in selected
    )


def _private_text(
    values: Mapping[str, object],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    value = _SPACE_RE.sub(" ", str(values.get(name, "")).strip())
    if len(value) < minimum:
        raise ValueError("Complete every private company and contact field")
    if len(value) > maximum:
        raise ValueError("One of the private fields is too long")
    return value


def _company_url(values: Mapping[str, object]) -> str:
    value = str(values.get("company_url", "")).strip()
    if len(value) > 2048:
        raise ValueError("Company URL is too long")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete https:// company URL")
    if parsed.username or parsed.password:
        raise ValueError("Company URL cannot contain credentials")
    return value


def _public_message(
    values: Mapping[str, object],
    *,
    private_values: tuple[str, ...],
) -> str:
    value = _SPACE_RE.sub(" ", str(values.get("public_message", "")).strip())
    if len(value) < 12 or len(value) > 280:
        raise ValueError("Anonymous public message must be 12 to 280 characters")
    if any(pattern.search(value) for pattern in (_EMAIL_RE, _URL_RE, _HANDLE_RE, _PHONE_RE)):
        raise ValueError("Remove contact details and links from the anonymous public message")
    if "<" in value or ">" in value:
        raise ValueError("Anonymous public messages must be plain text")
    lowered = value.casefold()
    for private_value in private_values:
        candidate = private_value.strip().casefold()
        if len(candidate) >= 4 and candidate in lowered:
            raise ValueError("Remove names and company details from the anonymous public message")
    return value


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def formatted_campaign_money(microdollars: int) -> str:
    dollars = Decimal(microdollars) / Decimal(MICRODOLLARS_PER_DOLLAR)
    return f"${dollars:,.0f}"


def _public_total(exact_microdollars: int) -> int:
    """Round down before publication to resist single-pledge differencing."""
    return (
        exact_microdollars // BEDROCK_GROUP_BUY_PUBLIC_BUCKET_MICRODOLLARS
    ) * BEDROCK_GROUP_BUY_PUBLIC_BUCKET_MICRODOLLARS
