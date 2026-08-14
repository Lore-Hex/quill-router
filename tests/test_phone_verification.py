"""Phone ownership proof.

A six-digit code is worth about 20 bits, which is nothing without limits — so
most of what matters here is the limits, and each one is asserted rather than
assumed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trusted_router import phone_verification as pv
from trusted_router.storage_models import User, utcnow


def _user() -> User:
    return User(id="user_1", email="owner@example.com")


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+1 (305) 951-1381", "+13059511381"),
            ("+1.305.951.1381", "+13059511381"),
            ("  +442071838750  ", "+442071838750"),
        ],
    )
    def test_accepts_human_formats(self, raw: str, expected: str) -> None:
        # Carriers reject anything but E.164 with a vague 400 rather than "your
        # number has dots in it", so the shape is enforced where the error can
        # actually say something useful.
        assert pv.normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "3059511381", "+1305call1381", "+123", "+" + "9" * 16])
    def test_rejects_what_a_carrier_would(self, raw: str) -> None:
        with pytest.raises(pv.PhoneNumberError):
            pv.normalize_phone(raw)


class TestVerification:
    def test_a_pending_number_is_not_reachable(self) -> None:
        # Otherwise "start verification" would itself be a way to send someone
        # a message, which is the whole thing this gate exists to prevent.
        user = _user()
        pv.begin(user, "+13059511381")

        assert user.pending_phone == "+13059511381"
        assert user.phone is None
        assert not user.phone_verified

    def test_the_right_code_promotes_the_number(self) -> None:
        user = _user()
        code = pv.begin(user, "+13059511381")

        result = pv.confirm(user, code)

        assert result.verified
        assert user.phone == "+13059511381"
        assert user.phone_verified
        assert user.phone_verified_at
        # Nothing reusable is left behind.
        assert user.pending_phone is None
        assert user.phone_code_hash is None

    def test_the_code_is_not_stored_in_the_clear(self) -> None:
        # A leaked database must not hand over the ability to verify someone
        # else's phone.
        user = _user()
        code = pv.begin(user, "+13059511381")

        assert code not in (user.phone_code_hash or "")
        assert code not in (user.phone_code_salt or "")

    def test_a_wrong_code_counts_down_and_says_so(self) -> None:
        user = _user()
        pv.begin(user, "+13059511381")

        result = pv.confirm(user, "000000")

        assert result.status == "mismatch"
        assert result.attempts_remaining == pv.MAX_ATTEMPTS - 1

    def test_exhausting_attempts_burns_the_code_immediately(self) -> None:
        # 20 bits survives unlimited guessing, so running out of attempts must
        # invalidate the code AT THAT MOMENT — not merely refuse the next guess.
        # Asserting only the next call's status passes even if the secret is
        # left sitting in the record, because a separate guard catches it.
        user = _user()
        code = pv.begin(user, "+13059511381")

        for _ in range(pv.MAX_ATTEMPTS - 1):
            assert pv.confirm(user, "000000").status == "mismatch"
        assert user.phone_code_hash is not None  # still live up to the last one

        final = pv.confirm(user, "000000")

        assert final.status == "too_many_attempts"
        # The secret itself is gone, not just refused.
        assert user.phone_code_hash is None
        assert user.pending_phone is None
        assert pv.confirm(user, code).status == "no_pending"
        assert not user.phone_verified

    def test_an_expired_code_is_refused_and_cleared(self) -> None:
        user = _user()
        code = pv.begin(user, "+13059511381")
        later = utcnow() + dt.timedelta(seconds=pv.CODE_TTL_SECONDS + 1)

        result = pv.confirm(user, code, now=later)

        assert result.status == "expired"
        assert user.pending_phone is None
        assert not user.phone_verified

    def test_confirming_without_starting_is_not_an_error_path_that_verifies(self) -> None:
        user = _user()
        assert pv.confirm(user, "123456").status == "no_pending"
        assert not user.phone_verified

    def test_a_code_only_verifies_the_number_it_was_issued_for(self) -> None:
        # Re-starting verification against a different number must invalidate
        # the old code, or a code could be replayed onto another number.
        user = _user()
        first = pv.begin(user, "+13059511381")
        pv.begin(user, "+442071838750")

        assert pv.confirm(user, first).status == "mismatch"
        assert user.phone is None


class TestResendFloor:
    def test_immediate_resend_is_refused(self) -> None:
        # Without a floor, a retry loop — or someone else's phone — gets texted
        # continuously.
        user = _user()
        pv.begin(user, "+13059511381")

        allowed, wait = pv.can_resend(user)

        assert not allowed
        assert 0 < wait <= pv.RESEND_FLOOR_SECONDS

    def test_resend_allowed_after_the_floor(self) -> None:
        user = _user()
        pv.begin(user, "+13059511381")
        later = utcnow() + dt.timedelta(seconds=pv.RESEND_FLOOR_SECONDS + 1)

        allowed, wait = pv.can_resend(user, now=later)

        assert allowed and wait == 0

    def test_a_first_send_is_always_allowed(self) -> None:
        assert pv.can_resend(_user()) == (True, 0)


class TestClear:
    def test_clearing_removes_the_number_and_the_proof(self) -> None:
        user = _user()
        code = pv.begin(user, "+13059511381")
        pv.confirm(user, code)

        pv.clear(user)

        assert user.phone is None
        assert not user.phone_verified
        assert user.phone_verified_at is None
        assert user.pending_phone is None
