"""Every writer of an API key's spend configuration, held to a reviewed list.

A strict key's limit fields, and their tr_key_limit mirrors, never go from all
unset to any set (storage_key_patch.StrictKeyFirstLimit), and a strict key's
mirror row is never deleted while the key lives. tests/key_limit_writers.py
finds every place that could break that structurally: an in-process change
of a key's limit fields, any write to tr_entities (where api_key bodies
live) or tr_key_limit by mutation, payload or SQL in Python, shell, SQL or
Go, and every call of an entity writer whose kind is api_key or not a
literal. Each is listed below with why it cannot break it; a new one fails
until it is reviewed and listed, and a listed one that is gone fails too.
"""

from __future__ import annotations

import functools
import textwrap
from pathlib import Path

import pytest

from tests.key_limit_writers import ROOT, repository_paths, scan_paths
from tests.key_limit_writers import scan_repository as _scan_repository


@functools.cache
def scan_repository() -> frozenset[tuple[str, str, str]]:
    return frozenset(_scan_repository())

_PATCH = "the one guarded patch function (StrictKeyFirstLimit)"
_CREATE = "creates a new key, whose limits are its creation's"
_VIA_PATCH = "writes the key apply_key_patch returned, in the same transaction, the mirror from it"
_COUNTERS = "reads the key in its transaction and changes only usage, holds or disabled, never a limit field"
_GENERIC = (
    "a generic entity writer: every caller giving it api_key is listed as entity.api_key, every caller "
    "with a computed kind as entity.unresolved"
)
_OWN_KINDS = "writes only its own entity kinds, never api_key"
_DELETE = "deletes entities; a deleted key authorizes nothing (and a strict key's row is kept, SpannerApiKeys.delete)"
_COUNTER_DML = "updates usage, reserved or window usage only; never a limit column"
_REPARTITION = (
    "repartitions an existing limit across shards from the entity's own value; a strict key is refused "
    "before anything is written (inspect_key_usage_reshard, key_usage_shard_count)"
)
_FIXTURE = "builds keys for tests, golden vectors or a load test, never against production"
_POSTGRES = (
    "Postgres planes (AWS, Azure) only, which the GCP edge never decides; it mirrors the entity it is given "
    "after apply_key_patch or at creation"
)

