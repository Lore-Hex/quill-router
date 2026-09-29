from __future__ import annotations

import json
from typing import Any

from tests.fakes.spanner import make_fake_store
from trusted_router.byok_crypto import decrypt_byok_secret, encrypt_byok_secret
from trusted_router.config import Settings
from trusted_router.storage import ApiKey, CreditAccount, Generation, ProviderBenchmarkSample
from trusted_router.storage_gcp import SpannerBigtableStore
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
        model="openai/gpt-5.4-nano",
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
        spanner_module=None,
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


def test_gcp_store_disables_spanner_builtin_metrics(monkeypatch: Any) -> None:
    from google.cloud import spanner, spanner_v1

    spanner_calls: list[dict[str, Any]] = []
    pool_sizes: list[int] = []
    monkeypatch.setattr(
        "trusted_router.storage_gcp.configure_spanner_rpc_deadlines",
        lambda _database: None,
    )

    class FakeSpannerClient:
        def __init__(self, **kwargs: Any) -> None:
            spanner_calls.append(kwargs)

        def instance(self, _instance_id: str) -> FakeSpannerClient:
            return self

        def database(self, _database_id: str, **_kwargs: Any) -> object:
            # `pool=FixedSizePool(size=N)` is passed in production to
            # bound resident memory; accept-and-ignore here.
            return object()

    class FakePool:
        def __init__(self, *, size: int) -> None:
            pool_sizes.append(size)

    monkeypatch.setattr(spanner, "Client", FakeSpannerClient)
    monkeypatch.setattr(spanner_v1, "FixedSizePool", FakePool)
    monkeypatch.delenv("TR_SPANNER_POOL_SIZE", raising=False)

    SpannerBigtableStore(
        project_id="project",
        spanner_instance_id="spanner",
        spanner_database_id="database",
    )

    # credentials=None is the GCP-default ADC path (Cloud Run / GCE);
    # local or migration tooling may pass explicit service_account.Credentials
    # via GCP_SERVICE_ACCOUNT_KEY_JSON. Both keep disable_builtin_metrics
    # set so we don't pull OpenTelemetry runtime metrics.
    assert spanner_calls == [
        {
            "project": "project",
            "credentials": None,
            "disable_builtin_metrics": True,
        }
    ]
    assert pool_sizes == [8]


def test_gcp_store_opens_regional_ledger_when_local_issuance_is_disabled(
    monkeypatch: Any,
) -> None:
    """Every control-plane region must settle leases issued by another region."""
    from google.cloud import bigtable, spanner

    monkeypatch.setattr(
        "trusted_router.storage_gcp.configure_spanner_rpc_deadlines",
        lambda _database: None,
    )

    class FakeSpannerClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def instance(self, _instance_id: str) -> FakeSpannerClient:
            return self

        def database(self, _database_id: str, **_kwargs: Any) -> object:
            return object()

    class FakeBigtableClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def instance(self, _instance_id: str) -> FakeBigtableClient:
            return self

        def table(self, _table_id: str, *, app_profile_id: str) -> object:
            assert app_profile_id == "quota-us"
            return object()

    monkeypatch.setattr(spanner, "Client", FakeSpannerClient)
    monkeypatch.setattr(bigtable, "Client", FakeBigtableClient)

    store = SpannerBigtableStore(
        project_id="project",
        spanner_instance_id="spanner",
        spanner_database_id="database",
        bigtable_instance_id="bigtable",
        regional_quota_leases_enabled=False,
        regional_quota_bigtable_app_profiles={"us-central1": "quota-us"},
    )

    assert store._regional_quota_ledger is not None
    assert store._regional_quota_ledger.supports_region("us-central1") is True
    # Cross-region callbacks (a europe-west4 process reading the us-central1
    # cluster) need more than the client's 1 s padded floor; the default
    # budget is 4 s and the setting reaches the ledger unchanged.
    assert store._regional_quota_ledger._operation_timeout_seconds == 4.0
    tuned = SpannerBigtableStore(
        project_id="project",
        spanner_instance_id="spanner",
        spanner_database_id="database",
        bigtable_instance_id="bigtable",
        regional_quota_leases_enabled=False,
        regional_quota_bigtable_app_profiles={"us-central1": "quota-us"},
        regional_quota_ledger_timeout_seconds=6.5,
    )
    assert tuned._regional_quota_ledger is not None
    assert tuned._regional_quota_ledger._operation_timeout_seconds == 6.5


