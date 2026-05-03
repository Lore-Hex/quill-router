from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from tests.fakes.spanner import make_fake_store
from trusted_router.storage import ApiKey, Generation, ProviderBenchmarkSample
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_activity_index import (
    activity_generations as _bt_activity_generations,
)
from trusted_router.storage_gcp_activity_index import (
    write_generation as _bt_write_generation,
)
from trusted_router.storage_gcp_benchmark_index import (
    provider_benchmark_samples as _bt_provider_benchmark_samples,
)
from trusted_router.storage_gcp_benchmark_index import (
    write_provider_benchmark as _bt_write_provider_benchmark,
)
from trusted_router.storage_gcp_codec import reverse_time_key as _reverse_time_key


def _api_key(key_hash: str, workspace_id: str, created_at: str) -> ApiKey:
    return ApiKey(
        hash=key_hash,
        salt="salt",
        secret_hash=f"digest-{key_hash}",  # noqa: S106 - placeholder test digest.
        lookup_hash=f"lookup-{key_hash}",
        name=key_hash,
        label="sk-tr...abcd",
        workspace_id=workspace_id,
        creator_user_id=None,
        created_at=created_at,
    )


def _generation(generation_id: str, workspace_id: str, created_at: str) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req-{generation_id}",
        workspace_id=workspace_id,
        key_hash="key_1",
        model="openai/gpt-4o-mini",
        provider_name="OpenAI",
        app="test",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=100,
        usage_type="Credits",
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at=created_at,
    )


def test_gcp_list_keys_uses_workspace_index() -> None:
    """list_keys must read the api_key_by_workspace index, not scan every
    api_key row. Asserts the prefix shape SpannerApiKeys passes to
    _list_entities."""
    from trusted_router.storage_gcp_io import SpannerIO
    from trusted_router.storage_gcp_keys import SpannerApiKeys

    key = _api_key("key_1", "ws_1", "2026-05-02T10:00:00Z")
    other = _api_key("key_2", "ws_2", "2026-05-02T11:00:00Z")
    calls: list[tuple[str, str | None]] = []

    def list_entities(kind: str, *, cls: type[Any], prefix: str | None = None, suffix: str | None = None):
        calls.append((kind, prefix))
        assert suffix is None
        assert cls is dict
        assert kind == "api_key_by_workspace"
        assert prefix == "ws_1#"
        return [{"key_id": "key_1"}, {"key_id": "missing"}, {"key_id": "key_2"}]

    def read_entity(kind: str, entity_id: str, cls: type[Any]) -> Any:
        return {"key_1": key, "key_2": other}.get(entity_id) if kind == "api_key" else None

    io = SpannerIO(
        database=None,
        write_entity_batch=lambda *_a, **_kw: None,
        read_entity_tx=lambda *_a, **_kw: None,
        write_entity_tx=lambda *_a, **_kw: None,
        write_entity=lambda *_a, **_kw: None,
        read_entity=read_entity,
        list_entities=list_entities,
        delete_entities=lambda *_a, **_kw: None,
        delete_entities_tx=lambda *_a, **_kw: None,
    )
    api_keys = SpannerApiKeys(io)

    assert api_keys.list_for_workspace("ws_1") == [key]
    assert calls == [("api_key_by_workspace", "ws_1#")]


def test_gcp_api_key_lookup_uses_index_and_never_stores_raw_key() -> None:
    store, db, _ = make_fake_store()

    raw, api_key = store.create_api_key(
        workspace_id="ws_1",
        name="indexed",
        creator_user_id="user_1",
        raw_key="sk-tr-v1-indexed-raw-secret",
    )

    assert store.get_key_by_raw(raw) == api_key
    assert store.get_key_by_raw(raw + "-wrong") is None
    assert ("api_key_lookup", api_key.lookup_hash) in db.rows
    assert ("api_key_by_workspace", f"ws_1#{api_key.hash}") in db.rows
    serialized = "\n".join(row.body for row in db.rows.values())
    assert raw not in serialized


def test_gcp_byok_upsert_updates_secret_ref_and_hint() -> None:
    store, db, _ = make_fake_store()

    first = store.upsert_byok_provider(
        workspace_id="ws_1",
        provider="mistral",
        secret_ref="secretmanager://old",  # noqa: S106 - placeholder secret ref.
        key_hint="mis...old",
    )
    second = store.upsert_byok_provider(
        workspace_id="ws_1",
        provider="mistral",
        secret_ref="secretmanager://new",  # noqa: S106 - placeholder secret ref.
        key_hint="mis...new",
    )

    assert first.workspace_id == "ws_1"
    assert second.secret_ref == "secretmanager://new"  # noqa: S105 - placeholder secret ref.
    assert second.key_hint == "mis...new"
    stored = json.loads(db.rows[("byok", "ws_1#mistral")].body)
    assert stored["secret_ref"] == "secretmanager://new"  # noqa: S105 - placeholder secret ref.
    assert stored["updated_at"] is not None


