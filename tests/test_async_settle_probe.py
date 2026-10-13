from __future__ import annotations

import datetime as dt
import io
import json
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import provision_async_settle_shadow_probe_workspace as provisioning
from scripts.provision_synthetic_monitor import provision
from trusted_router.config import Settings
from trusted_router.enclave_regions import ENCLAVE_REGIONS
from trusted_router.storage import InMemoryStore
from trusted_router.synthetic import cli, shadow_probe
from trusted_router.synthetic.alerts import alert_on_failure_streak
from trusted_router.synthetic.components import (
    OPS_PROBE_TYPES,
    sample_component_ids,
    sample_slo_class_ids,
)
from trusted_router.synthetic.shadow_probe_contract import PROBE_NAMES, SHAPES, Shape
from trusted_router.synthetic.status import _monitor_freshness, status_snapshot


def test_fixed_request_contract() -> None:
    assert len(SHAPES) == len(PROBE_NAMES) == 8
    for shape in SHAPES:
        body = shape.body()
        expected: dict[str, Any] = {
            "model": {"openai": "openai/gpt-4.1-mini", "anthropic": "anthropic/claude-haiku-4.5"}[shape.provider],
            "provider": {"only": [shape.provider], "allow_fallbacks": False},
            "stream": shape.stream,
        }
        if shape.route == "/chat/completions":
            expected.update(messages=[{"role": "user", "content": "Reply OK."}], max_tokens=8)
        else:
            expected.update(input="Reply OK.", max_output_tokens=8)
        assert body == expected


@pytest.mark.parametrize("regions", ["us-central1", "us-central1,europe-west4", ""])
def test_targets_always_use_all_regional_enclaves(regions: str) -> None:
    result = shadow_probe.targets(Settings(environment="test", regions=regions))
    assert [target.name for target in result] == list(ENCLAVE_REGIONS)
    assert [target.api_base_url for target in result] == [
        f"https://api-{region}.quillrouter.com/v1" for region in ENCLAVE_REGIONS
    ]


def success(shape: Shape) -> httpx.Response:
    payload = ({"choices": [{"finish_reason": "stop"}]} if shape.route == "/chat/completions"
               else {"type": "response.completed", "response": {"status": "completed"}})
    if shape.stream:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n")
    return httpx.Response(200, json=payload)


@pytest.mark.asyncio
async def test_every_shape_every_target_unique_idempotency_and_private_samples() -> None:
    requests: list[httpx.Request] = []
    key = secrets.token_urlsafe(24)
    cfg = Settings(environment="test", async_settle_shadow_probe_api_key=key,
                   async_settle_shadow_probe_repetitions=2)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        shape = next(s for s in SHAPES if s.body() == body and "/v1" + s.route == request.url.path)
        assert request.headers["authorization"] == "Bearer " + key
        assert request.url.host in {f"api-{r}.quillrouter.com" for r in ENCLAVE_REGIONS}
        return success(shape)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        samples = await shadow_probe.run_pass(cfg, monitor_region="test", client=client)
    assert len(samples) == len(requests) == 64
    assert len({request.headers["idempotency-key"] for request in requests}) == 64
    assert all(sample.status == "up" for sample in samples)
    assert {s.probe_type for s in samples} == PROBE_NAMES
    assert PROBE_NAMES <= OPS_PROBE_TYPES
    for sample in samples:
        assert sample_component_ids(sample) == sample_slo_class_ids(sample) == []
        sample.status = "down"
        assert not alert_on_failure_streak(object(), sample)
    now = dt.datetime.now(dt.UTC)
    assert _monitor_freshness(samples, now=now)["latest_sample_at"] is None
    before, after = status_snapshot([], now=now, settings=cfg), status_snapshot(samples, now=now, settings=cfg)
    before.pop("generated_at")
    after.pop("generated_at")
    assert before == after
    from trusted_router.routes.internal.synthetic import _client_watch_pass
    _client_watch_pass(cfg, samples)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [httpx.Response(500), httpx.Response(200, text="bad"),
                                     httpx.Response(200, json={"choices": []})])