def test_gcp_api_key_lookup_uses_index_and_never_stores_raw_key() -> None:
    store, db = make_fake_store()
    store._write_entity(
        "credit",
        "ws_1",
        CreditAccount(workspace_id="ws_1"),
    )

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


def test_gcp_read_gateway_authorization_ignores_unknown_dataclass_fields() -> None:
    store, db = make_fake_store()
    auth = store.create_gateway_authorization(
        workspace_id="ws_1",
        key_hash="key_1",
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=123,
        credit_reservation_id="res_1",
        key_reserved_microdollars=0,
        requested_model_id="anthropic/claude-haiku-4.5",
        candidate_model_ids=["anthropic/claude-haiku-4.5"],
        region="us",
        endpoint_id="anthropic:claude-haiku-4.5",
        candidate_endpoint_ids=["anthropic:claude-haiku-4.5"],
        idempotency_key="idem_1",
        tags={"team": "x"},
        idempotency_fingerprint="fingerprint_1",
    )
    row = db.rows[("gateway_authorization", auth.id)]
    body = json.loads(row.body)
    body["some_future_field"] = 1
    row.body = json.dumps(body, separators=(",", ":"), sort_keys=True)

    refetched = store.get_gateway_authorization(auth.id)

    assert refetched is not None
    assert refetched.id == auth.id
    assert refetched.workspace_id == "ws_1"
    assert refetched.key_hash == "key_1"
    assert refetched.tags == {"team": "x"}
    assert refetched.idempotency_fingerprint == "fingerprint_1"


def test_gcp_byok_upsert_updates_secret_ref_and_hint() -> None:
    store, db = make_fake_store()

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


def test_gcp_byok_upsert_persists_encrypted_envelope_without_raw_key() -> None:
    store, db = make_fake_store()
    settings = Settings(environment="test")
    raw_key = "sk-storage-gcp-byok-secret-1111"
    envelope = encrypt_byok_secret(
        raw_key,
        settings,
        workspace_id="ws_1",
        provider="openai",
    )

    config = store.upsert_byok_provider(
        workspace_id="ws_1",
        provider="openai",
        secret_ref="byok://workspaces/ws_1/providers/openai",  # noqa: S106 - encrypted ref.
        key_hint="sk-sto...1111",
        encrypted_secret=envelope,
    )

    serialized = "\n".join(row.body for row in db.rows.values())
    assert raw_key not in serialized
    assert config.encrypted_secret is not None
    assert decrypt_byok_secret(
        config.encrypted_secret,
        settings,
        workspace_id="ws_1",
        provider="openai",
    ) == raw_key
    refetched = store.get_byok_provider("ws_1", "openai")
    assert refetched is not None and refetched.encrypted_secret is not None
    assert decrypt_byok_secret(
        refetched.encrypted_secret,
        settings,
        workspace_id="ws_1",
        provider="openai",
    ) == raw_key


def test_gcp_verification_tokens_are_one_time_wrong_purpose_safe_and_hash_only() -> None:
    store, db = make_fake_store()

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
    store, db = make_fake_store()

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


def test_gcp_wallet_challenge_reuse_is_bounded_per_normalized_scope() -> None:
    store, db = make_fake_store()
    address = "0x" + "a" * 40
    nonces: list[str] = []
    challenge_ids: list[str] = []

    for index in range(128):
        proposed_nonce = f"bounded-wallet-nonce-{index}"
        nonce, challenge = store.create_wallet_challenge(
            address=address.upper() if index % 2 else f"  {address}  ",
            message=(
                "trusted.example wants you to sign in with your Ethereum account:\n"
                f"{address}\n\nNonce: {proposed_nonce}"
            ),
            ttl_seconds=60,
            raw_nonce=proposed_nonce,
        )
        nonces.append(nonce)
        challenge_ids.append(challenge.hash)

    assert len([key for key in db.rows if key[0] == "wallet_challenge"]) == 1
    assert len([key for key in db.rows if key[0] == "wallet_challenge_lookup"]) == 1
    assert len([key for key in db.rows if key[0] == "wallet_challenge_by_scope"]) == 1
    assert set(nonces) == {"bounded-wallet-nonce-0"}
    assert len(set(challenge_ids)) == 1
    assert store.consume_wallet_challenge("bounded-wallet-nonce-127") is None
    active = store.consume_wallet_challenge(nonces[0])
    assert active is not None
    assert "Nonce: bounded-wallet-nonce-0" in active.message


def test_gcp_rate_limit_counts_in_same_window_and_resets_later() -> None:
    import datetime as dt

    store, _db = make_fake_store()
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


