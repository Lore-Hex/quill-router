from __future__ import annotations

import dataclasses
import hashlib
import threading
import uuid
from dataclasses import dataclass

from trusted_router.storage_models import (
    BedrockGroupBuyAggregate,
    BedrockGroupBuyAggregateShard,
    BedrockGroupBuyPledge,
    BedrockGroupBuyPublicMessage,
    iso_now,
)

BEDROCK_GROUP_BUY_SHARD_COUNT = 32


def bedrock_group_buy_shard(user_id: str) -> int:
    digest = hashlib.sha256(f"bedrock-group-buy:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % BEDROCK_GROUP_BUY_SHARD_COUNT


def new_public_message_id() -> str:
    return f"bgm_{uuid.uuid4().hex}"


def public_message_index_id(*, updated_at: str, message_id: str) -> str:
    # ISO timestamps sort chronologically. Inverting epoch microseconds makes
    # an ordinary ascending entity scan return newest comments first.
    import datetime as dt

    parsed = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    micros = int(parsed.timestamp() * 1_000_000)
    reverse = 9_999_999_999_999_999 - micros
    return f"{reverse:016d}#{message_id}"


def empty_aggregate_shard(shard_id: int) -> BedrockGroupBuyAggregateShard:
    return BedrockGroupBuyAggregateShard(shard_id=shard_id)


def apply_pledge_delta(
    shard: BedrockGroupBuyAggregateShard,
    *,
    old: BedrockGroupBuyPledge | None,
    new: BedrockGroupBuyPledge | None,
    now: str,
) -> BedrockGroupBuyAggregateShard:
    updated = dataclasses.replace(shard)
    if old is not None:
        updated.active_pledge_count -= 1
        updated.monthly_minimum_microdollars -= old.monthly_minimum_microdollars
        updated.expected_bedrock_monthly_microdollars -= (
            old.expected_bedrock_monthly_microdollars
        )
        updated.expected_all_llm_monthly_microdollars -= (
            old.expected_all_llm_monthly_microdollars
        )
    if new is not None:
        updated.active_pledge_count += 1
        updated.monthly_minimum_microdollars += new.monthly_minimum_microdollars
        updated.expected_bedrock_monthly_microdollars += (
            new.expected_bedrock_monthly_microdollars
        )
        updated.expected_all_llm_monthly_microdollars += (
            new.expected_all_llm_monthly_microdollars
        )
    if min(
        updated.active_pledge_count,
        updated.monthly_minimum_microdollars,
        updated.expected_bedrock_monthly_microdollars,
        updated.expected_all_llm_monthly_microdollars,
    ) < 0:
        raise RuntimeError("Bedrock group-buy aggregate would become negative")
    updated.updated_at = now
    return updated


@dataclass(frozen=True)
class PreparedPledgeMutation:
    pledge: BedrockGroupBuyPledge
    public_message: BedrockGroupBuyPublicMessage | None
    old_public_message_id: str
    old_public_message_index_id: str


def prepare_pledge_mutation(
    *,
    incoming: BedrockGroupBuyPledge,
    existing: BedrockGroupBuyPledge | None,
    candidate_public_message_id: str,
    now: str,
) -> PreparedPledgeMutation:
    if incoming.monthly_minimum_microdollars <= 0:
        raise ValueError("monthly minimum must be positive")
    if incoming.aggregate_shard != bedrock_group_buy_shard(incoming.user_id):
        raise ValueError("invalid aggregate shard")
    if existing is not None and existing.aggregate_shard != incoming.aggregate_shard:
        raise RuntimeError("Bedrock group-buy pledge changed aggregate shard")

    public_id = ""
    public_index_id = ""
    public_projection = None
    if incoming.publish_message and incoming.public_message:
        public_id = (
            existing.public_message_id
            if existing is not None and existing.public_message_id
            else candidate_public_message_id
        )
        public_index_id = public_message_index_id(updated_at=now, message_id=public_id)
        public_projection = BedrockGroupBuyPublicMessage(
            id=public_id,
            message=incoming.public_message,
            created_at=(
                existing.created_at
                if existing is not None and existing.public_message_id
                else now
            ),
            updated_at=now,
        )

    pledge = dataclasses.replace(
        incoming,
        public_message_id=public_id,
        public_message_index_id=public_index_id,
        created_at=existing.created_at if existing is not None else now,
        accepted_at=existing.accepted_at if existing is not None else incoming.accepted_at,
        updated_at=now,
    )
    return PreparedPledgeMutation(
        pledge=pledge,
        public_message=public_projection,
        old_public_message_id=existing.public_message_id if existing is not None else "",
        old_public_message_index_id=(
            existing.public_message_index_id if existing is not None else ""
        ),
    )


def sum_aggregate_shards(
    shards: list[BedrockGroupBuyAggregateShard],
) -> BedrockGroupBuyAggregate:
    return BedrockGroupBuyAggregate(
        active_pledge_count=sum(item.active_pledge_count for item in shards),
        monthly_minimum_microdollars=sum(
            item.monthly_minimum_microdollars for item in shards
        ),
        expected_bedrock_monthly_microdollars=sum(
            item.expected_bedrock_monthly_microdollars for item in shards
        ),
        expected_all_llm_monthly_microdollars=sum(
            item.expected_all_llm_monthly_microdollars for item in shards
        ),
    )


class InMemoryBedrockGroupBuy:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.pledges: dict[str, BedrockGroupBuyPledge] = {}
        self.shards: dict[int, BedrockGroupBuyAggregateShard] = {}
        self.public_messages: dict[str, BedrockGroupBuyPublicMessage] = {}

    def reset(self) -> None:
        with self._lock:
            self.pledges.clear()
            self.shards.clear()
            self.public_messages.clear()

    def upsert(self, incoming: BedrockGroupBuyPledge) -> BedrockGroupBuyPledge:
        with self._lock:
            existing = self.pledges.get(incoming.user_id)
            now = iso_now()
            mutation = prepare_pledge_mutation(
                incoming=incoming,
                existing=existing,
                candidate_public_message_id=new_public_message_id(),
                now=now,
            )
            shard = self.shards.get(
                incoming.aggregate_shard,
                empty_aggregate_shard(incoming.aggregate_shard),
            )
            self.shards[incoming.aggregate_shard] = apply_pledge_delta(
                shard,
                old=existing,
                new=mutation.pledge,
                now=now,
            )
            if mutation.old_public_message_id:
                self.public_messages.pop(mutation.old_public_message_id, None)
            if mutation.public_message is not None:
                self.public_messages[mutation.public_message.id] = mutation.public_message
            self.pledges[incoming.user_id] = mutation.pledge
            return dataclasses.replace(mutation.pledge)

    def get(self, user_id: str) -> BedrockGroupBuyPledge | None:
        with self._lock:
            record = self.pledges.get(user_id)
            return dataclasses.replace(record) if record is not None else None

    def withdraw(self, user_id: str) -> bool:
        with self._lock:
            existing = self.pledges.pop(user_id, None)
            if existing is None:
                return False
            now = iso_now()
            shard = self.shards.get(
                existing.aggregate_shard,
                empty_aggregate_shard(existing.aggregate_shard),
            )
            self.shards[existing.aggregate_shard] = apply_pledge_delta(
                shard,
                old=existing,
                new=None,
                now=now,
            )
            if existing.public_message_id:
                self.public_messages.pop(existing.public_message_id, None)
            return True

    def aggregate(self) -> BedrockGroupBuyAggregate:
        with self._lock:
            return sum_aggregate_shards(list(self.shards.values()))

    def list_public_messages(self, *, limit: int = 50) -> list[BedrockGroupBuyPublicMessage]:
        with self._lock:
            rows = sorted(
                self.public_messages.values(),
                key=lambda item: (item.updated_at, item.id),
                reverse=True,
            )
            return [dataclasses.replace(item) for item in rows[:limit]]

    def list_private_pledges(self, *, limit: int = 1000) -> list[BedrockGroupBuyPledge]:
        with self._lock:
            rows = sorted(
                self.pledges.values(),
                key=lambda item: (item.updated_at, item.user_id),
                reverse=True,
            )
            return [dataclasses.replace(item) for item in rows[:limit]]
