"""Public setup metadata. Unknown limits stay unknown, not invented caps."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Reviewed 2026-09-15 against https://api-docs.deepseek.com/api/create-chat-completion/
# and https://api-docs.deepseek.com/quick_start/pricing/ . The rolling Flash ID
# currently names V4.1. Default thinking output is 64K, maximum is 384K.
DEEPSEEK_LIMITS = {
    "deepseek/deepseek-v4.1-flash", "deepseek/deepseek-flash",
    "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813",
}


def privacy(item: dict[str, Any]) -> dict[str, list[str]]:
    providers: dict[str, set[str]] = {key: set() for key in ("any", "no_store", "zdr", "confidential")}
    endpoints = (item.get("trustedrouter") or {}).get("endpoints")
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(endpoint, dict) or endpoint.get("usage_type") != "Credits":
            continue
        slug = endpoint.get("provider")
        parameters = endpoint.get("supported_parameters")
        if (not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", slug)
                or not isinstance(parameters, list) or "tools" not in parameters):
            continue
        providers["any"].add(slug)
        # These are separate guarantees, not a numeric implication chain.
        if endpoint.get("stores_content") is False:
            providers["no_store"].add(slug)
        if endpoint.get("provider_zero_data_retention") is True:
            providers["zdr"].add(slug)
        if endpoint.get("provider_confidential_compute") is True and endpoint.get("provider_e2ee") is True:
            providers["confidential"].add(slug)
    return {key: sorted(slugs) for key, slugs in providers.items()}


def positive_int(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= 100_000_000 else None


def limits(item: dict[str, Any]) -> dict[str, Any]:
    top = item.get("top_provider") or {}
    context = positive_int(item.get("context_length"))
    output = positive_int(top.get("max_completion_tokens"))
    default = positive_int(item.get("default_max_tokens"))
    source = None
    if item["id"] in DEEPSEEK_LIMITS:
        output = output or 393216
        default = default or 65536
        source = "https://api-docs.deepseek.com/api/create-chat-completion/"
    if output and context:
        output = min(output, context)
    if default and output:
        default = min(default, output)
    return {"context": context, "output": output, "default_output": default,
            "limits_source": source}


def public_price(value: Any, *, per_million: bool = True) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 80:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or not isinstance(exponent, int) or exponent < -18 or amount < 0 or amount > 1_000_000:
        return None
    return format(amount * (1_000_000 if per_million else 1), "f")


def pricing(item: dict[str, Any]) -> dict[str, str | None]:
    price = item.get("pricing") or {}
    policy = item.get("trustedrouter") or {}
    return {
        "input_per_million": public_price(price.get("prompt")),
        "output_per_million": public_price(price.get("completion")),
        "cached_input_per_million": public_price(price.get("input_cache_read")),
        "request_usd": public_price(price.get("request"), per_million=False) or micro_price(policy.get("request_price_microdollars")),
        "minimum_charge_usd": micro_price(policy.get("minimum_charge_microdollars")),
    }


def micro_price(value: Any) -> str | None:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        return None
    return format(Decimal(value) / 1_000_000, "f")
