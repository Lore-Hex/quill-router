from __future__ import annotations

from typing import Any

from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_group_buy import (
    apply_pledge_delta,
    empty_aggregate_shard,
    new_public_message_id,
    prepare_pledge_mutation,
    sum_aggregate_shards,
)
from trusted_router.storage_models import (
    BedrockGroupBuyAggregate,
    BedrockGroupBuyAggregateShard,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
    iso_now,
)

_PLEDGE_KIND = "bedrock_group_buy_pledge"
_SHARD_KIND = "bedrock_group_buy_aggregate_shard"
_PUBLIC_MESSAGE_KIND = "bedrock_group_buy_public_message"
_PUBLIC_MESSAGE_INDEX_KIND = "bedrock_group_buy_public_message_index"


class SpannerBedrockGroupBuy:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def upsert(self, incoming: BedrockGroupBuyPledge) -> BedrockGroupBuyPledge:
        candidate_public_id = new_public_message_id()

        def txn(transaction: Any) -> BedrockGroupBuyPledge:
            existing = self._io.read_entity_tx(
                transaction,
                _PLEDGE_KIND,
                incoming.user_id,
                BedrockGroupBuyPledge,
            )
            now = iso_now()
            mutation = prepare_pledge_mutation(
                incoming=incoming,
                existing=existing,
                candidate_public_message_id=candidate_public_id,
                now=now,
            )
            shard_id = f"{incoming.aggregate_shard:02d}"
            shard = self._io.read_entity_tx(
                transaction,
                _SHARD_KIND,
                shard_id,
                BedrockGroupBuyAggregateShard,
            ) or empty_aggregate_shard(incoming.aggregate_shard)
            updated_shard = apply_pledge_delta(
                shard,
                old=existing,
                new=mutation.pledge,
                now=now,
            )
            self._io.write_entity_tx(
                transaction,
                _PLEDGE_KIND,
                incoming.user_id,
                mutation.pledge,
            )
            self._io.write_entity_tx(transaction, _SHARD_KIND, shard_id, updated_shard)
            if mutation.old_public_message_index_id:
                self._io.delete_entities_tx(
                    transaction,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    [mutation.old_public_message_index_id],
                )
            if (
                mutation.old_public_message_id
                and mutation.public_message is None
            ):
                self._io.delete_entities_tx(
                    transaction,
                    _PUBLIC_MESSAGE_KIND,
                    [mutation.old_public_message_id],
                )
            if mutation.public_message is not None:
                self._io.write_entity_tx(
                    transaction,
                    _PUBLIC_MESSAGE_KIND,
                    mutation.public_message.id,
                    mutation.public_message,
                )
                self._io.write_entity_tx(
                    transaction,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    mutation.pledge.public_message_index_id,
                    {"message_id": mutation.public_message.id},
                )
            return mutation.pledge

        return run_in_transaction_with_retry(self._io.database, txn)

    def get(self, user_id: str) -> BedrockGroupBuyPledge | None:
        return self._io.read_entity(_PLEDGE_KIND, user_id, BedrockGroupBuyPledge)

    def withdraw(self, user_id: str) -> bool:
        def txn(transaction: Any) -> bool:
            existing = self._io.read_entity_tx(
                transaction,
                _PLEDGE_KIND,
                user_id,
                BedrockGroupBuyPledge,
            )
            if existing is None:
                return False
            now = iso_now()
            shard_id = f"{existing.aggregate_shard:02d}"
            shard = self._io.read_entity_tx(
                transaction,
                _SHARD_KIND,
                shard_id,
                BedrockGroupBuyAggregateShard,
            ) or empty_aggregate_shard(existing.aggregate_shard)
            updated_shard = apply_pledge_delta(
                shard,
                old=existing,
                new=None,
                now=now,
            )
            self._io.write_entity_tx(transaction, _SHARD_KIND, shard_id, updated_shard)
            self._io.delete_entities_tx(transaction, _PLEDGE_KIND, [user_id])
            if existing.public_message_id:
                self._io.delete_entities_tx(
                    transaction,
                    _PUBLIC_MESSAGE_KIND,
                    [existing.public_message_id],
                )
            if existing.public_message_index_id:
                self._io.delete_entities_tx(
                    transaction,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    [existing.public_message_index_id],
                )
            return True

        return run_in_transaction_with_retry(self._io.database, txn)

    def aggregate(self) -> BedrockGroupBuyAggregate:
        shards = self._io.list_entities(
            _SHARD_KIND,
            cls=BedrockGroupBuyAggregateShard,
        )
        return sum_aggregate_shards(shards)

    def list_public_messages(self, *, limit: int = 50) -> list[BedrockGroupBuyPublicMessage]:
        pointers = self._io.list_entities(
            _PUBLIC_MESSAGE_INDEX_KIND,
            cls=dict,
            limit=max(0, limit),
        )
        messages: list[BedrockGroupBuyPublicMessage] = []
        for pointer in pointers:
            message_id = str(pointer.get("message_id", ""))
            if not message_id:
                continue
            message = self._io.read_entity(
                _PUBLIC_MESSAGE_KIND,
                message_id,
                BedrockGroupBuyPublicMessage,
            )
            if message is not None:
                messages.append(message)
        return messages[:limit]

    def list_private_pledges(self, *, limit: int = 1000) -> list[BedrockGroupBuyPledge]:
        rows = self._io.list_entities(
            _PLEDGE_KIND,
            cls=BedrockGroupBuyPledge,
            limit=max(0, limit),
        )
        rows.sort(key=lambda item: (item.updated_at, item.user_id), reverse=True)
        return rows[:limit]
