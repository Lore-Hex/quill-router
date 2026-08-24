"""The decline-reason boundary: operators see it, the applicant never does."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.identity_guidance import (
    _DECLINED_MESSAGE,
    _RESUBMISSION_ADVICE,
    guidance_for,
)
from trusted_router.main import create_app
from trusted_router.routes.internal.veriff import MAX_REASON_LENGTH, decision_reason
from trusted_router.storage import STORE

SHARED_SECRET = "veriff-guidance-test-secret"  # noqa: S105 - test fixture.

#: Every granular decline reason Veriff documents, verbatim. None of these
#: strings may ever appear in a response the applicant can read.
FRAUD_REASONS: list[tuple[int, str]] = [
    (503, "Suspected document tampering"),
    (504, "Person showing suspicious behaviour"),
    (505, "Known fraud"),
    (506, "Velocity/abuse duplicated user"),
    (515, "Suspected document tampering (screen)"),
    (516, "Person is not present during the session"),
    (517, "Presented a printout of the document"),
    (518, "Presented a device screen"),
    (526, "Photos are not genuine"),
]


def _payload(
    *,
    status: str,
    code: int,
    vendor_data: str,
    reason: str | None = None,
    reason_code: int | None = None,
) -> bytes:
    verification: dict[str, Any] = {
        "id": "session-one",
        "status": status,
        "code": code,
        "vendorData": vendor_data,
        "person": {"firstName": "Ada", "lastName": "Lovelace"},
    }
    if reason is not None:
        verification["reason"] = reason
    if reason_code is not None:
        verification["reasonCode"] = reason_code
    return json.dumps({"verification": verification}, separators=(",", ":")).encode()


def _signature(raw: bytes) -> str:
    return hmac.new(SHARED_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _client_and_user(email: str) -> tuple[TestClient, Any]:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key=SHARED_SECRET,
    )
    client = TestClient(create_app(settings, init_observability=False))
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="identity guidance",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    STORE.set_user_identity_status(
        user.id,
        status="pending",
        session_id="session-one",
        session_url="https://verify.example/session-one",
        increment_attempts=True,
    )
    return client, user


def _post(client: TestClient, raw: bytes) -> httpx.Response:
    return client.post(
        "/v1/internal/veriff/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-hmac-signature": _signature(raw),
        },
    )


@pytest.mark.parametrize(("reason_code", "reason"), FRAUD_REASONS)
def test_decline_stores_the_reason_but_never_shows_it(
    reason_code: int, reason: str
) -> None:
    """The webhook keeps the fraud signal; the page shows the neutral text.

    This is the whole point of the split. Printing "Presented a device screen"
    tells whoever did it exactly which detector fired and what to change on the
    next attempt, which is a free lesson for the one population we least want
    to teach. Operators still need the signal, so it is stored.
    """
    client, user = _client_and_user(f"decline-{reason_code}@example.com")
    raw = _payload(
        status="declined",
        code=9102,
        vendor_data=user.id,
        reason=reason,
        reason_code=reason_code,
    )
    assert _post(client, raw).status_code == 200

    stored = STORE.get_user(user.id)
    assert stored is not None
    assert stored.identity_status == "declined"
    assert stored.veriff_decision_reason == reason
    assert stored.veriff_decision_reason_code == reason_code

    guidance = guidance_for("declined", reason_code=reason_code)
    assert guidance is not None
    assert guidance.detail == _DECLINED_MESSAGE
    assert guidance.reason_shown is False
    # Veriff's own wording never appears verbatim.
    assert reason.lower() not in guidance.detail.lower()


def test_every_decline_reason_produces_identical_copy() -> None:
    """No reason code may fork the decline text — that fork IS the leak.

    This is the load-bearing test, not the verbatim check above. The checklist
    does name printouts and phone screens, which is also how Veriff words 517
    and 518 — and that is fine precisely because someone declined for 505
    reads the identical sentence. Constant copy carries zero information about
    which detector fired. The day someone tailors this message per code, the
    signal leaks even if no Veriff wording is copied, so the invariant to hold
    is sameness, not vocabulary.
    """
    messages = {
        guidance_for("declined", reason_code=code).detail  # type: ignore[union-attr]
        for code, _ in FRAUD_REASONS
    }
    assert messages == {_DECLINED_MESSAGE}
    assert guidance_for("declined", reason_code=None).detail == _DECLINED_MESSAGE  # type: ignore[union-attr]


def test_resubmission_shows_the_specific_reason() -> None:
    """Veriff strongly advises telling people about resubmission reasons."""
    guidance = guidance_for("resubmission_requested", reason_code=205)
    assert guidance is not None
    assert guidance.reason_shown is True
    assert "glare" in guidance.detail.lower()

    unknown = guidance_for("resubmission_requested", reason_code=999)
    assert unknown is not None
    assert unknown.reason_shown is False
    assert unknown.detail  # falls back rather than showing nothing


def test_resubmission_advice_is_actionable() -> None:
    """Every canned line tells the person what to DO, not just what failed."""
    for text in _RESUBMISSION_ADVICE.values():
        assert text.endswith(".")
        assert len(text.split(".")) >= 3, text  # a diagnosis AND an instruction


def test_approved_and_unknown_statuses_say_nothing() -> None:
    assert guidance_for("approved") is None
    assert guidance_for("pending") is None
    assert guidance_for("none") is None
    assert guidance_for("") is None


def test_expired_gets_its_own_copy() -> None:
    guidance = guidance_for("expired")
    assert guidance is not None
    assert "expired" in guidance.detail.lower()
    assert guidance.detail != _DECLINED_MESSAGE


def test_console_page_shows_neutral_copy_for_a_declined_user() -> None:
    """Render the actual page: the reason must not be in the HTML."""
    client, user = _client_and_user("decline-console@example.com")
    raw = _payload(
        status="declined",
        code=9102,
        vendor_data=user.id,
        reason="Presented a device screen",
        reason_code=518,
    )
    assert _post(client, raw).status_code == 200

    page = client.get("/console/account/verification")
    assert page.status_code == 200
    body = page.text
    assert "device screen" not in body.lower()
    assert "518" not in body
    stored = STORE.get_user(user.id)
    assert stored is not None and stored.veriff_decision_reason is not None
    assert stored.veriff_decision_reason.lower() not in body.lower()
    assert "hold the physical document" in body.lower()
    assert "That attempt did not pass" in body


def test_resubmission_page_shows_the_specific_advice() -> None:
    client, user = _client_and_user("resub-console@example.com")
    raw = _payload(
        status="resubmission_requested",
        code=9103,
        vendor_data=user.id,
        reason="Glare on the document",
        reason_code=205,
    )
    assert _post(client, raw).status_code == 200

    page = client.get("/console/account/verification")
    assert page.status_code == 200
    assert "tilt it away from the light" in page.text.lower()


def test_reason_parsing_is_defensive() -> None:
    """Veriff authors `reason` free-hand, so it is bounded and type-checked."""
    assert decision_reason({}) == (None, None)
    assert decision_reason({"reason": "   ", "reasonCode": None}) == (None, None)
    assert decision_reason({"reason": "x" * 900})[0] == "x" * MAX_REASON_LENGTH
    assert decision_reason({"reasonCode": "518"})[1] == 518
    assert decision_reason({"reasonCode": "not-a-number"})[1] is None
    assert decision_reason({"reasonCode": True})[1] is None
    assert decision_reason({"reasonCode": {"nested": 1}})[1] is None


def test_missing_reason_does_not_erase_a_stored_one() -> None:
    """A later decision with no reason must not blank the operator's evidence."""
    client, user = _client_and_user("reason-keep@example.com")
    first = _payload(
        status="declined",
        code=9102,
        vendor_data=user.id,
        reason="Known fraud",
        reason_code=505,
    )
    assert _post(client, first).status_code == 200

    STORE.set_user_identity_status(user.id, status="pending", session_id="session-one")
    second = _payload(status="declined", code=9102, vendor_data=user.id)
    assert _post(client, second).status_code == 200

    stored = STORE.get_user(user.id)
    assert stored is not None
    assert stored.veriff_decision_reason == "Known fraud"
    assert stored.veriff_decision_reason_code == 505
