"""Narrow USD ledger bridge for verified Lightning funding workers."""

from datetime import UTC, datetime

from trusted_router.auth import is_api_key_expired
from trusted_router.storage_models import CreditProvenance
from trusted_router.store_protocol import Store
from trusted_router.typed_balance import live_credit_summary


class LightningCredits:
    def __init__(self, store: Store) -> None:
        self.store = store

    def resolve(self, raw_key: str, *, new: bool) -> str:
        if new:
            self.store.ensure_lightning_key(raw_key)
        context = self.store.api_key_auth_context(raw_key)
        if context is None or context.workspace is None:
            raise ValueError("invalid_api_key")
        key = context.api_key
        if key.disabled or key.federated_home or is_api_key_expired(key.expires_at):
            raise ValueError("invalid_api_key")
        return context.workspace.id

    def balance(self, account_id: str) -> int:
        summary = live_credit_summary(account_id, store=self.store)
        if summary is None:
            raise ValueError("credit_account_not_found")
        return summary["available"]

    def credit(self, account_id: str, payment_hash: str, amount_microdollars: int) -> None:
        # Durable immutable claim first. A process crash here leaves a retryable
        # payment, not a credit. The existing USD operation commits exactly once.
        self.store.bind_lightning_payment(account_id, payment_hash, amount_microdollars)
        self.store.credit_workspace_typed_direct(
            account_id, amount_microdollars, "lightning:" + payment_hash,
            provenance=CreditProvenance("invoice", "lightning", payment_hash, datetime.now(UTC)),
            payment_amount_microdollars=amount_microdollars, currency="USD",
        )