ALLOWED_WRITERS: dict[tuple[str, str, str], str] = {
    # In-process changes of a key's limit fields.
    ("assign", "src/trusted_router/storage_key_patch.py", "_apply"): _PATCH,
    ("setattr", "src/trusted_router/storage_key_patch.py", "_apply"): _PATCH,
    ("assign", "src/trusted_router/routes/oauth_keys.py", "register_oauth_key_routes.oauth_authorize_approve.approve"): (
        "sets a consent's limit, from which a new key is created"
    ),
    ("replace", "scripts/fastpath/key_window_vectors.py", "_key"): _FIXTURE,
    # api_key entity writes, by kind.
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.create"): _CREATE,
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.metadata_txn"): (
        _VIA_PATCH + " (no limit field in the patch)"
    ),
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.txn"): _VIA_PATCH,
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.reserve_limit.txn"): _COUNTERS,
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.add_usage.txn"): _COUNTERS,
    ("entity.api_key", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys._release_limit.txn"): _COUNTERS,
    ("entity.api_key", "src/trusted_router/storage_legacy_trust.py", "create_spanner_legacy_authorization.create"): (
        _COUNTERS
    ),
    ("entity.api_key", "src/trusted_router/storage_account_closure.py", "_finish"): (
        "account closure: sets disabled on each key it read in the transaction"
    ),
    ("entity.api_key", "src/trusted_router/storage_postgres.py", "PostgresStore.create_api_key.create"): _CREATE,
    ("entity.api_key", "src/trusted_router/storage_postgres.py", "PostgresStore.update_key.update"): _VIA_PATCH,
    ("entity.api_key", "src/trusted_router/storage_postgres.py", "PostgresStore.upsert_federated_api_key.upsert"): (
        "a peer plane copies the home plane's record, where the rule holds; federated keys never reach the "
        "GCP edge (key.federated), and GCP imports none"
    ),
    ("entity.api_key", "scripts/stress_credit_shards.py", "_seed"): _FIXTURE,
    ("entity.api_key", "scripts/fastpath/authorize_vectors.py", "build_world"): _FIXTURE,
    ("entity.api_key", "scripts/fastpath/authorize_vectors.py", "_run_case"): _FIXTURE,
    # Entity writes whose kind is computed.
    ("entity.unresolved", "src/trusted_router/storage_account_closure.py", "close_spanner.txn"): (
        "passes _finish's kinds on: credit, api_key (disabled only) and user"
    ),
    ("entity.unresolved", "src/trusted_router/storage_account_closure.py", "close_postgres.txn"): (
        "passes _finish's kinds on: credit, api_key (disabled only) and user"
    ),
    ("entity.unresolved", "src/trusted_router/storage_gcp.py", "SpannerStore.reserved_resale.txn"): (
        "the NYTE resale ledger's own kinds (storage_resale), never api_key"
    ),
    ("entity.unresolved", "src/trusted_router/storage_postgres.py", "PostgresStore.reserved_resale.txn"): (
        "the NYTE resale ledger's own kinds (storage_resale), never api_key"
    ),
    ("entity.unresolved", "src/trusted_router/storage_gcp_authorize.py", "typed_finalize_atomic.txn"): (
        "a settle's request records (generation, activity), never api_key"
    ),
    ("entity.unresolved", "src/trusted_router/storage_postgres.py", "PostgresStore._consume_secret.consume"): (
        "verification tokens and OAuth codes, never api_key"
    ),
    # Writes to tr_entities.
    ("mutation.entities", "src/trusted_router/storage_gcp.py", "SpannerStore._write_entity_batch"): _GENERIC,
    ("mutation.entities", "src/trusted_router/storage_gcp.py", "SpannerStore._write_entity_tx"): _GENERIC,
    ("mutation.entities", "src/trusted_router/storage_gcp.py", "SpannerStore._write_receipt_key_tx"): (
        "receipt keys only"
    ),
    ("mutation.entities", "src/trusted_router/storage_gcp_google_ads.py",
     "SpannerGoogleAdsDeliveryStore._write_entity_batch"): "Google Ads delivery records only",
    ("mutation.entities", "src/trusted_router/storage_gcp_google_ads.py",
     "SpannerGoogleAdsDeliveryStore._write_entity_tx"): "Google Ads delivery records only",
    ("mutation.entities", "src/trusted_router/storage_gcp_async_admission.py", "_write_control"): _OWN_KINDS,
    ("mutation.entities", "src/trusted_router/storage_gcp_async_settle_shadow.py", "EvidenceStore.write"): _OWN_KINDS,
    ("mutation.entities", "src/trusted_router/storage_gcp_shadow_observation.py", "ObservationStore.write"): (
        _OWN_KINDS
    ),
    ("mutation.entities", "src/trusted_router/storage_gcp_shadow_observation.py",
     "ObservationStore.close_result.run"): _OWN_KINDS,
    ("mutation.entities", "src/trusted_router/storage_gcp_credit_shard_admin.py", "reshard_credit_account.txn"): (
        "credit accounts only"
    ),
    ("mutation.entities", "src/trusted_router/storage_gcp_key_shard_admin.py", "reshard_key_usage.txn"): (
        _REPARTITION
    ),
    ("mutation.entities", "scripts/consolidate_starter_credit.py", "mutations"): "credit accounts only",
    ("mutation.entities", "scripts/expand_billing_shards.py", "mutations"): (
        "credit and api_key bodies with usage_shard_count changed; refuses strict keys and any lifetime limit"
    ),
    ("mutation.entities", "scripts/deploy/replay_orphan_writes.py", "main"): (
        "replays every kind but api_key, which goes through replay_api_keys"
    ),
    ("mutation.entities", "scripts/deploy/replay_orphan_writes.py", "replay_api_keys.txn"): (
        "writes an api_key record only after reading the target's key in the same transaction, never one "
        "that changes a held key's spend configuration (api_key_replay_refusal)"
    ),
    ("sql.entities", "src/trusted_router/storage_gcp_counter_dml.py", "entity_insert_statement"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_gcp_counter_dml.py", "insert_entity_dml_at"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_gcp_counter_dml.py", "update_entity_body_dml"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_gcp_trust.py", "_sync_principal_recovery_pause_tx"): (
        "workspace bodies only (kind='workspace')"
    ),
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._write_entity_tx"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._write_indexed_entity_tx"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._insert_entity_once_tx"): _GENERIC,
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._insert_indexed_entity_once_tx"): (
        _GENERIC
    ),
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._write_receipt_key_tx"): (
        "receipt keys only"
    ),
    ("sql.entities", "src/trusted_router/storage_postgres.py", "PostgresStore._insert_receipt_key_once_tx"): (
        "receipt keys only"
    ),
    ("sql.entities", "src/trusted_router/storage_postgres.py",
     "PostgresStore.backfill_provider_benchmark_indexed_at_page.operation"): "provider_benchmark rows only",
    # Deletes of tr_entities rows.
    ("mutation.entities_delete", "src/trusted_router/storage_gcp.py", "SpannerStore._delete_entities"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp.py", "SpannerStore._delete_entities_tx"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp.py", "SpannerStore.attach_verified_email.txn"): (
        _DELETE
    ),
    ("mutation.entities_delete", "src/trusted_router/storage_gcp.py", "SpannerStore.session_auth_context"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp.py", "SpannerStore.set_user_email.txn"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp_async_settle_shadow.py",
     "EvidenceStore.cleanup.delete"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp_google_ads.py",
     "SpannerGoogleAdsDeliveryStore._delete_entities"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp_google_ads.py",
     "SpannerGoogleAdsDeliveryStore._delete_entities_tx"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp_keys.py",
     "SpannerApiKeys._delete_batch.delete_batch_txn"): _DELETE,
    ("mutation.entities_delete", "src/trusted_router/storage_gcp_shadow_observation.py",
     "ObservationStore.runtime_stop.run"): _DELETE,
    ("mutation.entities_delete", "scripts/cleanup_smoke_signups.py", "main.txn"): (
        "deletes synthetic smoke signups whole, key and rows together"
    ),
    ("sql.entities_delete", "src/trusted_router/storage_gcp_counter_dml.py", "delete_entity_dml"): _DELETE,
    ("sql.entities_delete", "src/trusted_router/storage_postgres.py", "PostgresStore._delete_entity_tx"): _DELETE,
    ("sql.entities_delete", "src/trusted_router/storage_postgres.py", "PostgresStore._delete_keys_batch.delete"): (
        _DELETE
    ),
    ("sql.entities_delete", "src/trusted_router/storage_postgres.py", "PostgresStore.forget_chat_browser_keys"): (
        "deletes expired chat browser key index rows"
    ),
    # Writes to tr_key_limit.
    ("mutation.key_limit", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.create"): _CREATE,
    ("mutation.key_limit", "src/trusted_router/storage_gcp_keys.py", "SpannerApiKeys.update.txn"): _VIA_PATCH,
    ("mutation.key_limit", "src/trusted_router/storage_gcp_key_shard_admin.py", "reshard_key_usage.txn"): _REPARTITION,
    ("mutation.key_limit", "src/trusted_router/storage_gcp_key_escrow.py", "rebalance_key_limit_headroom.txn"): (
        "moves an existing lifetime limit's headroom between a sharded key's shards; a strict key has one shard"
    ),
    ("mutation.key_limit", "src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn"): (
        "repairs reserved only, from the open holds"
    ),
    ("mutation.key_limit", "scripts/expand_billing_shards.py", "mutations"): (
        "inserts new shards copying the entity's window caps; refuses strict keys"
    ),
    ("mutation.key_limit", "scripts/stress_credit_shards.py", "_seed"): _FIXTURE,
    ("mutation.key_limit_delete", "src/trusted_router/storage_gcp_key_shard_admin.py", "reshard_key_usage.txn"): (
        "removes shards past a shrunk count; a strict key is refused before anything is written"
    ),
    ("mutation.key_limit_delete", "scripts/cleanup_smoke_signups.py", "main.txn"): (
        "deletes synthetic smoke signups whole, key and rows together"
    ),
    ("sql.key_limit", "src/trusted_router/storage_gcp_counter_dml.py", "reserve_key_statement"): _COUNTER_DML,
    ("sql.key_limit", "src/trusted_router/storage_gcp_counter_dml.py", "release_key_statement"): _COUNTER_DML,
    ("sql.key_limit", "src/trusted_router/storage_gcp_strict_budget.py", "reserve_strict_key"): _COUNTER_DML,
    ("sql.key_limit", "scripts/backfill_leased_key_usage.py", "update_sql"): _COUNTER_DML,
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"): _POSTGRES,
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore.create_api_key.create"): _POSTGRES,
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore.upsert_federated_api_key.upsert"): (
        _POSTGRES
    ),
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore._release_key_hold_tx"): _COUNTER_DML,
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore.finalize_gateway_authorization.finalize"): (
        _COUNTER_DML
    ),
    ("sql.key_limit", "src/trusted_router/storage_postgres.py", "PostgresStore.reserve_key_limit.reserve"): (
        _COUNTER_DML
    ),
    ("sql.key_limit_delete", "src/trusted_router/storage_postgres.py", "PostgresStore._delete_keys_batch.delete"): (
        _POSTGRES + "; deletes a deleted key's rows"
    ),
    # Mutations and DML whose table is computed: none is tr_entities or tr_key_limit.
    ("mutation.unresolved", "src/trusted_router/fastpath_enabler.py", "_apply"): (
        "applies the enabler's own fastpath table mutations (tr_fastpath_workspace)"
    ),
    ("sql.dynamic", "src/trusted_router/analytics_sink.py", "ClickHouseAnalyticsSink._post_rows"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/operational_analytics_direct.py", "DirectOperationalAnalyticsSink.flush"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/storage_gcp_federated_settlement.py", "_book_usage"): (
        "the credit balance table (federated settlement), never tr_key_limit's limits"
    ),
    ("sql.dynamic", "src/trusted_router/storage_gcp_settle_outbox.py", "done_retention_statements"): (
        "settle outbox retention"
    ),
}


