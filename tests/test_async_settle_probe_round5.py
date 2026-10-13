"""Round-five reviewer regressions; local stores and deterministic barriers."""

from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local
from types import SimpleNamespace

import pytest

from scripts import provision_async_settle_shadow_probe_workspace as p
from scripts.async_settle.probe_cohort_check import check
from tests.test_async_settle_probe_round3 import (
    assert_probe_references_isolated,
    assert_request_import_graph_isolated,
    source_inventory,
)
from tests.test_async_settle_probe_round3 import (
    scratch_source as scratch_source,
)
from trusted_router.async_settle_shadow_evidence import SAMPLE
from trusted_router.security import new_api_key
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import generation_id_for_authorization
from trusted_router.storage_operational_analytics import analytics_surrogate


@pytest.fixture
def typed(monkeypatch):
    monkeypatch.setattr(
        p, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed")
    )


def evidence_case():
    now = dt.datetime(2026, 10, 12, 12, tzinfo=dt.UTC)
    row = dict(
        workspace_fingerprint=analytics_surrogate("workspace", "cohort"),
        observed_at_us=int(now.timestamp() * 1e6),
        authorization_id="auth-one",
        adapter="openai",
        route_type="chat.completions",
        streamed=False,
        classification="exact",
    )
    rows = [row]
    counters = []
    evidence = SimpleNamespace(
        day=lambda kind, day: (
            [(str(i), json.dumps(r)) for i, r in enumerate(rows if kind == SAMPLE else counters)]
            if day == now.date().isoformat()
            else []
        )
    )
    return now, row, rows, counters, evidence, [generation_id_for_authorization("auth-one")]


@pytest.mark.parametrize(
    "damage",
    [
        "workspace",
        "stale",
        "future",
        "duplicate",
        "excluded",
        "drops",
        "rejections",
        "comparison_dropped",
    ],
)
def test_reconciliation_negative(damage):
    now, row, rows, counters, evidence, ids = evidence_case()
    assert not check(evidence, "cohort", now, ids)["errors"]
    if damage == "workspace":
        row["workspace_fingerprint"] = analytics_surrogate("workspace", "other")
    elif damage == "stale":
        row["observed_at_us"] -= 601_000_000
    elif damage == "future":
        row["observed_at_us"] += 1_000_000
    elif damage == "duplicate":
        rows.append(dict(row))
    elif damage == "excluded":
        row["eligibility"] = {"exclusion": "bad"}
    else:
        counters.append({"flushed_at_us": int(now.timestamp() * 1e6), damage: 1})
    assert check(evidence, "cohort", now, ids)["errors"]


def test_registered_other_workspace_rejected(tmp_path, typed):
    store = InMemoryStore()
    user = store.ensure_user("other", email="other@example.invalid")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _ = store.create_api_key(workspace_id=workspace.id, name="other", creator_user_id=user.id)
    path = tmp_path / "key"
    path.write_text(raw)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        p.apply_cohort(store, p.cohort_names(), 100_000_000, path)
    assert store.find_user_by_email(p.cohort_names()["email"]) is None


def test_key_and_directory_fsynced_before_db(tmp_path, monkeypatch, typed):
    import stat

    store = InMemoryStore()
    seen = []
    original = os.fsync
    ensure = InMemoryStore.ensure_user

    def sync(fd):
        seen.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)

    def user(self, *a, **kw):
        assert seen == ["file", "directory"]
        return ensure(self, *a, **kw)

    monkeypatch.setattr(p.os, "fsync", sync)
    monkeypatch.setattr(InMemoryStore, "ensure_user", user)
    p.apply_cohort(store, p.cohort_names(), 100_000_000, tmp_path / "key")


def test_precreated_hardlink_rejected(tmp_path, typed):
    store = InMemoryStore()
    attacker = tmp_path / "attacker"
    path = tmp_path / "key"
    attacker.write_text(new_api_key())
    attacker.chmod(0o600)
    os.link(attacker, path)
    with pytest.raises(ValueError):
        p.apply_cohort(store, p.cohort_names(), 100_000_000, path)


