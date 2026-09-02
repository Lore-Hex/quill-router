"""Fail-closed eligibility policy for monetized delegated-auth apps."""

from __future__ import annotations

from trusted_router.storage_models import OAuthApp, User


def user_can_receive_creator_payouts(user: User | None) -> bool:
    return bool(
        user is not None
        and user.identity_verified
        and (user.identity_verified_name or "").strip()
    )


def oauth_app_is_effectively_suspended(
    app: OAuthApp,
    owner: User | None,
) -> bool:
    if app.suspended:
        return True
    return app.markup_basis_points > 0 and not user_can_receive_creator_payouts(owner)


def oauth_app_can_authorize(app: OAuthApp, owner: User | None) -> bool:
    return not oauth_app_is_effectively_suspended(app, owner)