def test_gcp_verification_tokens_are_one_time_wrong_purpose_safe_and_hash_only() -> None:
    store, db, _ = make_fake_store()

    raw, token = store.create_verification_token(user_id="user_1", purpose="signup", ttl_seconds=60)

    assert store.consume_verification_token(raw, purpose="login") is None
    consumed = store.consume_verification_token(raw, purpose="signup")
    replay = store.consume_verification_token(raw, purpose="signup")

    assert consumed is not None
    assert consumed.hash == token.hash
    assert consumed.consumed_at is not None
    assert replay is None
    assert ("verification_token_lookup", token.lookup_hash) in db.rows
    serialized = "\n".join(row.body for row in db.rows.values())
    assert raw not in serialized


def test_gcp_wallet_challenge_is_one_time_and_hash_only() -> None:
    store, db, _ = make_fake_store()

    raw, challenge = store.create_wallet_challenge(
        address="0x" + "a" * 40,
        message="Sign in to TrustedRouter",
        ttl_seconds=60,
        raw_nonce="nonce-secret",
    )

    consumed = store.consume_wallet_challenge(raw)
    replay = store.consume_wallet_challenge(raw)

    assert consumed is not None
    assert consumed.hash == challenge.hash
    assert consumed.address == "0x" + "a" * 40
    assert consumed.consumed_at is not None
    assert replay is None
    assert raw not in "\n".join(row.body for row in db.rows.values())


def test_gcp_rate_limit_counts_in_same_window_and_resets_later() -> None:
    import datetime as dt

    store, _db, _ = make_fake_store()
    now = dt.datetime(2026, 5, 2, 12, 0, 1, tzinfo=dt.UTC)

    first = store.hit_rate_limit(namespace="ip", subject="1.2.3.4", limit=2, window_seconds=60, now=now)
    second = store.hit_rate_limit(namespace="ip", subject="1.2.3.4", limit=2, window_seconds=60, now=now)
    third = store.hit_rate_limit(namespace="ip", subject="1.2.3.4", limit=2, window_seconds=60, now=now)
    next_window = store.hit_rate_limit(
        namespace="ip",
        subject="1.2.3.4",
        limit=2,
        window_seconds=60,
        now=now + dt.timedelta(seconds=61),
    )

    assert first.allowed is True and first.remaining == 1
    assert second.allowed is True and second.remaining == 0
    assert third.allowed is False and third.retry_after_seconds > 0
    assert next_window.allowed is True and next_window.remaining == 1


class _FakeCell:
    def __init__(self, value: Any) -> None:
        self.value = json.dumps(asdict(value), separators=(",", ":"), sort_keys=True).encode()


class _FakeReadRow:
    def __init__(self, value: Any) -> None:
        self.cells = {"m": {b"body": [_FakeCell(value)]}}


class _FakeDirectRow:
    def __init__(self, key: bytes, committed: list[bytes]) -> None:
        self.key = key
        self.committed = committed

    def set_cell(self, *_args: Any) -> None:
        return None

    def commit(self) -> None:
        self.committed.append(self.key)


