"""Place a real hold using each backend's supported reservation primitive."""

from collections.abc import Callable
from typing import Any

from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_counter_dml import release_credit, reserve_credit
from trusted_router.storage_gcp_counters import credit_shard_count
from trusted_router.storage_models import CreditAccount


def reserve_for_transfer(store: Any, workspace_id: str, amount: int) -> Callable[[], None]:
    if isinstance(store, SpannerBigtableStore):

        def reserve(tx: Any) -> list[tuple[int, int]]:
            account = store._read_entity_tx(tx, "credit", workspace_id, CreditAccount)
            assert account is not None
            rows = tx.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@workspace_id "
                "AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"workspace_id": workspace_id, "shard_count": credit_shard_count(account)},
                param_types={
                    "workspace_id": store._param_types.STRING,
                    "shard_count": store._param_types.INT64,
                },
            )
            remaining = amount
            holds = []
            for shard, credits, usage, reserved in rows:
                take = min(remaining, max(0, int(credits) - int(usage) - int(reserved)))
                if take:
                    assert reserve_credit(
                        tx, store._param_types, workspace_id, take, shard=int(shard)
                    )
                    holds.append((int(shard), take))
                    remaining -= take
            assert remaining == 0
            return holds

        holds = store._run_in_transaction(reserve)

        def refund() -> None:
            def release(tx: Any) -> None:
                for shard, held in holds:
                    assert (
                        release_credit(tx, store._param_types, workspace_id, held, 0, shard=shard)
                        == 1
                    )

            store._run_in_transaction(release)

        return refund
    hold = store.reserve(workspace_id, "transfer-hold-key", amount)
    return lambda: store.refund(hold.id)


def fail_before_transfer_credit(store: Any, recipient_id: str, monkeypatch: Any) -> None:
    """Fail after the actual debit, at the first recipient credit operation."""
    from trusted_router.storage import InMemoryStore

    if isinstance(store, InMemoryStore):
        recipient = store.credit_money[recipient_id]
        original = type(recipient).__setattr__
        fired = False

        def fail(self: Any, name: str, value: Any) -> None:
            nonlocal fired
            if self is recipient and name == "total_credits_microdollars" and not fired:
                fired = True
                raise RuntimeError("injected before recipient credit")
            original(self, name, value)

        monkeypatch.setattr(type(recipient), "__setattr__", fail)
    else:
        def fail_credit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected before recipient credit")

        monkeypatch.setattr(type(store), "_credit_workspace_balance_tx", fail_credit)