@pytest.mark.parametrize(
    "step", ["ensure_user", "update_workspace", "credit_workspace_typed_direct", "create_api_key"]
)
def test_dry_run_after_partial_apply(step, tmp_path, typed, monkeypatch, capsys):
    store = InMemoryStore()
    original = getattr(InMemoryStore, step)

    def fail(self, *a, **kw):
        original(self, *a, **kw)
        raise RuntimeError("lost reply")

    with monkeypatch.context() as m:
        m.setattr(InMemoryStore, step, fail)
        with pytest.raises(RuntimeError):
            p.apply_cohort(store, p.cohort_names(), 100_000_000, tmp_path / "key")
    assert p.main([], store=store) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["apply"] is False
    assert (tmp_path / "key").read_text() not in json.dumps(result)


@pytest.mark.parametrize("boundary", ["startup", "mounted"])
def test_real_settlement_token_map_cannot_share_probe_key(boundary):
    import secrets

    from fastapi import HTTPException
    from starlette.requests import Request

    from trusted_router.config import Settings, validate_shadow_probe_key
    from trusted_router.routes.internal.federation import require_federation_settlement_peer

    key, probe = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    args = dict(
        environment="worker",
        service_surface="internal",
        storage_backend="memory",
        internal_gateway_token=secrets.token_urlsafe(32),
        observer_internal_token=secrets.token_urlsafe(32),
        federation_settlement_inbound_tokens="aws-eu=" + key,
    )
    cfg = Settings(**args, async_settle_shadow_probe_api_key=probe)

    def request(token):
        return Request(
            {
                "type": "http",
                "headers": [(b"x-trustedrouter-federation-settlement-token", token.encode())],
            }
        )

    assert require_federation_settlement_peer(request(key), cfg) == "aws-eu"
    with pytest.raises(HTTPException):
        require_federation_settlement_peer(request(probe), cfg)
    with pytest.raises(ValueError, match="distinct"):
        if boundary == "startup":
            Settings(**args, async_settle_shadow_probe_api_key=key)
        else:
            validate_shadow_probe_key(cfg, key)


def test_complete_crash_file_is_synced_on_resume(tmp_path, monkeypatch, typed):
    import stat

    store = InMemoryStore()
    path = tmp_path / "key"
    # Simulates a full userspace write followed by process death before fsync.
    path.write_text(new_api_key())
    path.chmod(0o600)
    synced = []
    original = os.fsync
    ensure = InMemoryStore.ensure_user

    def sync(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)

    def user(self, *a, **kw):
        assert synced == ["file", "directory"], (
            "resume writes DB without syncing the recovered file or directory"
        )
        return ensure(self, *a, **kw)

    monkeypatch.setattr(p.os, "fsync", sync)
    monkeypatch.setattr(InMemoryStore, "ensure_user", user)
    p.apply_cohort(store, p.cohort_names(), 100_000_000, path)


def test_checker_rejects_conflicting_retry():
    import copy
    import time

    from tests.test_async_settle_shadow_accounting import sample_row
    from trusted_router.async_settle_shadow_evidence import COUNTER
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

    row = sample_row()
    identity = row["authorization_day"] + "/" + row["authorization_id"]
    persisted = {}

    class LocalEvidence(EvidenceStore):
        def transaction(self, fn, deadline):
            return fn(None)

        def query(self, tx, statement, deadline):
            params = statement[1]
            raw = persisted.get((params["kind"], params["id"]))
            return [] if raw is None else [(raw,)]

        def write(self, tx, kind, key, body, deadline):
            persisted[kind, key] = json.dumps(body)

        def day(self, kind, day):
            return [
                (key, raw)
                for (k, key), raw in persisted.items()
                if k == kind and key.startswith(day + "/")
            ]

    evidence = LocalEvidence(None)
    deadline = time.monotonic() + 60
    assert evidence.insert_sample(identity, row, deadline) == "inserted"
    retry = copy.deepcopy(row)
    retry["payload_hash"] = "b" * 64
    assert evidence.insert_sample(identity, retry, deadline) == "conflict"
    # Runtime persists the first sample and increments this counter on conflict.
    persisted[COUNTER, row["authorization_day"] + "/writer"] = json.dumps(
        dict(flushed_at_us=row["observed_at_us"], conflicting_samples=1)
    )
    now = dt.datetime.fromtimestamp(row["observed_at_us"] / 1e6, dt.UTC)
    from tests.test_async_settle_shadow import context

    workspace = context().authorization.workspace_id
    assert row["workspace_fingerprint"] == analytics_surrogate("workspace", workspace)
    result = check(
        evidence, workspace, now, [generation_id_for_authorization(row["authorization_id"])]
    )
    assert result["errors"], "retained first exact sample masks a conflicting retry"