def test_provider_benchmark_from_generation_carries_ttfb_default_organic() -> None:
    generation = Generation(
        id="gen_1",
        request_id="req_1",
        workspace_id="ws_1",
        key_hash="key_1",
        model="anthropic/claude-opus-4.7",
        provider_name="Anthropic",
        app="TestApp",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=100,
        usage_type="Credits",
        speed_tokens_per_second=20.0,
        finish_reason="stop",
        status="success",
        streamed=True,
        provider="anthropic",
        elapsed_milliseconds=250,
        first_token_milliseconds=140,
        ttfb_milliseconds=90,
        region="us-east-1",
    )

    sample = ProviderBenchmarkSample.from_generation(generation)

    assert sample.ttfb_milliseconds == 90
    assert sample.first_token_milliseconds == 140
    # Organic production traffic is the default provenance.
    assert sample.source == "organic"


def test_gcp_workspace_update_persists_name_and_deleted_state() -> None:
    store, db = make_fake_store()
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


def test_gcp_reconcile_generation_activity_re_enqueues_durable_delivery() -> None:
    from trusted_router.storage_gcp_codec import generation_workspace_id

    store, db = make_fake_store(operational_analytics_outbox_enabled=True)
    existing = _generation("gen_existing", "ws_1", "2026-05-02T12:00:00Z")
    newer = _generation("gen_newer", "ws_1", "2026-05-03T12:00:00Z")
    for generation in (existing, newer):
        store._write_entity("generation", generation.id, generation)
        store._write_entity(
            "generation_by_workspace",
            generation_workspace_id(generation),
            {"generation_id": generation.id},
        )
    store._write_entity(
        "generation_by_workspace",
        "ws_1#2026-05-02#2026-05-02T13:00:00Z#missing",
        {"generation_id": "missing"},
    )

    assert store.reconcile_generation_activity("ws_1") == 2
    delivered = [
        event["event_id"]
        for event in db.operational_analytics_outbox
        if event["event_kind"] == "activity"
    ]
    assert sorted(delivered) == [existing.id, newer.id]


def test_reconcile_without_durable_outbox_repairs_nothing() -> None:
    store, db = make_fake_store()
    generation = _generation("gen_outbox_off", "ws_off", "2026-05-02T12:00:00Z")
    store._write_entity("generation", generation.id, generation)

    result = store.generation_store.reconcile_activity(generation_id=generation.id, detailed=True)

    assert result.scanned == 1
    assert result.durable_repaired == 0
    assert result.durable_failed == []
    assert result.missing == []
    assert db.operational_analytics_outbox == []


def test_reconcile_reports_durable_outcomes(monkeypatch) -> None:
    store, db = make_fake_store(
        operational_analytics_outbox_enabled=True, generation_records_enabled=True,
    )
    generation = _generation("gen_outcomes", "ws_outcomes", "2026-09-24T00:00:00Z")
    store._write_entity("generation", generation.id, generation)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Spanner outbox unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store._operational_analytics_outbox, "enqueue_activity_tx", unavailable)
        failed = store.generation_store.reconcile_activity(
            generation_id=generation.id, detailed=True,
        )
    assert failed.durable_repaired == 0
    assert failed.durable_failed == [generation.id]
    assert db.operational_analytics_outbox == []

    repaired = store.generation_store.reconcile_activity(generation_id=generation.id, detailed=True)
    assert repaired.durable_repaired == 1
    assert repaired.durable_failed == []
    assert [event["event_id"] for event in db.operational_analytics_outbox] == [generation.id]


def test_gcp_broadcast_claims_due_jobs_with_lease() -> None:
    store, db = make_fake_store()
    destination = store.create_broadcast_destination(
        workspace_id="ws_1",
        type="webhook",
        name="webhook",
        endpoint="https://webhook.example/otlp",
    )
    job = store.enqueue_broadcast_delivery(
        workspace_id="ws_1",
        destination_id=destination.id,
        generation_id="gen_1",
        settle_body={"request_id": "req_1"},
    )
    job.next_attempt_at = "2000-01-01T00:00:00Z"
    store._write_entity("broadcast_delivery", job.id, job)
    store._write_entity(
        "broadcast_delivery_due",
        f"pending#2000-01-01T00:00:00Z#{job.id}",
        {
            "job_id": job.id,
            "next_attempt_at": job.next_attempt_at,
            "workspace_id": job.workspace_id,
        },
    )

    first_claim = store.claim_broadcast_deliveries(limit=10, lease_seconds=60)
    second_claim = store.claim_broadcast_deliveries(limit=10, lease_seconds=60)

    assert [item.id for item in first_claim] == [job.id]
    assert second_claim == []
    stored = json.loads(db.rows[("broadcast_delivery", job.id)].body)
    assert stored["lease_owner"].startswith("bworker_")
    assert stored["leased_until"]


