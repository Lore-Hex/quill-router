"""Fixed billing math for user-provided Custom Models."""

from __future__ import annotations

from trusted_router.money import token_cost_microdollars

CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS = 7_000
HUMAN_PRICE_MIN_MICRODOLLARS_PER_M = 100_000_000_000
HUMAN_PRICE_MAX_MICRODOLLARS_PER_M = 1_000_000_000_000
MACHINE_PRICE_MAX_MICRODOLLARS_PER_M = 1_000_000_000
USER_MODEL_PAYOUT_SETTLE_FIELD = "_trustedrouter_user_model_payout_microdollars"
USER_MODEL_OWNER_SETTLE_FIELD = "_trustedrouter_user_model_owner_user_id"
USER_MODEL_ID_SETTLE_FIELD = "_trustedrouter_user_model_id"


def user_model_payout_event_id(authorization_id: str) -> str:
    return f"custom_model_payout:{authorization_id}"


def user_model_authorization_id_from_payout_event_id(event_id: str) -> str | None:
    prefix = "custom_model_payout:"
    if not event_id.startswith(prefix):
        return None
    authorization_id = event_id.removeprefix(prefix)
    return authorization_id or None


def validate_custom_model_price(
    prompt_price: int,
    completion_price: int,
    *,
    kind: str,
) -> None:
    prices = (int(prompt_price), int(completion_price))
    if kind == "human":
        valid = all(
            HUMAN_PRICE_MIN_MICRODOLLARS_PER_M <= price <= HUMAN_PRICE_MAX_MICRODOLLARS_PER_M
            for price in prices
        )
    elif kind in {"machine", "agent"}:
        valid = all(0 <= price <= MACHINE_PRICE_MAX_MICRODOLLARS_PER_M for price in prices)
    else:
        valid = False
    if not valid:
        raise ValueError("custom_model_price_out_of_bounds")


def custom_model_cost_microdollars(
    *,
    input_tokens: int,
    output_tokens: int,
    prompt_price: int,
    completion_price: int,
) -> int:
    cost = token_cost_microdollars(input_tokens, prompt_price) + token_cost_microdollars(
        output_tokens,
        completion_price,
    )
    has_positive_billable_tokens = (input_tokens > 0 and prompt_price > 0) or (
        output_tokens > 0 and completion_price > 0
    )
    if cost == 0 and has_positive_billable_tokens:
        return 1
    return cost


def owner_share_microdollars(customer_charge: int) -> int:
    return int(customer_charge) * CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS // 10_000