@pytest.mark.parametrize("state", ["personal", "renamed", "funded", "keyed"])
def test_sole_owner_membership_every_state(state, tmp_path, monkeypatch, typed):
    store = InMemoryStore()
    names = p.cohort_names()
    user = store.ensure_user(names["email"], email=names["email"], trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    path = tmp_path / "key"
    if state != "personal":
        store.update_workspace(workspace.id, name=names["workspace_name"])
    if state in {"funded", "keyed"}:
        p.apply_cohort(store, names, 100_000_000, path)
        if state == "funded":
            store.update_key(store.get_key_by_raw(path.read_text()).hash, {"disabled": True})
    store.add_members(workspace.id, ["unrelated@fixture.invalid"], role="admin")
    foreign = store.find_user_by_email("unrelated@fixture.invalid")
    assert store.user_can_manage(foreign.id, workspace.id)

    def no_mutation(*args, **kwargs):
        pytest.fail("foreign member accepted before mutation")

    for method in (
        "ensure_user",
        "update_workspace",
        "credit_workspace_typed_direct",
        "create_api_key",
        "update_key",
    ):
        monkeypatch.setattr(InMemoryStore, method, no_mutation)
    with pytest.raises(ValueError, match="sole-owner"):
        p.apply_cohort(store, names, 100_000_000, path)
    if state == "keyed":
        with pytest.raises(ValueError, match="validation failed"):
            p.validate(store, path.read_text())


@pytest.mark.parametrize("race", ["two-files", "same-file", "two-labels"])
def test_concurrent_apply_reconciles_created_key(race, tmp_path, monkeypatch, typed):
    store = InMemoryStore()
    paths = [tmp_path / "a", tmp_path / ("b" if race == "two-files" else "a")]
    labels = ["a", "b"] if race == "two-labels" else ["", ""]
    for path in set(paths):
        path.write_text(new_api_key())
        path.chmod(0o600)
    barrier = Barrier(2)
    original = InMemoryStore.create_api_key
    created = []

    def interleave(self, **kwargs):
        barrier.wait(timeout=30)  # Both callers passed the pre-create lookup.
        result = original(self, **kwargs)
        created.append(result[1])
        barrier.wait(timeout=30)  # Both writes committed before either reread.
        return result

    with monkeypatch.context() as patch:
        patch.setattr(InMemoryStore, "create_api_key", interleave)

        def apply(index):
            try:
                return p.apply_cohort(
                    store, p.cohort_names(labels[index]), 100_000_000, paths[index]
                )
            except p.ConcurrentProvisioning:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, range(2)))
    assert len(created) == 2
    for index, result in enumerate(results):
        assert paths[index].exists()
        if result is not None:
            bound = store.get_key_by_raw(paths[index].read_text())
            assert (result["key_id"], result["workspace_id"]) == (bound.hash, bound.workspace_id)
            assert not bound.disabled
    assert sum(not key.disabled for key in created) <= 1
    for workspace in {key.workspace_id for key in created}:
        assert store.credit_money_snapshot(workspace)[0] == 100_000_000
    # A fresh unregistered file converges after a failed run's disabled key;
    # successful runs reuse their original file.
    for index, result in enumerate(results):
        path = paths[index] if result else tmp_path / ("retry-" + str(index))
        workspace = store.list_workspaces_for_user(
            store.find_user_by_email(p.cohort_names(labels[index])["email"]).id
        )[0]
        active = [key for key in store.list_keys(workspace.id) if not key.disabled]
        if active and result is None:
            continue  # The other caller owns this workspace's surviving key.
        retry = p.apply_cohort(store, p.cohort_names(labels[index]), 100_000_000, path)
        assert store.get_key_by_raw(path.read_text()).hash == retry["key_id"]


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode", "owner"])
def test_recovery_descriptor_rejects_unsafe_files(damage, tmp_path, monkeypatch, typed):
    store = InMemoryStore()
    path = tmp_path / "key"
    path.write_text(new_api_key())
    path.chmod(0o600)
    if damage == "symlink":
        path.rename(tmp_path / "target")
        path.symlink_to(tmp_path / "target")
    elif damage == "hardlink":
        os.link(path, tmp_path / "alias")
    elif damage == "mode":
        path.chmod(0o400)
    else:
        monkeypatch.setattr(p.os, "getuid", lambda: -1)
    with pytest.raises((ValueError, OSError)):
        p.apply_cohort(store, p.cohort_names(), 100_000_000, path)
    assert store.find_user_by_email(p.cohort_names()["email"]) is None


