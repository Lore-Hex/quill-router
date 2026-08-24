from __future__ import annotations

from collections.abc import Callable
from typing import Any

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


class PostgresBedrockGroupBuy:
    def __init__(
        self,
        *,
        run_transaction: Callable[[Callable[[Any], Any]], Any],
        read_entity_tx: Callable[..., Any],
        write_entity_tx: Callable[[Any, str, str, Any], None],
        delete_entity_tx: Callable[[Any, str, str], int],
        read_entity: Callable[[str, str, type[Any]], Any],
        list_entities: Callable[..., list[Any]],
    ) -> None:
        self._run_transaction = run_transaction
        self._read_entity_tx = read_entity_tx
        self._write_entity_tx = write_entity_tx
        self._delete_entity_tx = delete_entity_tx
        self._read_entity = read_entity
        self._list_entities = list_entities

    def upsert(self, incoming: BedrockGroupBuyPledge) -> BedrockGroupBuyPledge:
        candidate_public_id = new_public_message_id()

        def txn(conn: Any) -> BedrockGroupBuyPledge:
            existing = self._read_entity_tx(
                conn,
                _PLEDGE_KIND,
                incoming.user_id,
                BedrockGroupBuyPledge,
                for_update=True,
            )
            now = iso_now()
            mutation = prepare_pledge_mutation(
                incoming=incoming,
                existing=existing,
                candidate_public_message_id=candidate_public_id,
                now=now,
            )
            shard_id = f"{incoming.aggregate_shard:02d}"
            shard = self._read_entity_tx(
                conn,
                _SHARD_KIND,
                shard_id,
                BedrockGroupBuyAggregateShard,
                for_update=True,
            ) or empty_aggregate_shard(incoming.aggregate_shard)
            updated_shard = apply_pledge_delta(
                shard,
                old=existing,
                new=mutation.pledge,
                now=now,
            )
            self._write_entity_tx(
                conn,
                _PLEDGE_KIND,
                incoming.user_id,
                mutation.pledge,
            )
            self._write_entity_tx(conn, _SHARD_KIND, shard_id, updated_shard)
            if mutation.old_public_message_index_id:
                self._delete_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    mutation.old_public_message_index_id,
                )
            if mutation.old_public_message_id and mutation.public_message is None:
                self._delete_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_KIND,
                    mutation.old_public_message_id,
                )
            if mutation.public_message is not None:
                self._write_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_KIND,
                    mutation.public_message.id,
                    mutation.public_message,
                )
                self._write_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    mutation.pledge.public_message_index_id,
                    {"message_id": mutation.public_message.id},
                )
            return mutation.pledge

        return self._run_transaction(txn)

    def get(self, user_id: str) -> BedrockGroupBuyPledge | None:
        return self._read_entity(_PLEDGE_KIND, user_id, BedrockGroupBuyPledge)

    def withdraw(self, user_id: str) -> bool:
        def txn(conn: Any) -> bool:
            existing = self._read_entity_tx(
                conn,
                _PLEDGE_KIND,
                user_id,
                BedrockGroupBuyPledge,
                for_update=True,
            )
            if existing is None:
                return False
            now = iso_now()
            shard_id = f"{existing.aggregate_shard:02d}"
            shard = self._read_entity_tx(
                conn,
                _SHARD_KIND,
                shard_id,
                BedrockGroupBuyAggregateShard,
                for_update=True,
            ) or empty_aggregate_shard(existing.aggregate_shard)
            updated_shard = apply_pledge_delta(
                shard,
                old=existing,
                new=None,
                now=now,
            )
            self._write_entity_tx(conn, _SHARD_KIND, shard_id, updated_shard)
            self._delete_entity_tx(conn, _PLEDGE_KIND, user_id)
            if existing.public_message_id:
                self._delete_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_KIND,
                    existing.public_message_id,
                )
            if existing.public_message_index_id:
                self._delete_entity_tx(
                    conn,
                    _PUBLIC_MESSAGE_INDEX_KIND,
                    existing.public_message_index_id,
                )
            return True

        return self._run_transaction(txn)

    def aggregate(self) -> BedrockGroupBuyAggregate:
        shards = self._list_entities(
            _SHARD_KIND,
            BedrockGroupBuyAggregateShard,
        )
        return sum_aggregate_shards(shards)

    def list_public_messages(self, *, limit: int = 50) -> list[BedrockGroupBuyPublicMessage]:
        pointers = self._list_entities(
            _PUBLIC_MESSAGE_INDEX_KIND,
            dict,
            limit=max(0, limit),
        )
        messages: list[BedrockGroupBuyPublicMessage] = []
        for pointer in pointers:
            message_id = str(pointer.get("message_id", ""))
            if not message_id:
                continue
            message = self._read_entity(
                _PUBLIC_MESSAGE_KIND,
                message_id,
                BedrockGroupBuyPublicMessage,
            )
            if message is not None:
                messages.append(message)
        return messages[:limit]

    def list_private_pledges(self, *, limit: int = 1000) -> list[BedrockGroupBuyPledge]:
        rows = self._list_entities(
            _PLEDGE_KIND,
            BedrockGroupBuyPledge,
            limit=max(0, limit),
        )
        rows.sort(key=lambda item: (item.updated_at, item.user_id), reverse=True)
        return rows[:limit]
