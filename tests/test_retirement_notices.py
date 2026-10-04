from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trusted_router import provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_lifecycle import _Retirement
from trusted_router.services import retirement_notices as notices
from trusted_router.services.email import EmailMessage, EmailService
from trusted_router.storage import STORE

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
MODEL = "deepseek/deepseek-v4-flash"


class CapturingEmailService(EmailService):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.messages: list[EmailMessage] = []
        self.accepted = True
        self.error = False
        self._lock = threading.Lock()

    def send(self, message: EmailMessage) -> bool:
        with self._lock:
            self.messages.append(message)
        if self.error:
            raise RuntimeError("send failed")
        return self.accepted


@pytest.fixture
def setup_notice(monkeypatch, test_settings):
    settings = test_settings.model_copy(update={"retirement_notices_mode": "send"})
    retirement = _Retirement("tinfoil", frozenset({MODEL}), frozenset({"upstream"}), NOW + timedelta(days=14))
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (retirement,))
    endpoints = {
        "tinfoil": ModelEndpoint("tinfoil", MODEL, "tinfoil", "Credits", "upstream"),
        "near-ai": ModelEndpoint("near-ai", MODEL, "near-ai", "Credits", "upstream"),
    }
    monkeypatch.setattr(notices, "MODEL_ENDPOINTS", endpoints)
    user = STORE.ensure_user("owner@example.com", email_verified=True)
    workspace = STORE.create_workspace(user.id, "Private workspace name")
    analytics = SimpleNamespace(route_workspaces=Mock(return_value=[workspace.id]))
    email = CapturingEmailService(settings)
    return SimpleNamespace(
        settings=settings, retirement=retirement, endpoints=endpoints, user=user,
        workspace=workspace, analytics=analytics, email=email,
    )


def run(case, *, now=NOW):
    return notices.run_retirement_notice_pass(
        case.settings, now=now, analytics=case.analytics, email_service=case.email,
    )


def test_send_groups_models_and_retirements_per_workspace_and_retries_once(setup_notice, monkeypatch):
    case = setup_notice
    second_model = "qwen/qwen3.6-27b"
    first = replace(case.retirement, model_ids=frozenset({MODEL, second_model}))
    second = replace(case.retirement, provider="near-ai", effective_at=NOW + timedelta(days=16))
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (first, second))
    result = run(case)
    assert result == notices.RetirementNoticePassResult(workspaces=1, claimed=2, sent=1)
    assert run(case) == notices.RetirementNoticePassResult(workspaces=1)
    assert len(case.email.messages) == 1
    message = case.email.messages[0]
    assert message.to == "owner@example.com"
    assert message.mail_class == "retirement_notice"
    assert message.sender_profile == "alerts"
    assert message.text_body.count("retires at") == 3
    assert second_model in message.text_body
    assert "Tinfoil (tinfoil)" in message.text_body
    assert "2026-09-15 12:00:00 UTC" in message.text_body
    assert "2026-09-17 12:00:00 UTC" in message.text_body
    assert "last 30 days" in message.text_body
    assert "/models" in message.text_body
    assert case.workspace.id in message.text_body
    assert message.html_body.count("<li>") == 3
    assert len(case.analytics.route_workspaces.call_args_list) == 6
    assert case.analytics.route_workspaces.call_args_list[0].kwargs == {
        "provider": "tinfoil", "model": MODEL,
        "start_at": (NOW - timedelta(days=30)).isoformat(), "end_at": NOW.isoformat(),
    }


def test_concurrent_passes_claim_the_entire_group(setup_notice, monkeypatch):
    case = setup_notice
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (
        case.retirement, replace(case.retirement, provider="near-ai"),
    ))
    barrier = threading.Barrier(6)

    def concurrent_run(_):
        barrier.wait()
        return run(case)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(concurrent_run, range(6)))
    assert sum(result.sent for result in results) == 1
    assert sum(result.claimed for result in results) == 2
    assert len(case.email.messages) == 1
    assert case.email.messages[0].text_body.count("retires at") == 2


