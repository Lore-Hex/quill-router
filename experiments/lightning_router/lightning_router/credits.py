"""Boundary to the existing USD ledger, not a second inference currency."""

from typing import Protocol


class Credits(Protocol):
    def resolve(self, raw_key: str, *, new: bool) -> str:
        """Authenticate an existing key or idempotently provision a zero-credit
        key-only account. Return an immutable account/workspace identifier.
        Provision only AFTER verified Lightning settlement. Invoice creation
        must never call this with new=True. Provisioning must not grant
        management privileges or free credits.
        """
        ...

    def balance(self, account_id: str) -> int:
        """Read available integer microdollars from the authoritative USD ledger."""
        ...

    def usage(self, raw_key: str) -> dict[str, str | None]:
        """Read only this credential's spend and limits, never workspace activity."""
        ...

    def credit(self, account_id: str, payment_hash: str, amount_microdollars: int) -> None:
        """Commit a USD deposit exactly once under lightning:<payment_hash>.

        Replaying the same payment must succeed without adding funds again;
        changing its account or amount must fail. A successful return means
        the credit is durable and spendable by the existing inference path.
        """
        ...