def test_counter_policy_covers_schema():
    from scripts.async_settle.probe_cohort_check import BUCKET_POLICY, COUNTER_POLICY
    from trusted_router.async_settle_shadow_evidence import COUNT_FIELDS, PHASE_FIELDS, Counters
    from trusted_router.async_settle_shadow_v2_retained import COUNTER_FIELDS

    def fields(table):
        names = [name for _, names, _ in table for name in names.split()]
        assert len(names) == len(set(names))
        return set(names)

    assert fields(COUNTER_POLICY) == COUNTER_FIELDS == set(Counters("test", "revision")._day())
    assert fields(BUCKET_POLICY) == set(COUNT_FIELDS) | set(PHASE_FIELDS)


@pytest.mark.parametrize(
    "field",
    [
        "conflicting_samples",
        "last_mismatch_at_us",
        "first_gap_at_us",
        "counts.mismatch",
        "counts.requires_review",
        "terminal_counts.conflicting_samples",
        "terminal_counts.mismatch",
    ],
)
def test_every_conflict_diagnostic_vetoes(field):
    now, row, rows, counters, evidence, ids = evidence_case()
    counter = {"flushed_at_us": row["observed_at_us"]}
    if "." in field:
        bucket, name = field.split(".")
        counter[bucket] = [{name: 1}]
    else:
        counter[field] = row["observed_at_us"] if field.endswith("_us") else 1
    counters.append(counter)
    assert check(evidence, "cohort", now, ids)["errors"]


@pytest.mark.parametrize(
    "reference,reason",
    [
        ("def dump(s: Settings):\n    return s.model_dump()", "Settings enumeration"),
        ("cohort = Settings().__dict__", "Settings enumeration"),

        (
            'import importlib\ncohort = importlib.import_module("trusted_router.synthetic." + "shadow_" + "probe")',
            "outside exact allowlist",
        ),
        (
            'from trusted_router.config import Settings\ncohort = getattr(Settings(), "async_settle_shadow_" + "probe_api_key")',
            "outside exact allowlist",
        ),
        (
            'from trusted_router.config import Settings\ncohort = {k: v for k, v in Settings().model_dump().items() if k.startswith("async_settle_") and k.endswith("api_key")}',
            "Settings enumeration",
        ),
        (
            'cohort = lambda cfg: {k:v for k,v in cfg.model_dump().items() if k.startswith("async_settle_") and k.endswith("api_key")}',
            "Settings enumeration",
        ),
        ("cohort = lambda cfg, name: getattr(cfg, name)", "dynamic getattr"),
        ("import importlib\ncohort = lambda name: importlib.import_module(name)", "dynamic import"),
        (
            "from trusted_router.config import Settings\ncohort = dict(Settings())",
            "Settings enumeration",
        ),
        ("cohort = f\"async_settle_{'shadow'}_{'probe'}_api_key\"", "outside exact allowlist"),
        ('cohort = "_".join(["shadow", "probe"])', "outside exact allowlist"),
        ('cohort = "%s_%s" % ("shadow", "probe")', "outside exact allowlist"),
        ('cohort = "{}_{}".format("shadow", "probe")', "outside exact allowlist"),
    ],
)
def test_tripwire_constructed_reference_controls(scratch_source, reference, reason):
    path = scratch_source / "src/trusted_router/routes/inference.py"
    old = path.read_text()
    try:
        path.write_text(old + "\n" + reference + "\n")
        with pytest.raises(AssertionError, match=reason):
            inventory = source_inventory(scratch_source)
            assert_probe_references_isolated(scratch_source, inventory)
            assert_request_import_graph_isolated(inventory)
    finally:
        path.write_text(old)