def test_every_writer_of_a_keys_spend_configuration_is_known() -> None:
    found = scan_repository()
    unknown = sorted(found - ALLOWED_WRITERS.keys())
    assert not unknown, (
        "New writers of an API key's limits, the api_key entity, or tr_key_limit. Route limit changes through "
        "storage_key_patch.apply_key_patch (StrictKeyFirstLimit), then list each here with why it cannot give a "
        f"limitless strict key a limit or delete a live strict key's row: {unknown}"
    )


def test_the_writer_inventory_has_no_dead_entries() -> None:
    dead = sorted(ALLOWED_WRITERS.keys() - scan_repository())
    assert not dead, f"Listed writers no longer found; remove them: {dead}"


def test_the_scan_covers_python_shell_sql_and_go() -> None:
    python, text = repository_paths()
    assert any(p.name == "replay_orphan_writes.py" for p in python)
    assert any(p.suffix == ".sh" for p in text) and any(p.suffix == ".sql" for p in text)
    assert any(p.suffix == ".go" for p in text) and not any(p.name.endswith("_test.go") for p in text)


def _scan_sample(tmp_path: Path, name: str, source: str) -> set[tuple[str, str, str]]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    if path.suffix == ".py":
        return {(style, where) for style, _path, where in scan_paths([path], [])}  # type: ignore[misc]
    return {(style, where) for style, _path, where in scan_paths([], [path])}  # type: ignore[misc]


