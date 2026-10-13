from __future__ import annotations

import datetime as dt
import io
import json
import secrets
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
from trusted_router.synthetic.shadow_probe_contract import SHAPES, Shape

PROBE_NAMES = frozenset(shape.name for shape in SHAPES)


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


# Production field-name evidence, 2026-10-13:
# /tmp/probe-fix-author-1/production-evidence.md. Exact probe chat bodies have
# NO usage/trustedrouter fields. Even include_usage has no provider_usage.
CHAT_STREAM_USAGE = {
    "completion_tokens": 2, "cost_microdollars": 1, "inference_location": "fixture",
    "prompt_tokens": 10, "total_cost_microdollars": 1, "total_tokens": 12,
    "usage_estimated": True,
}
RESPONSES_STREAM_USAGE = {
    "cost_microdollars": 1, "inference_location": "fixture", "input_tokens": 10,
    "input_tokens_details": {}, "output_tokens": 2, "output_tokens_details": {},
    "total_cost_microdollars": 1, "total_tokens": 12, "usage_estimated": True,
}


def stream_payloads(shape: Shape, *, include_usage: bool = False) -> list[dict[str, Any]]:
    if shape.route == "/chat/completions":
        payloads = [{"object": "chat.completion.chunk", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]} for _ in range(3)]
        payloads.append({"object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop"}]})
        if include_usage:
            payloads.append({"object": "chat.completion.chunk", "choices": [], "usage": dict(CHAT_STREAM_USAGE)})
        return payloads
    return [{"type": "response.output_text.delta", "delta": "OK"} for _ in range(9)] + [
        {"type": "response.completed", "response": {"status": "completed", "usage": dict(RESPONSES_STREAM_USAGE)}}]


def stream_reply(shape: Shape, payloads: list[dict[str, Any]], *, done: bool = True) -> httpx.Response:
    text = "".join("data: " + json.dumps(payload) + "\n\n" for payload in payloads)
    if done and shape.route == "/chat/completions":
        text += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)


def success(shape: Shape) -> httpx.Response:
    if shape.stream:
        return stream_reply(shape, stream_payloads(shape))
    payload = ({"choices": [{"finish_reason": "stop"}]} if shape.route == "/chat/completions"
               else {"type": "response.completed", "response": {"status": "completed"}})
    inner = payload.get("response", payload)
    inner["usage"] = {"input_tokens": 10, "output_tokens": 2,
                      "provider_usage": {"generation_id": "gen-" + __import__("uuid").uuid4().hex}}
    return httpx.Response(200, json=payload)




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




def seed(store: InMemoryStore, path: Path) -> dict[str, Any]:
    names = provisioning.cohort_names()
    result = provision(store, **{k: names[k] for k in ("email", "workspace_name", "key_name", "funding_event_id")},
                       funding_microdollars=100_000_000, target_shards=1, apply=True, key_output_file=path)
    return result


def test_provision_dry_run_fixed_identity(capsys: Any) -> None:
    store = InMemoryStore()
    assert provisioning.main([], store=store) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["secret_name"] == provisioning.SECRET_NAME
    assert result["email"] == "async-settle-shadow-probe@trustedrouter.internal"
    assert result["funding_microdollars"] == 100_000_000
    assert store.find_user_by_email(result["email"]) is None


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
    assert provisioning.main(["--apply", "--key-output-file", str(path)], store=store) == 0
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
                classification="exact", authorization_id="auth-1", body="PRIVATE"))) for ws in ("cohort", "other")]
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
    assert called == [{"once": True, "no_ingest": True, "generation_ids_out": None}]
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
