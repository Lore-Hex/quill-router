"""Alert-vs-Limit budget mode: alert mode (default) never blocks — it emails the
workspace owner when a window is crossed (once per window); limit mode 429s."""

from __future__ import annotations

import pytest

from trusted_router.config import Settings
from trusted_router.services.budget_alerts import (
    build_budget_alert_email,
    maybe_send_budget_alerts,
)
from trusted_router.spend_windows import KeyWindowLimitExceeded
from trusted_router.storage import STORE


def _key(*, alert_only: bool, email: str = "owner@example.com"):
    user = STORE.ensure_user(email)
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=ws.id, name="k", creator_user_id=user.id,
        limit_daily_microdollars=1_000, budget_alert_only=alert_only,
    )
    return ws, key


def test_alert_mode_never_blocks_over_budget() -> None:
    STORE.reset()
    _ws, key = _key(alert_only=True)  # the default
    STORE.api_keys.add_usage(key.hash, 5_000, is_byok=False)  # way over $0.001 daily
    # Alert mode: authorize/reserve must NOT raise — the app keeps working.
    STORE.reserve_key_limit(key.hash, 500, usage_type="Credits")


def test_limit_mode_blocks_over_budget() -> None:
    STORE.reset()
    _ws, key = _key(alert_only=False)
    STORE.api_keys.add_usage(key.hash, 5_000, is_byok=False)
    with pytest.raises(KeyWindowLimitExceeded) as exc:
        STORE.reserve_key_limit(key.hash, 500, usage_type="Credits")
    assert exc.value.window == "daily"


class _FakeEmail:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> bool:
        self.sent.append(message)
        return True


def test_alert_emails_once_per_window_then_dedups(monkeypatch) -> None:
    STORE.reset()
    fake = _FakeEmail()
    monkeypatch.setattr(
        "trusted_router.services.budget_alerts.get_email_service", lambda _s: fake
    )
    ws, key = _key(alert_only=True, email="alertme@example.com")
    STORE.api_keys.add_usage(key.hash, 5_000, is_byok=False)  # cross the $0.001 daily budget
    settings = Settings(environment="test")

    maybe_send_budget_alerts(api_key_hash=key.hash, workspace_id=ws.id, settings=settings)
    assert len(fake.sent) == 1
    assert "daily" in fake.sent[0].subject
    assert fake.sent[0].to == "alertme@example.com"

    # Same window again -> deduped, no second email.
    maybe_send_budget_alerts(api_key_hash=key.hash, workspace_id=ws.id, settings=settings)
    assert len(fake.sent) == 1
    # The dedup marker is on the key.
    assert STORE.get_key_by_hash(key.hash).budget_alerted.get("daily")


def test_limit_mode_key_sends_no_alert(monkeypatch) -> None:
    STORE.reset()
    fake = _FakeEmail()
    monkeypatch.setattr(
        "trusted_router.services.budget_alerts.get_email_service", lambda _s: fake
    )
    ws, key = _key(alert_only=False)
    STORE.api_keys.add_usage(key.hash, 5_000, is_byok=False)
    maybe_send_budget_alerts(
        api_key_hash=key.hash, workspace_id=ws.id, settings=Settings(environment="test")
    )
    assert fake.sent == []  # limit-mode keys block instead; no alert email


def test_under_budget_sends_no_alert(monkeypatch) -> None:
    STORE.reset()
    fake = _FakeEmail()
    monkeypatch.setattr(
        "trusted_router.services.budget_alerts.get_email_service", lambda _s: fake
    )
    ws, key = _key(alert_only=True)
    STORE.api_keys.add_usage(key.hash, 500, is_byok=False)  # under $0.001
    maybe_send_budget_alerts(
        api_key_hash=key.hash, workspace_id=ws.id, settings=Settings(environment="test")
    )
    assert fake.sent == []


def test_build_budget_alert_email_content() -> None:
    msg = build_budget_alert_email(
        to="x@example.com", key_name="prod", workspace_name="Acme",
        crossings=[("daily", 5_000_000, 1_000_000)],
    )
    assert "prod" in msg.subject and "daily" in msg.subject
    assert "still working" in msg.text_body.lower()
    assert "$5" in msg.text_body and "$1" in msg.text_body


def _authorize(client, key_hash: str):
    return client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        },
    )


def test_route_alert_mode_over_budget_authorizes_but_limit_mode_429s(client) -> None:
    """The strongest guarantee: a full /internal/gateway/authorize over an
    already-crossed daily budget returns 200 in alert mode (app keeps working)
    and 429 in limit mode."""
    STORE.reset()
    user = STORE.ensure_user("routealert@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(ws.id, 5_000_000, "seed-credits")
    _raw, key = STORE.create_api_key(
        workspace_id=ws.id, name="k", creator_user_id=user.id,
        limit_daily_microdollars=1, budget_alert_only=True,  # alert mode
    )
    STORE.api_keys.add_usage(key.hash, 100_000, is_byok=False)  # daily usage >> 1 micro

    alert = _authorize(client, key.hash)
    assert alert.status_code == 200, alert.text  # alert mode never blocks

    STORE.update_key(key.hash, {"budget_alert_only": False})  # flip to limit mode
    limited = _authorize(client, key.hash)
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["type"] == "key_window_limit_exceeded"