def test_each_workspace_only_hears_about_the_models_it_used(setup_notice, monkeypatch):
    case = setup_notice
    other_model = "qwen/qwen3.6-27b"
    second_user = STORE.ensure_user("second@example.com", email_verified=True)
    second = STORE.create_workspace(second_user.id, "Second")
    retirement = replace(case.retirement, model_ids=frozenset({MODEL, other_model}))
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (retirement,))
    case.analytics.route_workspaces.side_effect = lambda **kwargs: (
        [case.workspace.id] if kwargs["model"] == MODEL else [second.id]
    )
    assert run(case) == notices.RetirementNoticePassResult(workspaces=2, claimed=2, sent=2)
    messages = {message.to: message.text_body for message in case.email.messages}
    assert MODEL in messages["owner@example.com"]
    assert other_model not in messages["owner@example.com"]
    assert other_model in messages["second@example.com"]
    assert MODEL not in messages["second@example.com"]


@pytest.mark.parametrize("error", [False, True])
def test_delivery_failure_remains_claimed(setup_notice, error):
    case = setup_notice
    case.email.accepted = False
    case.email.error = error
    first = run(case)
    assert first.claimed == 1
    assert first.sent == 0
    assert first.failed == 1
    case.email.accepted = True
    case.email.error = False
    assert run(case).claimed == 0
    assert len(case.email.messages) == 1


def test_claim_is_durable_before_send(setup_notice):
    case = setup_notice
    original_send = case.email.send

    def check_claim(message):
        assert STORE.claim_retirement_notices(
            case.workspace.id, [case.retirement.notice_id], occurred_at=NOW.isoformat(),
        ) == []
        return original_send(message)

    case.email.send = check_claim
    assert run(case).sent == 1


@pytest.mark.parametrize(("until", "expected"), [
    (timedelta(days=-1), 0), (timedelta(0), 0),
    (timedelta(hours=1, microseconds=-1), 0), (timedelta(hours=1), 1),
    (timedelta(days=45), 1), (timedelta(days=45, microseconds=1), 0),
])
def test_timing_window_edges(setup_notice, monkeypatch, until, expected):
    case = setup_notice
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (replace(case.retirement, effective_at=NOW + until),))
    assert run(case).sent == expected
    assert len(case.email.messages) == expected
    assert case.analytics.route_workspaces.call_count == expected


def test_off_does_no_discovery_or_claiming(setup_notice):
    case = setup_notice
    case.settings.retirement_notices_mode = "off"
    assert run(case) == notices.RetirementNoticePassResult()
    case.analytics.route_workspaces.assert_not_called()
    assert case.email.messages == []
    assert STORE.in_memory_target.retirement_notices == {}


def test_preview_sends_only_one_operator_summary_without_customer_claims(setup_notice):
    case = setup_notice
    second_user = STORE.ensure_user("second@example.com", email_verified=True)
    second_workspace = STORE.create_workspace(second_user.id, "Second secret workspace")
    case.analytics.route_workspaces.return_value.append(second_workspace.id)
    case.settings.retirement_notices_mode = "preview"
    case.settings.retirement_notices_preview_email = "operator@example.com"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run(case), range(4)))
    assert sum(result.preview_sent for result in results) == 1
    assert sum(result.sent for result in results) == 0
    [message] = case.email.messages
    assert message.to == "operator@example.com"
    assert message.mail_class == "retirement_notice_preview"
    assert message.text_body.count("1 eligible recipients") == 2
    for workspace in (case.workspace, second_workspace):
        assert workspace.id in message.text_body
        assert workspace.name not in message.text_body
    assert MODEL in message.text_body
    assert "2026-09-15 12:00:00 UTC" in message.text_body
    assert "fail over automatically" in message.text_body
    assert "owner@example.com" not in message.text_body
    assert "second@example.com" not in message.text_body
    assert STORE.in_memory_target.retirement_notices == {}
    assert run(case, now=NOW + timedelta(days=1)).preview_sent == 1
    case.settings.retirement_notices_mode = "send"
    assert run(case).sent == 2


def test_preview_without_operator_logs_ids_only(setup_notice, caplog):
    case = setup_notice
    case.settings.retirement_notices_mode = "preview"
    case.settings.retirement_notices_preview_email = None
    with caplog.at_level(logging.INFO):
        assert run(case).sent == 0
    assert case.email.messages == []
    assert case.workspace.id in caplog.text
    assert "1 eligible recipients" in caplog.text
    assert case.workspace.name not in caplog.text
    assert case.user.email not in caplog.text


def test_empty_preview_sends_nothing(setup_notice, monkeypatch):
    case = setup_notice
    case.settings.retirement_notices_mode = "preview"
    case.settings.retirement_notices_preview_email = "operator@example.com"
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", ())
    assert run(case).preview_sent == 0
    assert case.email.messages == []
    case.analytics.route_workspaces.assert_not_called()