def test_real_generation_consumer_inventory(monkeypatch):
    """The cohort is real, synthetic-attributed traffic: generation records and
    lookup; its own workspace activity/usage; shared operational outbox,
    delivery heartbeat and public analytics freshness; archival, repair and
    reconciliation; retirement-notice discovery; all-traffic calibration
    coverage all include it. It does not feed synthetic samples, status
    components, history, SLOs/burn rates, failure streaks, remediator, provider
    benchmarks/analytics, rankings/leaderboards, route health, drift,
    client-event rollups or primary fleet coverage.
    """
    from tests.async_settle_probe_consumers import CONSUMERS, consumer_snapshots

    before, after = consumer_snapshots(monkeypatch)
    assert before.keys() == after.keys() == CONSUMERS.keys()
    for consumer, expected in CONSUMERS.items():
        assert ("changes" if before[consumer] != after[consumer] else "unchanged") == expected, (
            consumer
        )
    assert before["public analytics freshness"]["seconds_since_last_delivery"] is None
    assert isinstance(after["public analytics freshness"]["seconds_since_last_delivery"], (int, float))
    assert (
        after["all-traffic coverage"]["windows"]["24h"]["coverage"]
        <= before["all-traffic coverage"]["windows"]["24h"]["coverage"]
    )


@pytest.mark.parametrize('field,encoding', [
    ('federation_settlement_inbound_tokens', 'plane-map-second'),
    ('google_alias_credentials_json', 'client-id'),
    ('google_alias_credentials_json', 'client-secret'),
    ('github_alias_credentials_json', 'client-id'),
    ('github_alias_credentials_json', 'client-secret'),
    ('postgres_dsn', 'uri'),
    ('postgres_dsn', 'keyword'),
    ('clickhouse_url', 'url-list'),
    ('provider_analytics_clickhouse_url', 'url-list'),
    ('operational_analytics_clickhouse_url', 'url-list'),
    ('sentry_dsn', 'dsn'),
    ('attribution_cookie_key', 'base64'),
    ('byok_envelope_key_b64', 'base64'),
    ('adyen_hmac_key', 'hex'),
])
@pytest.mark.asyncio
async def test_structured_credentials_real_encodings(field, encoding, tmp_path, monkeypatch):
    import base64

    from trusted_router.config import Settings, validate_shadow_probe_key
    key = 'x' * 32
    if encoding == 'plane-map-second':
        value = 'azure=' + 'z' * 32 + ',aws-eu=' + key
    elif encoding.startswith('client-'):
        value = json.dumps({'fixture.invalid': {
            'client_id': key if encoding == 'client-id' else 'client',
            'client_secret': key if encoding == 'client-secret' else 'secret'}})
    elif encoding == 'keyword':
        value = "host=localhost password='" + key + "'"
    elif encoding == 'uri':
        value = 'postgresql://reader:' + key + '@localhost/db'
    elif encoding == 'url-list':
        value = 'http://localhost:8123,http://reader:' + key + '@localhost:8124'
    elif encoding == 'dsn':
        value = 'https://public:' + key + '@sentry.invalid/1'
    elif encoding == 'base64':
        value = base64.b64encode(key.encode()).decode()
    else:
        value = key.encode().hex()
    settings = Settings(environment='test', **{field: value})
    with pytest.raises(ValueError, match='distinct'):
        validate_shadow_probe_key(settings, key)
    with pytest.raises(ValueError, match='distinct'):
        Settings(environment='test', async_settle_shadow_probe_api_key=key, **{field: value})
    from trusted_router.synthetic import cli
    path = tmp_path / 'mounted-key'
    path.write_text(key)
    path.chmod(0o600)
    monkeypatch.setenv('TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE', str(path))
    monkeypatch.delenv('TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY', raising=False)
    monkeypatch.setattr(cli, 'get_settings', lambda: settings)
    def no_http(**kwargs):
        pytest.fail('reused mounted credential reached HTTP setup')
    monkeypatch.setattr(cli.httpx, 'AsyncClient', no_http)
    assert await cli.run_shadow_probe(once=True) == 2



