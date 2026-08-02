from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from trusted_router.routes import public as public_routes
from trusted_router.services.email import EmailMessage


@pytest.fixture(autouse=True)
def reset_support_rate_limit() -> None:
    public_routes._INQUIRY_HITS.clear()


def _payload(**overrides: str) -> dict[str, str]:
    payload = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "category": "api",
        "request_id": "req_support_123",
        "subject": "Streaming request failed",
        "message": "The request returned 503 at 12:05 UTC.",
        "website": "",
    }
    payload.update(overrides)
    return payload


def test_support_submission_sends_to_help_with_reply_to(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent_messages: list[EmailMessage] = []

    class FakeEmailService:
        def send(self, message: EmailMessage) -> bool:
            sent_messages.append(message)
            return True

    monkeypatch.setattr(
        public_routes,
        "get_email_service",
        lambda _settings: FakeEmailService(),
    )
    support_text = "Sensitive support marker that must not enter application logs."
    with caplog.at_level(logging.INFO, logger="trusted_router.routes.public"):
        response = client.post(
            "/support/inquiry",
            json=_payload(message=support_text),
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert message.to == "help@trustedrouter.com"
    assert message.reply_to == "ada@example.com"
    assert message.subject == (
        "TrustedRouter support: API and routing: Streaming request failed"
    )
    assert "req_support_123" in message.text_body
    assert support_text in message.text_body
    assert all(support_text not in record.getMessage() for record in caplog.records)
    assert any(
        record.getMessage().startswith("support_inquiry.sent category=api ")
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"name": ""},
        {"email": "not-an-email"},
        {"subject": ""},
        {"message": ""},
        {"category": "unsupported"},
    ),
)
def test_support_submission_rejects_invalid_fields(
    client: TestClient,
    overrides: dict[str, str],
) -> None:
    response = client.post("/support/inquiry", json=_payload(**overrides))

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": "missing_fields"}


def test_support_honeypot_is_accepted_without_sending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingIfCalledEmailService:
        def send(self, _message: EmailMessage) -> bool:
            raise AssertionError("honeypot submission must not send email")

    monkeypatch.setattr(
        public_routes,
        "get_email_service",
        lambda _settings: FailingIfCalledEmailService(),
    )

    response = client.post(
        "/support/inquiry",
        json=_payload(website="https://spam.example"),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_support_delivery_failure_tells_user_to_use_email(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RefusingEmailService:
        def send(self, _message: EmailMessage) -> bool:
            return False

    monkeypatch.setattr(
        public_routes,
        "get_email_service",
        lambda _settings: RefusingEmailService(),
    )

    response = client.post("/support/inquiry", json=_payload())

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "delivery_unavailable"}


def test_support_submission_is_rate_limited(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEmailService:
        def send(self, _message: EmailMessage) -> bool:
            return True

    monkeypatch.setattr(
        public_routes,
        "get_email_service",
        lambda _settings: FakeEmailService(),
    )

    for index in range(5):
        response = client.post(
            "/support/inquiry",
            json=_payload(subject=f"Support request {index}"),
        )
        assert response.status_code == 200

    limited = client.post("/support/inquiry", json=_payload())
    assert limited.status_code == 429
    assert limited.json() == {"ok": False, "error": "rate_limited"}