def test_only_eligible_owners_and_admins_receive_notices(setup_notice):
    case = setup_notice
    for email, role in [
        ("admin@example.com", "admin"), ("member@example.com", "member"),
        ("blocked@example.com", "admin"),
        ("disabled@example.com", "admin"), ("suspended@example.com", "admin"),
        ("unverified@example.com", "admin"), ("no-email@example.com", "admin"),
    ]:
        [member] = STORE.add_members(case.workspace.id, [email], role)
        user = STORE.get_user(member.user_id)
        user.email_verified = email != "unverified@example.com"
        user.disabled = email == "disabled@example.com"
        user.suspended = email == "suspended@example.com"
        if email == "no-email@example.com":
            user.email = None
    STORE.block_email_sending(email="BLOCKED@example.com", reason="complaint")
    assert run(case).sent == 2
    assert sorted(message.to for message in case.email.messages) == ["admin@example.com", "owner@example.com"]


@pytest.mark.parametrize("state", ["deleted", "federated_home", "missing", "blocked"])
def test_no_eligible_recipient_does_not_claim(setup_notice, state):
    case = setup_notice
    if state == "missing":
        case.analytics.route_workspaces.return_value = ["ws-missing"]
    elif state == "blocked":
        STORE.block_email_sending(email=case.user.email, reason="bounce")
    else:
        setattr(case.workspace, state, True if state == "deleted" else "aws")
    assert run(case) == notices.RetirementNoticePassResult(workspaces=1)
    assert case.email.messages == []
    assert STORE.in_memory_target.retirement_notices == {}


def test_analytics_failure_does_not_consume_notices(setup_notice):
    case = setup_notice
    case.analytics.route_workspaces.side_effect = RuntimeError("analytics unavailable")
    with pytest.raises(RuntimeError, match="analytics unavailable"):
        run(case)
    assert STORE.in_memory_target.retirement_notices == {}
    assert case.email.messages == []


def test_partial_analytics_discovery_is_not_sent_or_claimed(setup_notice, monkeypatch):
    case = setup_notice
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (
        case.retirement, replace(case.retirement, provider="near-ai"),
    ))
    case.analytics.route_workspaces.side_effect = [[case.workspace.id], RuntimeError("query failed")]
    with pytest.raises(RuntimeError, match="query failed"):
        run(case)
    assert case.analytics.route_workspaces.call_count == 2
    assert case.email.messages == []
    assert STORE.in_memory_target.retirement_notices == {}


def test_impact_last_route_and_named_replacement(setup_notice):
    case = setup_notice
    del case.endpoints["near-ai"]
    retirement = replace(case.retirement, replacement_model_ids=("deepseek/deepseek-v4.1-flash",))
    impact = notices.retirement_impact(retirement, MODEL, now=NOW)
    assert "No routes remain" in impact
    assert "will stop working" in impact
    assert "deepseek/deepseek-v4.1-flash" in impact
    assert "not an automatic substitution" in impact
    assert "fail over automatically" not in impact


def test_impact_failover_preserves_confidential(setup_notice):
    impact = notices.retirement_impact(setup_notice.retirement, MODEL, now=NOW)
    assert "fail over automatically" in impact
    assert "near-ai (Credits)" in impact
    assert "last confidential" not in impact
    assert "No routes remain" not in impact


def test_last_confidential_route_after_other_scheduled_cutovers(setup_notice, monkeypatch):
    case = setup_notice
    # Both confidential routes still exist today. Tinfoil goes first; at the
    # later NEAR cutover it must not be advertised as a surviving alternative.
    later = replace(case.retirement, provider="near-ai", effective_at=NOW + timedelta(days=16))
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (case.retirement, later))
    case.endpoints["deepseek"] = ModelEndpoint("direct", MODEL, "deepseek", "Credits")
    earlier_impact = notices.retirement_impact(case.retirement, MODEL, now=NOW)
    assert "near-ai (Credits)" in earlier_impact
    assert "last confidential" not in earlier_impact
    impact = notices.retirement_impact(later, MODEL, now=NOW)
    assert 'last confidential (provider.min_privacy="confidential") route' in impact
    assert 'Requests requiring confidential (provider.min_privacy="confidential") will fail' in impact
    assert "will not lower their privacy requirement" in impact
    assert "deepseek (Credits)" in impact
    assert "tinfoil (Credits)" not in impact
    assert "near-ai (Credits)" not in impact