# One sample per detection style, in a file the scanner has never seen.
POSITIVE_CONTROLS = [
    ("assign", "def f(key):\n    key.limit_daily_microdollars = 5\n", {("assign", "f")}),
    ("setattr, f-string", "def f(key, w):\n    setattr(key, f'limit_{w}_microdollars', 5)\n", {("setattr", "f")}),
    ("setattr, variable",
     "def f(key, patch):\n    for name in ('limit_weekly_microdollars',):\n        setattr(key, name, patch[name])\n",
     {("setattr", "f")}),
    ("replace", "def f(key):\n    return replace(key, limit_microdollars=5)\n", {("replace", "f")}),
    ("mutation, literal table",
     "def f(batch, rows):\n    batch.insert_or_update(table='tr_entities', columns=('kind',), values=rows)\n",
     {("mutation.entities", "f")}),
    ("mutation, constant table",
     "LIMITS = 'tr_key_limit'\ndef f(txn, rows):\n    txn.update(table=LIMITS, columns=('limit_micro',), values=rows)\n",
     {("mutation.key_limit", "f")}),
    ("mutation, local variable table",
     "def f(txn, rows):\n    table = 'tr_key_limit'\n    txn.replace(table=table, columns=('x',), values=rows)\n",
     {("mutation.key_limit", "f")}),
    ("mutation, attribute table",
     "class S:\n    def f(self, txn, rows):\n        txn.insert(table=self.entity_table, columns=('kind',), values=rows)\n",
     {("mutation.entities", "S.f")}),
    ("mutation, positional table delete",
     "def f(txn, keyset):\n    txn.delete('tr_key_limit', keyset)\n", {("mutation.key_limit_delete", "f")}),
    ("mutation, unresolved table",
     "def f(txn, m):\n    txn.insert_or_update(table=m['table'], columns=m['c'], values=m['v'])\n",
     {("mutation.unresolved", "f")}),
    ("mutation payload",
     "def f(rows):\n    return [{'insert': {'table': 'tr_key_limit', 'columns': ['limit_micro'], 'values': rows}}]\n",
     {("mutation.key_limit", "f")}),
    ("read payload is no write",
     "def f(client):\n    return client.read(request={'table': 'tr_entities', 'columns': ['body'], 'key_set': {}})\n",
     set()),
    ("SQL insert with cap columns",
     "def f(conn):\n    conn.execute('INSERT INTO tr_key_limit (key_hash, shard, limit_micro) VALUES (%s, 0, %s)')\n",
     {("sql.key_limit", "f")}),
    ("lowercase SQL update",
     "def f(conn):\n    conn.execute(\"update tr_key_limit set day_limit_micro = 5 where key_hash = 'k'\")\n",
     {("sql.key_limit", "f")}),
    ("SQL with a lock hint",
     "def f(t):\n    t.execute_update('@{LOCK_SCANNED_RANGES=EXCLUSIVE} UPDATE tr_key_limit SET reserved = 0')\n",
     {("sql.key_limit", "f")}),
    ("insert or update DML on entities",
     "def f(t):\n    t.execute_update(\"INSERT OR UPDATE tr_entities (kind, id, body) VALUES ('api_key', @i, @b)\")\n",
     {("sql.entities", "f")}),
    ("f-string SQL, dynamic table",
     "def f(t, table):\n    t.execute_update(f'UPDATE {table} SET body = @b WHERE id = @i')\n",
     {("sql.dynamic", "f")}),
    ("entity helper, computed kind",
     "class S:\n    def _write_entity_tx(self, tx, kind, key, value):\n"
     "        tx.insert_or_update(table='tr_entities', columns=('kind',), values=[(kind,)])\n"
     "    def g(self, tx, rows):\n        for kind, key, value in rows:\n"
     "            self._write_entity_tx(tx, kind, key, value)\n",
     {("mutation.entities", "S._write_entity_tx"), ("entity.unresolved", "S.g")}),
    ("entity helper, api_key",
     "class S:\n    def _write_entity_tx(self, tx, kind, key, value):\n"
     "        tx.insert_or_update(table='tr_entities', columns=('kind',), values=[(kind,)])\n"
     "    def g(self, tx, key):\n        self._write_entity_tx(tx, 'api_key', key.hash, key)\n",
     {("mutation.entities", "S._write_entity_tx"), ("entity.api_key", "S.g")}),
    ("entity helper, passed on through a wrapper",
     "class S:\n    def _write_entity_tx(self, tx, kind, key, value):\n"
     "        tx.insert_or_update(table='tr_entities', columns=('kind',), values=[(kind,)])\n"
     "    def wrap(self, tx, kind, key, value):\n        self._write_entity_tx(tx, kind, key, value)\n"
     "    def g(self, tx, key):\n        self.wrap(tx, 'api_key', key.hash, key)\n",
     {("mutation.entities", "S._write_entity_tx"), ("entity.api_key", "S.g")}),
    ("write-named call given api_key", "def f(io, b, key):\n    io.write_entity_batch(b, 'api_key', key.hash, key)\n",
     {("entity.api_key", "f")}),
    ("a file write is no entity write",
     "class S:\n    def write(self, tx, kind):\n        tx.insert(table='tr_entities', columns=('kind',), values=[(kind,)])\n"
     "def g(handle, text):\n    handle.write(text)\n",
     {("mutation.entities", "S.write")}),
]


