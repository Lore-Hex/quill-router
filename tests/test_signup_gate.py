from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trusted_router.signup_gate import (
    GITHUB_STARTER_CREDIT_MIN_ACCOUNT_AGE,
    github_account_old_enough,
    starter_credit_for_email,
)

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_github_account_age_boundary() -> None:
    assert GITHUB_STARTER_CREDIT_MIN_ACCOUNT_AGE == timedelta(days=30)
    exactly = NOW - GITHUB_STARTER_CREDIT_MIN_ACCOUNT_AGE
    assert github_account_old_enough(_iso(exactly), now=NOW)
    assert not github_account_old_enough(_iso(exactly + timedelta(seconds=1)), now=NOW)
    assert github_account_old_enough("2011-01-25T18:44:36Z", now=NOW)
    assert github_account_old_enough("2011-01-25T18:44:36+00:00", now=NOW)


@pytest.mark.parametrize("created_at", [
    None,
    "",
    "not-a-date",
    "2011-01-25T18:44:36",  # no timezone
    "2026-10-15T00:00:00Z",  # in the future
])
def test_github_account_of_unknown_or_impossible_age_is_not_old_enough(created_at: str | None) -> None:
    assert not github_account_old_enough(created_at, now=NOW)


@pytest.mark.parametrize(("email", "credit"), [
    ("real@example.com", 300_000),
    ("real@gmail.com", 300_000),
    ("farm@hotmail.com", 0),
    ("farm@HotMail.co.uk", 0),
    ("farm@nothotmail.com", 300_000),
    ("farm@outlook.com", 0),
    ("farm@OUTLOOK.fr", 0),
    ("farm@notoutlook.com", 300_000),
])
def test_starter_credit_for_email(email: str, credit: int) -> None:
    assert starter_credit_for_email(email, 300_000) == credit