def test_gcp_broadcast_expired_lease_is_due_again() -> None:
    store, _db = make_fake_store()
    destination = store.create_broadcast_destination(
        workspace_id="ws_1",
        type="webhook",
        name="webhook",
        endpoint="https://webhook.example/otlp",
    )
    job = store.enqueue_broadcast_delivery(
        workspace_id="ws_1",
        destination_id=destination.id,
        generation_id="gen_1",
        settle_body={"request_id": "req_1"},
    )
    job.next_attempt_at = "2000-01-01T00:00:00Z"
    job.lease_owner = "dead-worker"
    job.leased_until = "2000-01-01T00:00:01Z"
    store._write_entity("broadcast_delivery", job.id, job)
    store._write_entity(
        "broadcast_delivery_due",
        f"pending#2000-01-01T00:00:00Z#{job.id}",
        {
            "job_id": job.id,
            "next_attempt_at": job.next_attempt_at,
            "workspace_id": job.workspace_id,
        },
    )

    claimed = store.claim_broadcast_deliveries(limit=10, lease_seconds=60)

    assert [item.id for item in claimed] == [job.id]
    assert claimed[0].lease_owner != "dead-worker"


def test_reverse_time_key_sorts_newer_generations_first() -> None:
    older = _reverse_time_key("2026-05-01T00:00:00Z")
    newer = _reverse_time_key("2026-05-02T00:00:00Z")

    assert newer < older
    assert len(newer) == len(older) == 13


def test_reconcile_pages_bound_spanner_reads_and_continue_past_missing_records(monkeypatch) -> None:
    from trusted_router.storage_gcp_codec import generation_workspace_id

    store, db = make_fake_store(operational_analytics_outbox_enabled=True)
    generations = [_generation(f"gen_{i}", "ws_page", f"2026-09-2{i}T00:00:00Z") for i in range(3)]
    refs = [(generation_workspace_id(g), json.dumps({"generation_id": g.id})) for g in generations]
    for generation in generations[1:]:
        store._write_entity("generation", generation.id, generation)
    snapshot_type = type(db.snapshot())
    original = snapshot_type.execute_sql
    calls = []

    def execute_sql(self, sql, *, params, param_types, **kwargs):
        if params.get("kind") != "generation_by_workspace":
            return original(self, sql, params=params, param_types=param_types, **kwargs)
        # The fake's generic reader does not implement keyset SQL. Assert the
        # server predicates, then model them before applying the server limit.
        assert "SELECT id, body" in sql
        assert "kind=@kind" in sql
        assert "STARTS_WITH(id, @prefix)" in sql
        assert "id > @after_id" in sql
        assert "ORDER BY id LIMIT @limit" in sql
        assert params["limit"] == 2  # one processed row plus a truncation sentinel
        assert params["prefix"] == "ws_page#"
        assert param_types["limit"] == store._param_types.INT64
        calls.append(params.copy())
        return [row for row in refs if row[0].startswith(params["prefix"]) and row[0] > params["after_id"]][:params["limit"]]

    monkeypatch.setattr(snapshot_type, "execute_sql", execute_sql)
    first = store.generation_store.reconcile_activity("ws_page", limit=1, detailed=True)
    assert first.scanned == 1
    assert first.durable_repaired == 0
    assert first.missing == [generations[0].id]
    assert first.truncated is True
    assert first.next_after_id == refs[0][0]
    second = store.generation_store.reconcile_activity(
        "ws_page", limit=1, detailed=True, after_id=first.next_after_id,
    )
    assert second.durable_repaired == 1
    assert second.truncated is True
    assert second.next_after_id == refs[1][0]
    last = store.generation_store.reconcile_activity(
        "ws_page", limit=1, detailed=True, after_id=second.next_after_id,
    )
    assert last.durable_repaired == 1
    assert last.truncated is False
    assert last.next_after_id is None
    assert [call["after_id"] for call in calls] == ["", refs[0][0], refs[1][0]]
    delivered = sorted(event["event_id"] for event in db.operational_analytics_outbox)
    assert delivered == [generations[1].id, generations[2].id]
