"""Fixed billing math and durable field names for OAuth app markup."""

from __future__ import annotations

from trusted_router.custom_model_billing import CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS

APP_MARKUP_PAYOUT_SETTLE_FIELD = "_trustedrouter_app_markup_payout_microdollars"
APP_MARKUP_OWNER_SETTLE_FIELD = "_trustedrouter_app_markup_owner_user_id"
APP_MARKUP_APP_ID_SETTLE_FIELD = "_trustedrouter_app_markup_app_id"


def app_markup_microdollars(base_cost: int, markup_basis_points: int) -> int:
    return int(base_cost) * int(markup_basis_points) // 10_000


def app_markup_owner_share_microdollars(markup_microdollars: int) -> int:
    return int(markup_microdollars) * CUSTOM_MODEL_OWNER_SHARE_BASIS_POINTS // 10_000


def app_markup_microdollars_from_charge(
    charge_microdollars: int, markup_basis_points: int
) -> int:
    """Derive the markup component from a frozen charge that includes it."""
    return (
        int(charge_microdollars)
        * int(markup_basis_points)
        // (10_000 + int(markup_basis_points))
    )


def app_markup_payout_event_id(authorization_id: str) -> str:
    return f"app_markup_payout:{authorization_id}"


def app_markup_authorization_id_from_payout_event_id(event_id: str) -> str | None:
    prefix = "app_markup_payout:"
    if not event_id.startswith(prefix):
        return None
    authorization_id = event_id.removeprefix(prefix)
    return authorization_id or None
