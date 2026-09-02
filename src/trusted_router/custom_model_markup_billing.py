"""Billing math and durable settlement fields for prompt-wrapper markup."""

from __future__ import annotations

from trusted_router.app_markup_billing import (
    app_markup_microdollars,
    app_markup_microdollars_from_charge,
)
from trusted_router.custom_model_billing import CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS

CUSTOM_MODEL_MARKUP_PAYOUT_SETTLE_FIELD = (
    "_trustedrouter_custom_model_markup_payout_microdollars"
)
CUSTOM_MODEL_MARKUP_CHARGE_SETTLE_FIELD = (
    "_trustedrouter_custom_model_markup_charge_microdollars"
)
CUSTOM_MODEL_MARKUP_OWNER_SETTLE_FIELD = (
    "_trustedrouter_custom_model_markup_owner_user_id"
)
CUSTOM_MODEL_MARKUP_ID_SETTLE_FIELD = "_trustedrouter_custom_model_markup_model_id"
MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS = 30_000


def validate_custom_model_markup_basis_points(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CUSTOM_MODEL_MARKUP_BASIS_POINTS
    ):
        raise ValueError("custom_model_markup_out_of_bounds")
    return value


def custom_model_markup_microdollars(
    token_cost_microdollars: int,
    markup_basis_points: int,
) -> int:
    """Return the creator markup on token cost, using integer microdollars."""
    return app_markup_microdollars(token_cost_microdollars, markup_basis_points)


def custom_model_markup_microdollars_from_charge(
    charge_including_markup_microdollars: int,
    markup_basis_points: int,
) -> int:
    """Recover the collected custom markup from a charge that includes it."""
    return app_markup_microdollars_from_charge(
        charge_including_markup_microdollars,
        markup_basis_points,
    )


def collected_custom_model_markup_microdollars(
    final_charge_microdollars: int,
    custom_model_markup_basis_points: int,
    *,
    app_markup_basis_points: int = 0,
    additional_cost_microdollars: int = 0,
) -> int:
    """Recover the custom-model markup that survived outer charge clamps.

    App markup is the outermost pricing layer and hosted-tool/media cost is
    protected before the prompt-wrapper layer. This inverse is shared by the
    inline and durable-repair paths so a cap can never create an uncollected
    creator payout.
    """
    if custom_model_markup_basis_points <= 0:
        return 0
    app_markup = (
        app_markup_microdollars_from_charge(
            final_charge_microdollars,
            app_markup_basis_points,
        )
        if app_markup_basis_points > 0
        else 0
    )
    custom_charge = max(
        0,
        final_charge_microdollars
        - app_markup
        - max(0, additional_cost_microdollars),
    )
    return custom_model_markup_microdollars_from_charge(
        custom_charge,
        custom_model_markup_basis_points,
    )


def custom_model_markup_owner_share_microdollars(
    markup_microdollars: int,
) -> int:
    return int(markup_microdollars) * CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS // 10_000


def custom_model_markup_payout_event_id(authorization_id: str) -> str:
    return f"custom_model_markup_payout:{authorization_id}"


def custom_model_markup_authorization_id_from_payout_event_id(
    event_id: str,
) -> str | None:
    prefix = "custom_model_markup_payout:"
    if not event_id.startswith(prefix):
        return None
    authorization_id = event_id.removeprefix(prefix)
    return authorization_id or None