@pytest.mark.parametrize(("name", "source", "expected"), POSITIVE_CONTROLS, ids=[c[0] for c in POSITIVE_CONTROLS])
def test_each_detection_style_finds_its_sample(tmp_path: Path, name: str, source: str, expected: set) -> None:
    assert _scan_sample(tmp_path, "sample.py", source) == expected, name


@pytest.mark.parametrize(("name", "suffix", "source", "expected"), [
    ("shell DML", ".sh", "gcloud spanner databases execute-sql db --sql=\"UPDATE tr_key_limit SET limit_micro=1\"\n",
     {("sql.key_limit", "<file>")}),
    ("SQL file DML", ".sql", "delete from tr_key_limit where key_hash = 'k';\n", {("sql.key_limit_delete", "<file>")}),
    ("Go mutation", ".go", "m := spanner.InsertOrUpdate(\"tr_key_limit\", cols, vals)\n",
     {("mutation.key_limit", "<file>")}),
    ("Go DML", ".go", "stmt := spanner.Statement{SQL: `UPDATE tr_entities SET body = @b WHERE kind = 'api_key'`}\n",
     {("sql.entities", "<file>")}),
    ("DDL is no write", ".sh", "--ddl='CREATE TABLE tr_key_limit (key_hash STRING(64)) PRIMARY KEY (key_hash)'\n",
     set()),
])
def test_each_text_detection_style_finds_its_sample(tmp_path: Path, name: str, suffix: str, source: str,
                                                    expected: set) -> None:
    assert _scan_sample(tmp_path, "sample" + suffix, source) == expected, name


