from __future__ import annotations

from trusted_router.auth import Principal
from trusted_router.storage import STORE, User

# Where a wallet-only account adds and verifies an email (routes/wallet_oauth.py).
WALLET_EMAIL_ATTACH_PATH = "/auth/wallet/email"
STABLECOIN_CHECKOUT_METHODS = frozenset({"stablecoin", "crypto", "usdc"})
WALLET_ONLY_STABLECOIN_MESSAGE = (
    "Wallet-only accounts can only fund with stablecoin. "
    "Add and verify an email to use other payment methods."
)
WALLET_ONLY_BYOK_MESSAGE = (
    "Wallet-only accounts cannot register provider keys. "
    "Add and verify an email to use BYOK."
)


def is_wallet_only_user(user: User | None) -> bool:
    """Whether the identity is backed only by a wallet signature.

    An unverified email does not change the account's authentication
    properties, so card-like payment methods remain locked until verification.
    """
    return bool(user and user.wallet_address and not (user.email and user.email_verified))


def principal_user(principal: Principal) -> User | None:
    """The identity a request acts as: the session user, else the key's
    creator, else the workspace owner. Derived keys inherit their parent's
    creator (routes/keys.py); the owner fallback covers older creator-less
    keys, which would otherwise shed wallet-only restrictions."""
    if principal.user is not None:
        return principal.user
    if principal.api_key is not None and principal.api_key.creator_user_id:
        creator = STORE.get_user(principal.api_key.creator_user_id)
        if creator is not None:
            return creator
    return STORE.get_user(principal.workspace.owner_user_id)


def is_wallet_only_principal(principal: Principal) -> bool:
    return is_wallet_only_user(principal_user(principal))


def is_stablecoin_checkout_method(payment_method: str) -> bool:
    return payment_method.strip().lower() in STABLECOIN_CHECKOUT_METHODS
