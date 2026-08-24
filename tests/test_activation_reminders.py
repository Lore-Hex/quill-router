from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.acquisition import record_successful_api_call
from trusted_router.config import Settings
from trusted_router.services.activation_reminders import run_activation_reminder_pass
from trusted_router.services.email import EmailMessage
from trusted_router.storage import STORE
from trusted_router.storage_models import AcquisitionAttribution, iso_now

ROOT = Path(__file__).resolve().parents[1]


class CapturingEmailService:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.messages: list[EmailMessage] = []
        self._lock = threading.Lock()

    def send(self, message: EmailMessage) -> bool:
        with self._lock:
            self.messages.append(message)
        return self.accepted


def _signup(client: TestClient, email: str = "activation@example.com") -> dict[str, object]:
    response = client.post("/v1/signup", json={"email": email, "name": "Activation"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_signup_schedules_sorted_ten_minute_and_day_reminders(
    client: TestClient,
) -> None:
    payload = _signup(client)
    tasks = STORE.list_activation_reminders(limit=10)

    assert [task.stage for task in tasks] == ["10m", "24h"]
    assert {task.workspace_id for task in tasks} == {payload["workspace_id"]}
    assert tasks[0].id < tasks[1].id
    assert tasks[0].created_at == tasks[1].created_at


def test_due_reminder_sends_once_without_key_or_content(
    client: TestClient,
    test_settings: Settings,
) -> None:
    payload = _signup(client)
    raw_key = str(payload["key"])
    due = STORE.list_activation_reminders(limit=10)[0]
    email = CapturingEmailService()

    result = run_activation_reminder_pass(
        test_settings,
        now=due.due_at,
        email_service=email,  # type: ignore[arg-type]
    )
    replay = run_activation_reminder_pass(
        test_settings,
        now=due.due_at,
        email_service=email,  # type: ignore[arg-type]
    )

    assert result.sent == 1
    assert replay.sent == 0
    assert len(email.messages) == 1
    message = email.messages[0]
    rendered = "\n".join(
        [message.subject, message.text_body, message.html_body or ""]
    )
    assert message.mail_class == "activation_10m"
    assert raw_key not in rendered
    assert "prompt" not in rendered.lower()
    assert "output" not in rendered.lower()
    assert "$0.30" not in rendered
    assert "Claude Code" in rendered
    assert "Codex" in rendered
    assert "/console/api-keys#new-api-key" in rendered
    assert [task.stage for task in STORE.list_activation_reminders(limit=10)] == ["24h"]


def test_successful_call_cancels_every_due_reminder(
    client: TestClient,
    test_settings: Settings,
) -> None:
    payload = _signup(client)
    workspace_id = str(payload["workspace_id"])
    tasks = STORE.list_activation_reminders(limit=10)
    record_successful_api_call(
        workspace_id,
        model="trustedrouter/cheap",
        provider="test-provider",
        occurred_at=tasks[0].due_at,
    )
    email = CapturingEmailService()

    result = run_activation_reminder_pass(
        test_settings,
        now=tasks[-1].due_at,
        email_service=email,  # type: ignore[arg-type]
    )

    assert result.due == 2
    assert result.skipped_activated == 2
    assert result.sent == 0
    assert email.messages == []
    assert STORE.list_activation_reminders(limit=10) == []


def test_wallet_only_account_reminder_has_no_recipient(
    test_settings: Settings,
) -> None:
    user = STORE.create_wallet_user("0x1234567890123456789012345678901234567890")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    signup_at = (
        dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    assert STORE.create_acquisition_attribution(
        AcquisitionAttribution(
            workspace_id=workspace.id,
            anonymous_id="wallet-activation",
            first_touch={"utm_source": "direct"},
            last_touch={"utm_source": "direct"},
            signup_provider="wallet",
            signup_at=signup_at,
        )
    )
    email = CapturingEmailService()

    result = run_activation_reminder_pass(
        test_settings,
        now=iso_now(),
        email_service=email,  # type: ignore[arg-type]
    )

    assert result.skipped_no_email == 2
    assert result.sent == 0
    assert email.messages == []


def test_concurrent_regional_passes_claim_one_send(
    client: TestClient,
    test_settings: Settings,
) -> None:
    _signup(client)
    due = STORE.list_activation_reminders(limit=10)[0]
    email = CapturingEmailService()

    def run() -> int:
        result = run_activation_reminder_pass(
            test_settings,
            now=due.due_at,
            email_service=email,  # type: ignore[arg-type]
        )
        return result.sent

    with ThreadPoolExecutor(max_workers=6) as pool:
        sent = list(pool.map(lambda _index: run(), range(12)))

    assert sum(sent) == 1
    assert len(email.messages) == 1


def test_spanner_attribution_and_reminders_share_one_create_transaction() -> None:
    store, _database, _bigtable = make_fake_store()
    signup_at = "2026-08-06T12:00:00Z"
    record = AcquisitionAttribution(
        workspace_id="ws-spanner-reminder",
        anonymous_id="anon-spanner-reminder",
        first_touch={"utm_source": "google"},
        last_touch={"utm_source": "google"},
        signup_provider="email",
        signup_at=signup_at,
    )

    assert store.create_acquisition_attribution(record) is True
    assert store.create_acquisition_attribution(record) is False
    tasks = store.list_activation_reminders(limit=10)
    assert [task.stage for task in tasks] == ["10m", "24h"]

    stored, claimed = store.claim_activation_reminder(
        record.workspace_id,
        "10m",
        occurred_at="2026-08-06T12:10:00Z",
    )
    assert stored is not None
    assert claimed is True
    _, replay_claimed = store.claim_activation_reminder(
        record.workspace_id,
        "10m",
        occurred_at="2026-08-06T12:11:00Z",
    )
    assert replay_claimed is False


def test_reminder_worker_is_off_by_default_and_in_production_rollout() -> None:
    settings = Settings(_env_file=None)
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")

    assert settings.activation_reminder_interval_seconds == 0
    assert '"TR_ACTIVATION_REMINDER_INTERVAL_SECONDS=0"' in rollout
