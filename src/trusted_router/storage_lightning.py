"""Atomic, zero-grant Lightning identities and immutable USD deposit bindings.

No BTC balance lives here. A binding may precede the USD credit transaction;
that is intentional: retries can finish delivery but cannot change its target.
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import TYPE_CHECKING, Any

from trusted_router.security import (
    hash_api_key,
    key_label,
    lookup_hash_api_key,
    new_hash_salt,
    new_key_id,
    verify_api_key,
)
from trusted_router.storage_errors import StoreConflict
from trusted_router.storage_models import ApiKey, CreditAccount, Member, User, Workspace

if TYPE_CHECKING:
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_postgres import PostgresStore


# Coalesce same-key browser retries without an unbounded per-key lock cache.
# This only protects connection capacity; the database claim remains the
# correctness boundary between independent processes and replicas.
_PROVISION_LOCKS = tuple(threading.Lock() for _ in range(128))


def validate_raw_key(raw: str) -> str:
    if not re.fullmatch(r"sk-tr-v1-[A-Za-z0-9_-]{43}", raw):
        raise ValueError("invalid_lightning_key")
    return lookup_hash_api_key(raw)


def key_record(raw: str, workspace: Workspace) -> ApiKey:
    salt = new_hash_salt()
    return ApiKey(
        hash=new_key_id(), salt=salt, secret_hash=hash_api_key(raw, salt),
        lookup_hash=validate_raw_key(raw), label=key_label(raw), name="LightningRouter",
        workspace_id=workspace.id, creator_user_id=workspace.owner_user_id,
        management=False,
    )


def existing_key(raw: str, key: ApiKey | None) -> ApiKey:
    from trusted_router.auth import is_api_key_expired

    if key is None or not verify_api_key(raw, key.salt, key.secret_hash):
        raise ValueError("invalid_lightning_key")
    if key.disabled or is_api_key_expired(key.expires_at) or key.federated_home:
        raise ValueError("invalid_lightning_key")
    return key


def payment_binding(workspace_id: str, payment_hash: str, amount: int) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", payment_hash):
        raise ValueError("invalid_lightning_payment_hash")
    if not workspace_id or len(workspace_id) > 128:
        raise ValueError("invalid_lightning_workspace")
    if type(amount) is not int or not 0 < amount <= 1_000_000_000:
        raise ValueError("invalid_lightning_credit_amount")
    return {"workspace_id": workspace_id, "amount_microdollars": amount}


def check_binding(existing: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    if existing is not None and existing != expected:
        raise ValueError("lightning_payment_conflict")


def memory_key(store: InMemoryStore, raw: str) -> ApiKey:
    lookup = validate_raw_key(raw)
    with store._lock:
        # The independent lookup remains after API-key deletion: a revoked
        # Lightning identity must never be resurrected by new_account=true.
        key_id = store.lightning_keys.get(lookup)
        prior = store.get_key_by_raw(raw)
        if key_id is not None or prior is not None:
            key = existing_key(raw, store.get_key_by_hash(key_id) if key_id else prior)
            store.lightning_keys[lookup] = key.hash
            return key
        user = User(id=str(uuid.uuid4()), email=None)
        store.users[user.id] = user
        workspace = store.create_workspace(user.id, "LightningRouter", trial_credit_microdollars=0)
        _, key = store.create_api_key(
            workspace_id=workspace.id, name="LightningRouter", creator_user_id=user.id,
            raw_key=raw, management=False,
        )
        store.lightning_keys[lookup] = key.hash
        return key


def memory_bind(store: InMemoryStore, workspace_id: str, payment_hash: str, amount: int) -> None:
    binding = payment_binding(workspace_id, payment_hash, amount)
    with store._lock:
        if workspace_id not in store.credits:
            raise ValueError("credit account not found")
        check_binding(store.lightning_payments.get(payment_hash), binding)
        store.lightning_payments[payment_hash] = binding


def spanner_key(store: SpannerBigtableStore, raw: str) -> ApiKey:
    from trusted_router.storage_gcp_codec import member_id, workspace_key_id
    from trusted_router.storage_gcp_counters import (
        DEFAULT_NEW_BILLING_SHARDS,
        KEY_LIMIT_COLUMNS,
        KEY_LIMIT_TABLE,
        key_limit_mirror_rows,
    )

    lookup = validate_raw_key(raw)

    def txn(transaction: Any) -> ApiKey:
        prior = store._read_entity_tx(transaction, "lightning_key", lookup, dict)
        prior = prior or store._read_entity_tx(transaction, "api_key_lookup", lookup, dict)
        if prior:
            key = existing_key(raw, store._read_entity_tx(transaction, "api_key", prior["key_id"], ApiKey))
            store._write_entity_tx(transaction, "lightning_key", lookup, {"key_id": key.hash})
            return key
        user = User(id=str(uuid.uuid4()), email=None, owner_workspace_count=1)
        workspace = Workspace(id=str(uuid.uuid4()), name="LightningRouter", owner_user_id=user.id)
        credit = CreditAccount(workspace_id=workspace.id, shard_count=DEFAULT_NEW_BILLING_SHARDS)
        key = key_record(raw, workspace)
        for kind, entity_id, value in (
            ("user", user.id, user), ("workspace", workspace.id, workspace),
            ("member", member_id(workspace.id, user.id), Member(workspace.id, user.id, "owner")),
            ("credit", workspace.id, credit), ("api_key", key.hash, key),
            ("api_key_lookup", lookup, {"key_id": key.hash}),
            ("lightning_key", lookup, {"key_id": key.hash}),
            ("api_key_by_workspace", workspace_key_id(workspace.id, key.hash), {"key_id": key.hash}),
        ):
            store._write_entity_tx(transaction, kind, entity_id, value)
        store._seed_credit_balance_on_create(transaction, workspace.id, 0, shard_count=credit.shard_count)
        store._insert_owner_inventory_tx(transaction, user.id, workspace.id)
        transaction.insert_or_update(
            table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS,
            values=key_limit_mirror_rows(key.hash, key, store._spanner.COMMIT_TIMESTAMP),
        )
        return key

    return store._run_in_transaction(txn)


def spanner_bind(store: SpannerBigtableStore, workspace_id: str, payment_hash: str, amount: int) -> None:
    binding = payment_binding(workspace_id, payment_hash, amount)

    def txn(transaction: Any) -> None:
        if store._read_entity_tx(transaction, "credit", workspace_id, CreditAccount) is None:
            raise ValueError("credit account not found")
        existing = store._read_entity_tx(transaction, "lightning_payment", payment_hash, dict)
        check_binding(existing, binding)
        if existing is None:
            store._write_entity_tx(transaction, "lightning_payment", payment_hash, binding)

    store._run_in_transaction(txn)


def postgres_key(store: PostgresStore, raw: str) -> ApiKey:
    from trusted_router.storage_gcp_codec import workspace_key_id

    lookup = validate_raw_key(raw)

    def txn(conn: Any) -> ApiKey:
        claim = store._read_entity_tx(conn, "lightning_key", lookup, dict)
        if claim is not None:
            return existing_key(raw, store._read_entity_tx(conn, "api_key", claim["key_id"], ApiKey))
        # Unique insertion serializes competing creators even when SELECT FOR
        # UPDATE would find no row. The claim rolls back with account creation.
        key_id = new_key_id()
        if not store._insert_entity_once_tx(conn, "lightning_key", lookup, {"key_id": key_id}):
            claim = store._read_entity_tx(conn, "lightning_key", lookup, dict)
            assert claim is not None
            return existing_key(raw, store._read_entity_tx(conn, "api_key", claim["key_id"], ApiKey))
        prior = store._read_entity_tx(conn, "api_key_lookup", lookup, dict)
        if prior:
            key = existing_key(raw, store._read_entity_tx(conn, "api_key", prior["key_id"], ApiKey))
            store._write_entity_tx(conn, "lightning_key", lookup, {"key_id": key.hash})
            return key
        user = User(id=str(uuid.uuid4()), email=None)
        store._write_entity_tx(conn, "user", user.id, user)
        workspace = store._create_workspace_tx(conn, user.id, "LightningRouter", 0)
        key = key_record(raw, workspace)
        key.hash = key_id
        store._write_entity_tx(conn, "api_key", key.hash, key)
        store._write_entity_tx(conn, "api_key_lookup", lookup, {"key_id": key.hash})
        store._write_entity_tx(conn, "api_key_by_workspace", workspace_key_id(workspace.id, key.hash), {"key_id": key.hash})
        conn.execute(
            "INSERT INTO tr_key_limit (workspace_id, key_hash, shard, source_updated_at, updated_at) "
            "VALUES (%s, %s, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (workspace.id, key.hash),
        )
        store._write_key_limit_caps_tx(conn, key)
        return key

    lock = _PROVISION_LOCKS[int(lookup[:8], 16) % len(_PROVISION_LOCKS)]
    if not lock.acquire(timeout=10):
        raise StoreConflict("Lightning key provisioning is busy; retry the same key")
    try:
        return store._run_transaction(txn)
    finally:
        lock.release()


def postgres_bind(store: PostgresStore, workspace_id: str, payment_hash: str, amount: int) -> None:
    binding = payment_binding(workspace_id, payment_hash, amount)

    def txn(conn: Any) -> None:
        if store._read_entity_tx(conn, "credit", workspace_id, CreditAccount) is None:
            raise ValueError("credit account not found")
        if not store._insert_entity_once_tx(conn, "lightning_payment", payment_hash, binding):
            existing = store._read_entity_tx(conn, "lightning_payment", payment_hash, dict)
            check_binding(existing, binding)

    store._run_transaction(txn)
