"""At-most-once onboarding reminders for accounts without a successful call."""

from __future__ import annotations

import datetime as dt
import html
import logging
from dataclasses import dataclass

from trusted_router.config import Settings
from trusted_router.services.email import EmailMessage, EmailService, get_email_service
from trusted_router.storage import STORE
from trusted_router.storage_models import AcquisitionAttribution, iso_now

log = logging.getLogger(__name__)

_STAGES = frozenset({"10m", "24h"})


@dataclass(frozen=True)
class ActivationReminderPassResult:
    inspected: int = 0
    due: int = 0
    sent: int = 0
    skipped_activated: int = 0
    skipped_no_email: int = 0
    failed: int = 0


def run_activation_reminder_pass(
    settings: Settings,
    *,
    now: str | None = None,
    limit: int = 200,
    email_service: EmailService | None = None,
) -> ActivationReminderPassResult:
    """Process a bounded prefix of the sortable reminder queue.

    The acquisition milestone is claimed before SES is called. This makes the
    send at-most-once across every warm control-plane replica. A reminder is
    also rejected atomically when first-use has already been recorded.
    """
    occurred_at = now or iso_now()
    current = _parse_iso(occurred_at)
    service = email_service or get_email_service(settings)
    tasks = STORE.list_activation_reminders(limit=max(1, min(limit, 1_000)))
    delete_ids: list[str] = []
    counts = {
        "inspected": len(tasks),
        "due": 0,
        "sent": 0,
        "skipped_activated": 0,
        "skipped_no_email": 0,
        "failed": 0,
    }

    for task in tasks:
        try:
            due_at = _parse_iso(task.due_at)
        except ValueError:
            log.warning(
                "activation_reminder.invalid_due_at",
                extra={"stage": task.stage},
            )
            delete_ids.append(task.id)
            counts["failed"] += 1
            continue
        if due_at > current:
            break
        counts["due"] += 1
        delete_ids.append(task.id)
        if task.stage not in _STAGES:
            counts["failed"] += 1
            continue

        recipient = _workspace_owner_email(task.workspace_id)
        if recipient is None:
            counts["skipped_no_email"] += 1
            continue

        record, claimed = STORE.claim_activation_reminder(
            task.workspace_id,
            task.stage,
            occurred_at=occurred_at,
        )
        if not claimed or record is None:
            counts["skipped_activated"] += 1
            continue

        message = build_activation_reminder_email(
            settings,
            recipient=recipient,
            stage=task.stage,
            attribution=record,
        )
        try:
            accepted = service.send(message)
        except Exception:  # noqa: BLE001 - reminders must never kill the scheduler.
            log.exception(
                "activation_reminder.send_failed",
                extra={"stage": task.stage},
            )
            counts["failed"] += 1
            continue
        if accepted:
            counts["sent"] += 1
        else:
            counts["failed"] += 1

    if delete_ids:
        STORE.delete_activation_reminders(delete_ids)
    result = ActivationReminderPassResult(
        inspected=counts["inspected"],
        due=counts["due"],
        sent=counts["sent"],
        skipped_activated=counts["skipped_activated"],
        skipped_no_email=counts["skipped_no_email"],
        failed=counts["failed"],
    )
    if result.due:
        log.info(
            "activation_reminder.pass_completed",
            extra={
                "inspected": result.inspected,
                "due": result.due,
                "sent": result.sent,
                "skipped_activated": result.skipped_activated,
                "skipped_no_email": result.skipped_no_email,
                "failed": result.failed,
            },
        )
    return result


def build_activation_reminder_email(
    settings: Settings,
    *,
    recipient: str,
    stage: str,
    attribution: AcquisitionAttribution,
) -> EmailMessage:
    if stage not in _STAGES:
        raise ValueError("unsupported activation reminder stage")
    origin = f"https://{settings.trusted_domain}"
    quickstart_url = f"{origin}/console/api-keys#new-api-key"
    docs_url = f"{origin}/for-developers"
    if stage == "10m":
        subject = "Your TrustedRouter key is ready to test"
        lead = "Make your first API call in about a minute."
    else:
        subject = "Make your first TrustedRouter API call"
        lead = "Your account is ready, but it has not made a successful API call yet."

    text_body = (
        f"{lead}\n\n"
        f"Open your API Keys page: {quickstart_url}\n\n"
        "Create a new key, then copy the setup for Python, JavaScript, curl, "
        "Claude Code, or Codex. Your first successful request will complete "
        "account setup.\n\n"
        "No card is required when your account has trial credit. If your balance "
        "is empty, the page will take you to Credits first.\n\n"
        f"Developer quickstart: {docs_url}\n\n"
        "You are receiving this operational email because you created a TrustedRouter account."
    )
    safe_quickstart_url = html.escape(quickstart_url, quote=True)
    safe_docs_url = html.escape(docs_url, quote=True)
    html_body = (
        f"<p>{html.escape(lead)}</p>"
        f'<p><a href="{safe_quickstart_url}">Run my first API request</a></p>'
        "<p>Create a new key, then copy the setup for Python, JavaScript, curl, "
        "Claude Code, or Codex. Your first successful request will complete "
        "account setup.</p>"
        "<p>No card is required when your account has trial credit. If your balance "
        "is empty, the page will take you to Credits first.</p>"
        f'<p><a href="{safe_docs_url}">Open the developer quickstart</a></p>'
        "<p><small>You are receiving this operational email because you created a "
        "TrustedRouter account.</small></p>"
    )
    touch = attribution.last_touch
    return EmailMessage(
        to=recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        reply_to=settings.support_email,
        mail_class=f"activation_{stage}",
        acquisition_source=touch.get("utm_source"),
        acquisition_medium=touch.get("utm_medium"),
        acquisition_campaign=touch.get("utm_campaign"),
    )


def _workspace_owner_email(workspace_id: str) -> str | None:
    workspace = STORE.get_workspace(workspace_id)
    if workspace is None or workspace.deleted:
        return None
    user = STORE.get_user(workspace.owner_user_id)
    if user is None or not user.email:
        return None
    return user.email.strip().lower() or None


def _parse_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


__all__ = [
    "ActivationReminderPassResult",
    "build_activation_reminder_email",
    "run_activation_reminder_pass",
]