def test_impact_excludes_simultaneous_retirements_and_expired_catalog_routes(setup_notice, monkeypatch):
    case = setup_notice
    near = replace(case.retirement, provider="near-ai")
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (case.retirement, near))
    case.endpoints["expired"] = ModelEndpoint("expired", MODEL, "openai", "Credits", catalog_valid_until=NOW)
    assert "No routes remain" in notices.retirement_impact(case.retirement, MODEL, now=NOW)


def test_manifest_refresh_deadline_is_not_a_future_retirement(setup_notice):
    case = setup_notice
    case.endpoints["near-ai"] = replace(case.endpoints["near-ai"], catalog_valid_until=NOW + timedelta(hours=1))
    assert "near-ai (Credits)" in notices.retirement_impact(case.retirement, MODEL, now=NOW)


def test_impact_last_zdr_route_and_byok_qualification(setup_notice):
    case = setup_notice
    del case.endpoints["near-ai"]
    case.endpoints["openai"] = ModelEndpoint("byok", MODEL, "openai", "BYOK")
    impact = notices.retirement_impact(case.retirement, MODEL, now=NOW)
    assert 'last ZDR (provider.min_privacy="zdr") route' in impact
    assert "BYOK; requires your provider key" in impact
    assert "provider and credential settings allow them" in impact


def test_upstream_aliases_and_html_escaping(setup_notice):
    case = setup_notice
    alias = "test/<model>&alias"
    case.endpoints["alias"] = ModelEndpoint("alias", alias, "tinfoil", "Credits", "upstream")
    assert run(case).sent == 1
    [message] = case.email.messages
    assert alias in message.text_body
    assert alias not in message.html_body
    assert "test/&lt;model&gt;&amp;alias" in message.html_body
    assert message.text_body.count("retires at") == 2


def test_retirement_identity_survives_advice_and_model_list_edits(setup_notice):
    retirement = setup_notice.retirement
    assert retirement.notice_id == replace(
        retirement, model_ids=frozenset({"new-model"}), replacement_model_ids=("replacement",),
    ).notice_id
    assert retirement.notice_id != replace(retirement, effective_at=NOW).notice_id


@pytest.mark.parametrize(("mode", "surface", "enabled"), [
    ("off", "combined", False), ("preview", "combined", True),
    ("send", "control", True), ("send", "internal", False), ("send", "public", False),
])
def test_daily_worker_registration(test_settings, mode, surface, enabled):
    settings = test_settings.model_copy(update={"retirement_notices_mode": mode, "service_surface": surface})
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    workers = [callback for callback in app.router.on_startup if callback.__name__ == "_start_retirement_notice_loop"]
    assert len(workers) == int(enabled)


def test_worker_runs_at_startup_and_daily(test_settings, monkeypatch):
    settings = test_settings.model_copy(update={"retirement_notices_mode": "preview"})
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    [startup] = [callback for callback in app.router.on_startup if callback.__name__ == "_start_retirement_notice_loop"]
    tasks = []
    sleeps = []
    passes = Mock(side_effect=[RuntimeError("temporary failure"), None])
    monkeypatch.setattr(notices, "run_retirement_notice_pass", passes)
    monkeypatch.setattr(asyncio, "create_task", tasks.append)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(startup())
    assert len(tasks) == 1
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tasks[0])
    assert passes.call_count == 2
    assert 5 <= sleeps[0] <= 30
    assert sleeps[1:] == [86400, 86400]


def test_default_is_off_and_production_rollout_previews():
    assert Settings(_env_file=None).retirement_notices_mode == "off"
    rollout = (Path(__file__).resolve().parents[1] / "scripts/deploy/rollout.sh").read_text()
    # Production previews to the operator; customer delivery is a reviewed switch.
    assert '"TR_RETIREMENT_NOTICES_MODE=preview"' in rollout
    assert "TR_RETIREMENT_NOTICES_MODE=send" not in rollout
    assert '"TR_RETIREMENT_NOTICES_PREVIEW_EMAIL=joseph@jperla.com"' in rollout
    with pytest.raises(ValueError, match="retirement_notices_mode"):
        Settings(_env_file=None, retirement_notices_mode="invalid")