async def test_failures_are_samples_without_error_text(reply: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: reply)) as client:
        sample = await shadow_probe.probe(client, shadow_probe.targets(Settings(environment="test"))[0], SHAPES[0],
                                          api_key=secrets.token_urlsafe(24), monitor_region="test")
    assert sample.status == "down" and sample.error_type


@pytest.mark.parametrize("field", ["synthetic_monitor_api_key", "stage_d_probe_api_key",
                                    "internal_gateway_token", "observer_internal_token"])
def test_settings_reject_reused_key(field: str) -> None:
    key = secrets.token_urlsafe(24)
    with pytest.raises(ValueError, match="must be distinct"):
        Settings(environment="test", async_settle_shadow_probe_api_key=key, **{field: key})


@pytest.mark.parametrize("field,value", [
    ("async_settle_shadow_probe_repetitions", 0),
    ("async_settle_shadow_probe_repetitions", 9),
    ("async_settle_shadow_probe_cadence_minutes", 0),
    ("async_settle_shadow_probe_cadence_minutes", 7),
    ("async_settle_shadow_probe_cadence_minutes", 31),
])
def test_settings_bound_probe_volume(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        Settings(environment="test", **{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("mounted", [False, True])
async def test_cli_only_ingests_private_samples(monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path, mounted: bool) -> None:
    key, observer = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    cfg = Settings(environment="test", async_settle_shadow_probe_api_key=key, observer_internal_token=observer)
    monkeypatch.delenv("TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE", raising=False)
    if mounted:
        key_file = tmp_path / "probe-key"
        key_file.write_text(key)
        cfg = Settings(environment="worker", service_surface="observer", storage_backend="memory", observer_internal_token=observer)
        monkeypatch.setenv("TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(cli, "get_settings", lambda: cfg)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/internal/synthetic/samples":
            posted.append(json.loads(request.content))
            assert request.headers["x-trustedrouter-internal-token"] == observer
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        shape = next(s for s in SHAPES if s.body() == body and "/v1" + s.route == request.url.path)
        return success(shape)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    assert await cli.run_shadow_probe() == 0
    assert len(posted) == 1 and len(posted[0]["samples"]) == 32
    output = capsys.readouterr()
    assert key not in output.out + output.err and observer not in output.out + output.err


def seed(store: InMemoryStore, path: Path, label: str = "") -> dict[str, Any]:
    names = provisioning.cohort_names(label)
    result = provision(store, **{k: names[k] for k in ("email", "workspace_name", "key_name", "funding_event_id")},
                       funding_microdollars=100_000_000, target_shards=1, apply=True, key_output_file=path)
    return result


def test_provision_dry_run_and_labels(capsys: Any) -> None:
    store = InMemoryStore()
    assert provisioning.main(["--label", "second"], store=store) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["secret_name"] == provisioning.SECRET_NAME + "-second"
    assert result["email"] == "async-settle-shadow-probe-second@trustedrouter.internal"
    assert result["funding_microdollars"] == 100_000_000
    assert store.find_user_by_email(result["email"]) is None
    assert provisioning.cohort_names()["funding_event_id"] != provisioning.cohort_names("second")["funding_event_id"]


def test_provision_apply_keeps_key_file_private_and_funding_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, caplog: Any) -> None:
    store = InMemoryStore()
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    monkeypatch.setattr(provisioning, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    path = tmp_path / "key"
    assert provisioning.main(["--apply", "--key-output-file", str(path)], store=store) == 0
    raw = path.read_text()
    assert raw and path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    if raw in output.out + output.err + caplog.text:
        pytest.fail("credential escaped into CLI output or logs")
    result = json.loads(output.out)
    assert provisioning.main(["--apply"], store=store) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["credited"] is False
    assert repeated["pin_line"] == f'"TR_ASYNC_SETTLE_SHADOW_WORKSPACES={result["workspace_id"]}"'
    assert store.credit_money_snapshot(result["workspace_id"])[0] == 100_000_000


@pytest.mark.parametrize("damage", ["none", "app", "scope", "federated", "paused", "refill", "shards", "disabled", "byok", "untyped", "monitor", "extra_workspace", "credit_shards", "workspace_name"])
def test_validator_read_only_and_fail_closed(damage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    path = tmp_path / "key"
    result = seed(store, path)
    key = store.get_key_by_hash(result["key_id"])
    workspace = store.get_workspace(result["workspace_id"])
    account = store.get_credit_account(workspace.id)
    monkeypatch.setattr(provisioning, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    if damage == "app":
        key.app_id = "app"
    elif damage == "scope":
        key.scopes = ["inference"]
    elif damage == "federated":
        key.federated_home = "home"
    elif damage == "paused":
        workspace.billing_paused = True
    elif damage == "refill":
        account.auto_refill_enabled = True
    elif damage == "shards":
        key.usage_shard_count = 2
    elif damage == "disabled":
        key.disabled = True
    elif damage == "byok":
        monkeypatch.setattr(InMemoryStore, "list_byok_providers", lambda *a: [object()])
    elif damage == "untyped":
        monkeypatch.setattr(provisioning, "typed_billing_store", lambda _: None)
    elif damage == "extra_workspace":
        store.create_workspace(workspace.owner_user_id, "unexpected")
    elif damage == "credit_shards":
        account.shard_count = 2
    elif damage == "workspace_name":
        workspace.name = "unexpected"
    elif damage == "monitor":
        key.name = "Synthetic monitor"
    before = store.credit_money_snapshot(workspace.id)
    if damage == "none":
        assert provisioning.validate(store, path.read_text())["valid"]
    else:
        with pytest.raises(ValueError):
            provisioning.validate(store, path.read_text())
    assert before == store.credit_money_snapshot(workspace.id)


def test_validate_cli_redacts_backend_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    raw = secrets.token_urlsafe(24)
    monkeypatch.setattr(provisioning.sys, "stdin", io.StringIO(raw))
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(raw)
    monkeypatch.setattr(provisioning, "validate", fail)
    assert provisioning.main(["--validate"], store=InMemoryStore()) == 1
    output = capsys.readouterr()
    if raw in output.out + output.err:
        pytest.fail("credential escaped into CLI output")


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("bridge", [False, True])
def test_deploy_secret_guard_preserves_existing_commands(present: bool, bridge: bool, tmp_path: Path) -> None:
    block = Path("scripts/deploy/_async_settle_shadow_probe_jobs.sh").read_text()
    block = block[block.index("async_settle_shadow_probe_jobs() {"):] + "\nasync_settle_shadow_probe_jobs\n"
    # Execute the actual optional deploy block with local recording functions.
    # The existing full-script harness tests separately exercise its neighbours.
    stub = r'''set -euo pipefail
calls="$1"
PRESENT="$2"
gc() {
  printf '%s\n' "$*" >> "$calls"
  if [ "$1 $2" = "secrets describe" ]; then
    [ "$PRESENT" = "true" ]; return
  fi
}
synthetic_ingest_base_for_region() { echo "https://internal.example"; }
prepare_synthetic_ingest_target() { :; }
upsert_scheduler() { printf 'scheduler %s\n' "$*" >> "$calls"; }
_REGION_LIST=(us-central1 europe-west4)
DEPLOYED_COMBINED_BRIDGE="$3"
IMAGE=image
RUN_SERVICE_ACCOUNT=worker
PRIVATE_RUN_APP_JOB_NETWORK_ARGS=()
'''
    calls_file = tmp_path / "calls"
    rendered = tmp_path / "render.sh"
    rendered.write_text(stub + block)
    result = subprocess.run(["/bin/bash", str(rendered), str(calls_file), str(present).lower(), str(bridge).lower()],  # noqa: S603 - local recording shell
                            capture_output=True, text=True, check=False)  # noqa: S603 - local stubbed shell only
    assert result.returncode == 0, result.stderr
    calls = calls_file.read_text().splitlines()
    if not present:
        assert calls == ["secrets describe " + provisioning.SECRET_NAME]
        return
    jobs = [call for call in calls if call.startswith("run jobs deploy ")]
    schedules = [call for call in calls if call.startswith("scheduler ")]
    assert len(jobs) == len(schedules) == 2
    for job in jobs:
        assert "--args=-m,trusted_router.synthetic.cli,--async-settle-shadow-probe" in job
        assert "/var/run/secrets/shadow-probe/api-key=" + provisioning.SECRET_NAME in job
        assert "synthetic-monitor-api-key" not in job
        assert "TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY=" not in job
        env_arg = job.split("--set-env-vars ", 1)[1].split(" --set-secrets", 1)[0]
        env = dict(value.split("=", 1) for value in env_arg[3:].split("|"))
        cfg = Settings(**{name[3:].lower(): value for name, value in env.items()
                          if name[3:].lower() in Settings.model_fields},
                       **{("internal_gateway_token" if bridge else "observer_internal_token"): secrets.token_urlsafe(24)})
        assert cfg.service_surface == ("combined" if bridge else "observer")
        assert not cfg.async_settle_shadow_probe_api_key
    assert all(call.endswith("*/4 * * * *") for call in schedules)


@pytest.mark.asyncio
async def test_smoke_file_only_metadata_and_no_ingest(monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path) -> None:
    key = secrets.token_urlsafe(24)
    path = tmp_path / "key"
    path.write_text(key)
    path.chmod(0o600)
    monkeypatch.setenv("TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE", str(path))
    monkeypatch.delenv("TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY", raising=False)
    cfg = Settings(environment="test", async_settle_shadow_probe_repetitions=8)
    monkeypatch.setattr(cli, "get_settings", lambda: cfg)
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host in {f"api-{r}.quillrouter.com" for r in ENCLAVE_REGIONS}
        body = json.loads(request.content)
        shape = next(s for s in SHAPES if s.body() == body)
        response = success(shape)
        # Both response APIs, buffered and streamed, contain an ID and private
        # provider text. Only the former may leave smoke output.
        def with_id(payload: dict[str, Any]) -> dict[str, Any]:
            response_payload = payload.get("response", payload)
            response_payload.update(id="provider_response", output="PRIVATE_BODY",
                                    usage={"provider_usage": {"generation_id": "gen_smoke"}})
            return payload
        if shape.stream:
            payload = json.loads(response.text.split("data: ", 1)[1].split("\n", 1)[0])
            text = "data: " + json.dumps(with_id(payload)) + "\n\ndata: [DONE]\n\n"
        else:
            text = json.dumps(with_id(response.json()))
        return httpx.Response(200, headers=dict(response.headers) | {"x-private": "PRIVATE_HEADER"}, content=text)
    original = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await cli.run_shadow_probe(once=True, no_ingest=True) == 0
    output = capsys.readouterr()
    assert not output.err
    lines = output.out.splitlines()
    assert len(lines) == len(calls) == 32
    for line in lines:
        fields = line.split()
        assert len(fields) == 5
        assert fields[0] in ENCLAVE_REGIONS and fields[1] in PROBE_NAMES
        assert fields[2] == "2xx" and fields[3].removesuffix("ms").isdigit()
        assert fields[4] == "generation_id=gen_smoke"
    assert all(secret not in output.out for secret in (key, "PRIVATE_BODY", "PRIVATE_HEADER"))
    monkeypatch.delenv("TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE")
    cfg.async_settle_shadow_probe_api_key = key
    assert await cli.run_shadow_probe(once=True, no_ingest=True) == 2
    assert len(calls) == 32


def test_probe_contract_rejects_every_extra_key() -> None:
    for shape in SHAPES:
        shape.validate_body(shape.body())
        for key in ("metadata", "user", "tools", "unknown"):
            with pytest.raises(ValueError):
                shape.validate_body(shape.body() | {key: {}})


def test_target_shards_only_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    store = InMemoryStore()
    path = tmp_path / "key"
    result = seed(store, path)
    monkeypatch.setattr(provisioning, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    monkeypatch.setattr(provisioning.sys, "stdin", io.StringIO(path.read_text()))
    assert provisioning.main(["--validate", "--target-shards", "2"], store=store) == 1
    assert store.get_credit_account(result["workspace_id"]).shard_count == 1
    assert store.get_key_by_hash(result["key_id"]).usage_shard_count == 1
    capsys.readouterr()


def test_evidence_check_bounded_metadata_only() -> None:
    from scripts.async_settle.probe_cohort_check import check
    from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE
    from trusted_router.storage_operational_analytics import analytics_surrogate
    now = dt.datetime(2026, 10, 12, 0, 5, tzinfo=dt.UTC)
    calls = []
    class Evidence:
        def day(self, kind: str, day: str) -> list[tuple[str, str]]:
            calls.append((kind, day))
            if day != now.date().isoformat():
                return []
            if kind == COUNTER:
                return [("counter", json.dumps(dict(flushed_at_us=int(now.timestamp()*1e6),
                    exclusions=[{"reason": "not_eligible", "count": 1}], drops=[{"reason": "queue_full", "count": 2}],
                    rejections=[{"reason": "store_unavailable", "count": 3}], body="PRIVATE")))]
            return [("sample", json.dumps(dict(workspace_fingerprint=analytics_surrogate("workspace", ws),
                observed_at_us=int(now.timestamp()*1e6), adapter="openai", route_type="responses", streamed=True,
                classification="exact", body="PRIVATE"))) for ws in ("cohort", "other")]
    result = check(Evidence(), "cohort", now)
    assert calls == [(kind, day) for day in ("2026-10-11", "2026-10-12") for kind in (SAMPLE, COUNTER)]
    assert len(result["buckets"]) == 8 and sum(b["compared"] for b in result["buckets"]) == 1
    assert "PRIVATE" not in json.dumps(result)
    assert all(result["writer_day_counters"][0][group] for group in ("exclusions", "drops", "rejections"))
    assert "not workspace-attributable" in result["counter_scope"]


def test_smoke_cli_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    async def smoke(**options: Any) -> int:
        called.append(options)
        return 0
    monkeypatch.setattr(cli, "run_shadow_probe", smoke)
    assert cli.main(["--async-settle-shadow-probe", "--once", "--no-ingest"]) == 0
    assert called == [{"once": True, "no_ingest": True}]
    with pytest.raises(SystemExit):
        cli.main(["--once", "--no-ingest"])


def test_apply_rejects_shard_target_before_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    monkeypatch.setattr(provisioning, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    path = tmp_path / "key"
    assert provisioning.main(["--apply", "--target-shards", "2", "--key-output-file", str(path)], store=store) == 1
    assert not path.exists()
    assert store.find_user_by_email(provisioning.cohort_names()["email"]) is None


def test_smoke_generation_identifier_is_router_metadata_only() -> None:
    assert shadow_probe._generation_id({"id": "provider_response"}) is None
    assert shadow_probe._generation_id({"generation_id": "invalid\nbody"}) is None
    assert shadow_probe._generation_id({"trustedrouter": {"generation_id": "gen_router"}}) == "gen_router"
    assert shadow_probe._generation_id({"response": {"usage": {"provider_usage": {"generation_id": "gen_router"}}}}) == "gen_router"