# The replay script's write step before StrictKeyFirstLimit's guard reached
# it, verbatim: every kind, api_key among them, upserted whole.
REPLAY_BEFORE_THE_GUARD = '''
def main() -> int:
    replay_rows = [r for r in rows if r[0] not in SKIP_KINDS]
    BATCH = 1000
    written = 0
    for i in range(0, len(replay_rows), BATCH):
        batch = replay_rows[i : i + BATCH]
        with target.batch() as txn:
            txn.insert_or_update(
                table="tr_entities",
                columns=("kind", "id", "body", "updated_at"),
                values=batch,
            )
        written += len(batch)
'''


def test_the_scanner_flags_the_replay_as_it_was(tmp_path: Path) -> None:
    """The writer the first inventory missed: a whole-row upsert of every
    kind into tr_entities, its kinds data, not literals."""
    assert _scan_sample(tmp_path, "replay_orphan_writes.py", REPLAY_BEFORE_THE_GUARD) == {
        ("mutation.entities", "main")}
    # And the real script, guarded now, is still found, both of its writes.
    found = {(style, where) for style, path, where in scan_repository()
             if path == "scripts/deploy/replay_orphan_writes.py"}
    assert found == {("mutation.entities", "main"), ("mutation.entities", "replay_api_keys.txn")}


def test_the_root_is_the_repository() -> None:
    assert (ROOT / "src" / "trusted_router" / "storage_key_patch.py").is_file()
