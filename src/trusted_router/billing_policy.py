from __future__ import annotations

from trusted_router.auth import Principal
from trusted_router.storage import STORE, User

STABLECOIN_CHECKOUT_METHODS = frozenset({"stablecoin", "crypto", "usdc"})
WALLET_ONLY_STABLECOIN_MESSAGE = (
    "Wallet-only accounts can only fund with stablecoin. "
    "Add and verify an email to use other payment methods."
)


def is_wallet_only_user(user: User | None) -> bool:
    """Whether the identity is backed only by a wallet signature.

    An unverified email does not change the account's authentication
    properties, so card-like payment methods remain locked until verification.
    """
    return bool(user and user.wallet_address and not (user.email and user.email_verified))


def principal_user(principal: Principal) -> User | None:
    if principal.user is not None:
        return principal.user
    if principal.api_key is None or not principal.api_key.creator_user_id:
        return None
    return STORE.get_user(principal.api_key.creator_user_id)


def is_wallet_only_principal(principal: Principal) -> bool:
    return is_wallet_only_user(principal_user(principal))


def is_stablecoin_checkout_method(payment_method: str) -> bool:
    return payment_method.strip().lower() in STABLECOIN_CHECKOUT_METHODS
