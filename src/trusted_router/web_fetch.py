"""Content-free contract for the standalone DeepSeek Fetch API."""

from decimal import Decimal

from trusted_router.pricing import customer_fixed_price_microdollars

FETCH_MODEL_ID = "deepseek/fetch"
FETCH_ROUTE_TYPE = "tools.fetch"
FETCH_PATH = "/v1/tools/fetch"
# Official standalone rate, verified 2026-10-17. The temporary promotion
# applies to DeepSeek's server-side agent tools, not this standalone API.
FETCH_UPSTREAM_COST_MICRODOLLARS = 750
FETCH_PRICE_MICRODOLLARS = customer_fixed_price_microdollars(FETCH_UPSTREAM_COST_MICRODOLLARS)
FETCH_PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"


def fetch_pricing() -> dict[str, str]:
    return {"request": str(Decimal(FETCH_PRICE_MICRODOLLARS) / 1_000_000)}


def fetch_price_label() -> str:
    return f"${fetch_pricing()['request']} / successful request"


def fetch_metadata() -> dict[str, object]:
    return {
        "endpoint": FETCH_PATH,
        "billing_unit": "successful_request",
        "pricing": fetch_pricing(),
        "pricing_source": FETCH_PRICING_SOURCE,
        "max_url_characters": 8192,
    }