class _FakeBigtable:
    def __init__(
        self,
        rows: list[_FakeReadRow] | None = None,
        read_batches: list[list[_FakeReadRow]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.read_batches = read_batches or []
        self.reads: list[tuple[bytes, bytes, int]] = []
        self.committed: list[bytes] = []

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int):
        self.reads.append((start_key, end_key, limit))
        if self.read_batches:
            return self.read_batches.pop(0)[:limit]
        return self.rows[:limit]

    def direct_row(self, key: bytes) -> _FakeDirectRow:
        return _FakeDirectRow(key, self.committed)


def test_gcp_bigtable_activity_without_date_uses_recent_multi_day_index() -> None:
    newer = _generation("gen_new", "ws_1", "2026-05-02T12:00:00Z")
    older = _generation("gen_old", "ws_1", "2026-04-30T12:00:00Z")
    table = _FakeBigtable([_FakeReadRow(older), _FakeReadRow(newer)])

    rows = _bt_activity_generations(table, "m", "ws_1", api_key_hash=None, date=None, limit=100)

    assert [row.id for row in rows] == ["gen_new", "gen_old"]
    assert table.reads == [(b"ws_recent#ws_1#", b"ws_recent#ws_1#~", 100)]


def test_gcp_bigtable_activity_with_date_uses_daily_index() -> None:
    generation = _generation("gen_1", "ws_1", "2026-05-02T12:00:00Z")
    table = _FakeBigtable([_FakeReadRow(generation)])

    rows = _bt_activity_generations(
        table, "m", "ws_1", api_key_hash=None, date="2026-05-02", limit=50
    )

    assert [row.id for row in rows] == ["gen_1"]
    assert table.reads == [(b"ws#ws_1#2026-05-02#", b"ws#ws_1#2026-05-02#~", 50)]


def test_gcp_bigtable_activity_falls_back_to_legacy_workspace_index() -> None:
    generation = _generation("gen_legacy", "ws_1", "2026-05-02T12:00:00Z")
    table = _FakeBigtable([])

    def read_rows(*, start_key: bytes, end_key: bytes, limit: int):
        table.reads.append((start_key, end_key, limit))
        if start_key == b"ws#ws_1#":
            return [_FakeReadRow(generation)]
        return []

    table.read_rows = read_rows  # type: ignore[method-assign]

    rows = _bt_activity_generations(table, "m", "ws_1", api_key_hash=None, date=None, limit=25)

    assert [row.id for row in rows] == ["gen_legacy"]
    assert table.reads == [
        (b"ws_recent#ws_1#", b"ws_recent#ws_1#~", 25),
        (b"ws#ws_1#", b"ws#ws_1#~", 25),
    ]


def test_gcp_bigtable_activity_filters_key_and_sorts_recent_rows() -> None:
    newest = _generation("gen_newest", "ws_1", "2026-05-03T12:00:00Z")
    middle_other_key = _generation("gen_other", "ws_1", "2026-05-02T12:00:00Z")
    middle_other_key.key_hash = "key_2"
    oldest = _generation("gen_oldest", "ws_1", "2026-05-01T12:00:00Z")
    table = _FakeBigtable(
        [
            _FakeReadRow(oldest),
            _FakeReadRow(middle_other_key),
            _FakeReadRow(newest),
        ]
    )

    rows = _bt_activity_generations(table, "m", "ws_1", api_key_hash="key_1", date=None, limit=10)

    assert [row.id for row in rows] == ["gen_newest", "gen_oldest"]


def test_gcp_bigtable_activity_respects_limit_after_sorting() -> None:
    newest = _generation("gen_newest", "ws_1", "2026-05-03T12:00:00Z")
    oldest = _generation("gen_oldest", "ws_1", "2026-05-01T12:00:00Z")
    table = _FakeBigtable([_FakeReadRow(newest), _FakeReadRow(oldest)])

    rows = _bt_activity_generations(table, "m", "ws_1", api_key_hash=None, date=None, limit=1)

    assert [row.id for row in rows] == ["gen_newest"]
    assert table.reads == [(b"ws_recent#ws_1#", b"ws_recent#ws_1#~", 1)]


def test_gcp_generation_write_indexes_recent_and_daily_bigtable_rows() -> None:
    generation = _generation("gen_1", "ws_1", "2026-05-02T12:00:00Z")
    table = _FakeBigtable()

    _bt_write_generation(table, "m", generation)

    assert table.committed == [
        b"gen#gen_1",
        b"ws#ws_1#2026-05-02#2026-05-02T12:00:00Z#gen_1",
        f"ws_recent#ws_1#{_reverse_time_key(generation.created_at)}#gen_1".encode(),
    ]


def test_gcp_provider_benchmark_write_uses_privacy_safe_indexes() -> None:
    sample = ProviderBenchmarkSample(
        id="bench_1",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        input_tokens=10,
        output_tokens=5,
        total_cost_microdollars=100,
        speed_tokens_per_second=25.0,
        elapsed_milliseconds=200,
        created_at="2026-05-02T12:00:00Z",
    )
    table = _FakeBigtable()

    _bt_write_provider_benchmark(table, "m", sample)

    assert table.committed == [
        f"benchmark#2026-05-02#openai#openai/gpt-4o-mini#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
        f"benchmark_day_recent#2026-05-02#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
        f"benchmark_provider_day#2026-05-02#openai#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
        f"benchmark_recent#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
        f"benchmark_provider_recent#openai#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
        f"benchmark_model_recent#openai#openai/gpt-4o-mini#{_reverse_time_key(sample.created_at)}#bench_1".encode(),
    ]
    assert b"ws_" not in b"".join(table.committed)
    assert b"key_" not in b"".join(table.committed)


def test_gcp_provider_benchmark_read_filters_without_workspace_scope() -> None:
    openai = ProviderBenchmarkSample(
        id="bench_openai",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    mistral = ProviderBenchmarkSample(
        id="bench_mistral",
        model="mistral/mistral-small-2603",
        provider="mistral",
        provider_name="Mistral",
        status="error",
        usage_type="BYOK",
        streamed=True,
        created_at="2026-05-02T12:01:00Z",
    )
    table = _FakeBigtable([_FakeReadRow(mistral), _FakeReadRow(openai)])

    rows = _bt_provider_benchmark_samples(
        table, "m", date="2026-05-02", provider="openai", model=None, limit=10
    )

    assert [row.id for row in rows] == ["bench_openai"]
    assert table.reads == [
        (
            b"benchmark_provider_day#2026-05-02#openai#",
            b"benchmark_provider_day#2026-05-02#openai#~",
            10,
        )
    ]


def test_gcp_provider_benchmark_date_only_uses_daily_recent_index() -> None:
    older_openai = ProviderBenchmarkSample(
        id="bench_openai",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    newer_mistral = ProviderBenchmarkSample(
        id="bench_mistral",
        model="mistral/mistral-small-2603",
        provider="mistral",
        provider_name="Mistral",
        status="success",
        usage_type="BYOK",
        streamed=True,
        created_at="2026-05-02T12:01:00Z",
    )
    table = _FakeBigtable([_FakeReadRow(newer_mistral), _FakeReadRow(older_openai)])

    rows = _bt_provider_benchmark_samples(
        table, "m", date="2026-05-02", provider=None, model=None, limit=1
    )

    assert [row.id for row in rows] == ["bench_mistral"]
    assert table.reads == [
        (b"benchmark_day_recent#2026-05-02#", b"benchmark_day_recent#2026-05-02#~", 1)
    ]


def test_gcp_provider_benchmark_date_only_falls_back_to_legacy_overread() -> None:
    openai = ProviderBenchmarkSample(
        id="bench_openai",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    table = _FakeBigtable(read_batches=[[], [_FakeReadRow(openai)]])

    rows = _bt_provider_benchmark_samples(
        table, "m", date="2026-05-02", provider=None, model=None, limit=10
    )

    assert [row.id for row in rows] == ["bench_openai"]
    assert table.reads == [
        (b"benchmark_day_recent#2026-05-02#", b"benchmark_day_recent#2026-05-02#~", 10),
        (b"benchmark#2026-05-02#", b"benchmark#2026-05-02#~", 1000),
    ]


def test_gcp_provider_benchmark_read_uses_recent_provider_index_without_date() -> None:
    openai = ProviderBenchmarkSample(
        id="bench_openai",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    table = _FakeBigtable([_FakeReadRow(openai)])

    rows = _bt_provider_benchmark_samples(
        table, "m", date=None, provider="openai", model=None, limit=10
    )

    assert [row.id for row in rows] == ["bench_openai"]
    assert table.reads == [
        (b"benchmark_provider_recent#openai#", b"benchmark_provider_recent#openai#~", 10)
    ]


def test_gcp_provider_benchmark_read_uses_recent_model_index_without_date() -> None:
    openai = ProviderBenchmarkSample(
        id="bench_openai",
        model="openai/gpt-4o-mini",
        provider="openai",
        provider_name="OpenAI",
        status="success",
        usage_type="Credits",
        streamed=False,
        created_at="2026-05-02T12:00:00Z",
    )
    table = _FakeBigtable([_FakeReadRow(openai)])

    rows = _bt_provider_benchmark_samples(
        table,
        "m",
        date=None,
        provider="openai",
        model="openai/gpt-4o-mini",
        limit=5,
    )

    assert [row.id for row in rows] == ["bench_openai"]
    assert table.reads == [
        (
            b"benchmark_model_recent#openai#openai/gpt-4o-mini#",
            b"benchmark_model_recent#openai#openai/gpt-4o-mini#~",
            5,
        )
    ]


def test_gcp_workspace_update_persists_name_and_deleted_state() -> None:
    store, db, _ = make_fake_store()
    user = store.ensure_user("alice@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    renamed = store.update_workspace(workspace.id, name="Renamed")
    deleted = store.update_workspace(workspace.id, deleted=True)

    assert renamed is not None
    assert renamed.name == "Renamed"
    assert deleted is None
    assert store.get_workspace(workspace.id) is None
    workspace_row = json.loads(db.rows[("workspace", workspace.id)].body)
    assert workspace_row["name"] == "Renamed"
    assert workspace_row["deleted"] is True


def test_gcp_reconcile_generation_activity_rewrites_existing_generations(monkeypatch) -> None:
    from trusted_router.storage_gcp_keys import SpannerApiKeys

    existing = _generation("gen_existing", "ws_1", "2026-05-02T12:00:00Z")
    newer = _generation("gen_newer", "ws_1", "2026-05-03T12:00:00Z")
    store = object.__new__(SpannerBigtableStore)
    store.generation_family = "m"
    written: list[str] = []

    def list_entities(kind: str, *, cls: type[Any], prefix: str | None = None, suffix: str | None = None):
        assert kind == "generation_by_workspace"
        assert cls is dict
        assert prefix == "ws_1#"
        assert suffix is None
        return [
            {"generation_id": existing.id},
            {"generation_id": "missing"},
            {"generation_id": newer.id},
        ]

    def get_generation(generation_id: str) -> Generation | None:
        return {existing.id: existing, newer.id: newer}.get(generation_id)

    def write_generation_bigtable(_table: Any, _family: str, generation: Generation) -> None:
        written.append(generation.id)

    monkeypatch.setattr(
        "trusted_router.storage_gcp_generations._bt_write_generation",
        write_generation_bigtable,
    )

    store._list_entities = list_entities  # type: ignore[method-assign]
    store.get_generation = get_generation  # type: ignore[method-assign]
    # generation_store is what reconcile_generation_activity delegates to.
    from trusted_router.storage_gcp_generations import SpannerGenerations
    from trusted_router.storage_gcp_io import SpannerIO

    io = SpannerIO(
        database=None,
        write_entity_batch=lambda *_a, **_kw: None,
        read_entity_tx=lambda *_a, **_kw: None,
        write_entity_tx=lambda *_a, **_kw: None,
        write_entity=lambda *_a, **_kw: None,
        read_entity=lambda *_a, **_kw: None,
        list_entities=list_entities,
        delete_entities=lambda *_a, **_kw: None,
        delete_entities_tx=lambda *_a, **_kw: None,
    )
    store.api_keys = SpannerApiKeys(io)
    store.generation_store = SpannerGenerations(
        io,
        bt_table=None,
        generation_family="m",
        add_usage_to_key=store.api_keys.add_usage,
    )
    # reconcile uses get() which we override on the generation_store.
    store.generation_store.get = get_generation  # type: ignore[method-assign]

    assert store.reconcile_generation_activity("ws_1") == 2
    assert written == ["gen_existing", "gen_newer"]


def test_gcp_bigtable_failure_after_spanner_commit_is_repairable(caplog, monkeypatch) -> None:
    store, db, _ = make_fake_store()
    key = _api_key("key_1", "ws_1", "2026-05-02T10:00:00Z")
    generation = _generation("gen_repair", "ws_1", "2026-05-02T12:00:00Z")
    store._write_entity("api_key", key.hash, key)

    def fail_bigtable(_table: Any, _family: str, _generation: Generation) -> None:
        raise RuntimeError("bigtable unavailable")

    monkeypatch.setattr(
        "trusted_router.storage_gcp_generations._bt_write_generation",
        fail_bigtable,
    )

    store.add_generation(generation)

    assert ("generation", generation.id) in db.rows
    assert ("generation_by_workspace", f"ws_1#2026-05-02#2026-05-02T12:00:00Z#{generation.id}") in db.rows
    key_row = json.loads(db.rows[("api_key", key.hash)].body)
    assert key_row["usage_microdollars"] == generation.total_cost_microdollars
    assert "bigtable_generation_index_failed" in caplog.text

    repaired: list[str] = []

    def repair_bigtable(_table: Any, _family: str, repaired_generation: Generation) -> None:
        repaired.append(repaired_generation.id)

    monkeypatch.setattr(
        "trusted_router.storage_gcp_generations._bt_write_generation",
        repair_bigtable,
    )
    assert store.reconcile_generation_activity("ws_1", date="2026-05-02") == 1
    assert repaired == [generation.id]
    key_after_repair = json.loads(db.rows[("api_key", key.hash)].body)
    assert key_after_repair["usage_microdollars"] == generation.total_cost_microdollars


def test_reverse_time_key_sorts_newer_generations_first() -> None:
    older = _reverse_time_key("2026-05-01T00:00:00Z")
    newer = _reverse_time_key("2026-05-02T00:00:00Z")

    assert newer < older
    assert len(newer) == len(older) == 13