@pytest.mark.parametrize('cohort', ['shadow', 'monitor', 'stage-d'])
def test_retirement_notice_recipient_policy(cohort, monkeypatch, tmp_path, typed):
    from scripts import provision_stage_d_probe_workspace as stage_d
    from scripts import provision_synthetic_monitor as monitor
    from trusted_router.config import Settings
    from trusted_router.services import email
    from trusted_router.services import retirement_notices as notices
    store = InMemoryStore()
    monkeypatch.setattr(notices, 'STORE', store)
    monkeypatch.setattr(email, 'STORE', store)
    if cohort == 'shadow':
        result = p.apply_cohort(store, p.cohort_names(), 100_000_000, tmp_path / 'key')
        address = p.cohort_names()['email']
    else:
        address = monitor.DEFAULT_EMAIL if cohort == 'monitor' else stage_d.DEFAULT_EMAIL
        result = monitor.provision(store, email=address, workspace_name='Notice fixture', key_name='fixture',
                                   funding_microdollars=100_000_000, funding_event_id='fixture', target_shards=1,
                                   apply=True, key_output_file=tmp_path / 'key')
    user = store.find_user_by_email(address)
    service = email.EmailService(Settings(environment='test'))
    assert not user.email_verified
    assert notices._recipients(result['workspace_id'], service) == []
    # No implicit .internal suppression: verification would enable this address.
    user.email_verified = True
    assert notices._recipients(result['workspace_id'], service) == [address]


def test_stale_concurrent_success_is_rejected_by_validation(tmp_path, monkeypatch, typed):
    """The limit of the non-atomic safety net, and the step that catches it.

    Provisioning is a single-operator, run-once procedure. If two applies using the same
    file are nevertheless interleaved so that the second write lands after the first caller
    has already reread and returned, the first caller's success is stale: the raw key now
    resolves to the second caller's key, which that caller disabled. The runbook's mandatory
    read-only validation step must fail closed on exactly that end state.
    """
    store = InMemoryStore()
    path = tmp_path / "key"
    path.write_text(new_api_key())
    path.chmod(0o600)
    barrier, first_returned, thread = Barrier(2), Event(), local()
    original = InMemoryStore.create_api_key

    def interleave(self, **kwargs):
        barrier.wait(timeout=30)  # Both callers passed the pre-create lookup.
        if thread.index:
            assert first_returned.wait(30)  # Hold the second write until the first returned.
        return original(self, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(InMemoryStore, "create_api_key", interleave)

        def apply(index):
            thread.index = index
            try:
                return p.apply_cohort(store, p.cohort_names(), 100_000_000, path)
            except p.ConcurrentProvisioning:
                return None
            finally:
                if index == 0:
                    first_returned.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(apply, range(2))
    assert first is not None and second is None
    bound = store.get_key_by_raw(path.read_text())
    assert bound.disabled and bound.hash != first["key_id"]  # The stale success.
    with pytest.raises(ValueError, match="validation failed"):
        p.validate(store, path.read_text())
    assert store.credit_money_snapshot(first["workspace_id"])[0] == 100_000_000  # Funded once.
