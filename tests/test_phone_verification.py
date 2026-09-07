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

    @pytest.mark.parametrize(
        "raw", ["", "   ", "3059511381", "+1305call1381", "+123", "+" + "9" * 16]
    )
    def test_rejects_what_a_carrier_would(self, raw: str) -> None:
        with pytest.raises(pv.PhoneNumberError):
            pv.normalize_phone(raw)


class TestRegionalPolicy:
    @pytest.mark.parametrize("phone", ["+13059511381", "+442071838750"])
    def test_us_and_europe_can_verify_without_identity(self, phone: str) -> None:
        user = _user()

        code = pv.begin(user, phone)

        assert pv.confirm(user, code).status == "ok"

    @pytest.mark.parametrize("phone", ["+2348012345678", "+8801712345678"])
    def test_other_regions_require_identity_at_begin_and_confirm(self, phone: str) -> None:
        user = _user()

        with pytest.raises(pv.PhoneIdentityVerificationRequired):
            pv.begin(user, phone)

        user.identity_status = "approved"
        code = pv.begin(user, phone)
        user.identity_status = "none"

        assert pv.confirm(user, code).status == "identity_required"
        assert not user.phone_verified

        user.identity_status = "approved"
        assert pv.confirm(user, code).status == "ok"

    def test_caribbean_nanp_is_not_treated_as_us(self) -> None:
        user = _user()

        with pytest.raises(pv.PhoneIdentityVerificationRequired):
            pv.begin(user, "+18761234567")

        assert pv.phone_verification_requires_identity_first("+18761234567")


class TestVerification:
    def test_a_pending_number_is_not_reachable(self) -> None:
        # Otherwise "start verification" would itself be a way to send someone
        # a message, which is the whole thing this gate exists to prevent.
        user = _user()
        pv.begin(user, "+13059511381")

        assert user.pending_phone == "+13059511381"
        assert user.phone is None
        assert not user.phone_verified

    def test_begin_records_the_delivery_channel(self) -> None:
        user = _user()

        pv.begin(user, "+13059511381", channel="voice")

        assert user.phone_code_channel == "voice"

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

    def test_cancel_discards_only_the_pending_number(self) -> None:
        user = _user()
        first_code = pv.begin(user, "+13059511381", channel="voice")
        pv.confirm(user, first_code)
        pv.begin(user, "+442071838750", channel="sms")

        pv.cancel_pending(user)

        assert user.phone == "+13059511381"
        assert user.phone_verified
        assert user.pending_phone is None
        assert user.phone_code_channel is None

    def test_cancel_does_not_reset_the_resend_floor(self) -> None:
        # The floor bounds how often this account can make a phone ring. If
        # cancel cleared it, "cancel, start, cancel, start" would ring any
        # number continuously — precisely the loop the floor exists to stop.
        user = _user()
        pv.begin(user, "+13059511381", channel="voice")

        pv.cancel_pending(user)
        allowed, wait = pv.can_resend(user)

        assert user.phone_code_sent_at is not None
        assert not allowed
        assert 0 < wait <= pv.RESEND_FLOOR_SECONDS


@pytest.mark.parametrize("phone", ["+1٨761234567", "+1８761234567"])
def test_unicode_digits_rejected_at_begin(phone: str) -> None:
    with pytest.raises(pv.PhoneNumberError):
        pv.begin(_user(), phone)


@pytest.mark.parametrize("phone", ["+1٨761234567", "+1８761234567"])
def test_unicode_legacy_pending_rejected_at_confirm(phone: str) -> None:
    user = _user()
    code = pv.begin(user, "+13059511381")
    user.pending_phone = phone
    # Even identity approval must not make malformed phone numbers valid.
    user.identity_status = "approved"
    with pytest.raises(pv.PhoneNumberError):
        pv.confirm(user, code)
    assert not user.phone_verified


@pytest.mark.parametrize("phone", ["+19995551234", "+18761234567", "+18005551234"])
def test_unapproved_nanp_requires_identity_at_both_stages(phone: str) -> None:
    user = _user()
    with pytest.raises(pv.PhoneIdentityVerificationRequired):
        pv.begin(user, phone)
    user.identity_status = "approved"
    code = pv.begin(user, phone)
    user.identity_status = "none"
    assert pv.confirm(user, code).status == "identity_required"
    assert not user.phone_verified


@pytest.mark.parametrize("area", ["416", "340", "670", "671", "684", "787", "939"])
def test_canada_and_us_territories_verify_directly(area: str) -> None:
    user = _user()
    code = pv.begin(user, f"+1{area}5551234")
    assert pv.confirm(user, code).verified


@pytest.mark.parametrize("phone", ["+298123456", "+299123456"])
def test_faroe_islands_and_greenland_verify_directly(phone: str) -> None:
    user = _user()
    assert pv.confirm(user, pv.begin(user, phone)).verified


@pytest.mark.parametrize("prefix", ["375", "7", "90", "995", "374", "994"])
def test_sms_fraud_policy_exclusions(prefix: str) -> None:
    with pytest.raises(pv.PhoneIdentityVerificationRequired):
        pv.begin(_user(), f"+{prefix}123456789")


def test_round3_long_nanp_fails_closed() -> None:
    # Valid E.164 syntax with an allowed NPA, but 13 digits is not a NANP number.
    phone = pv.normalize_phone("+1416555012345")
    assert pv.phone_verification_requires_identity_first(phone)
    with pytest.raises(pv.PhoneIdentityVerificationRequired):
        pv.begin(_user(), phone)


def test_round3_nanp_overlap_is_exactly_us_territories() -> None:
    assert pv.DIRECT_PHONE_VERIFICATION_NANP_AREA_CODES & pv.CARIBBEAN_NANP_AREA_CODES == {
        "340", "670", "671", "684", "787", "939",
    }


@pytest.mark.parametrize("today", [dt.date.today(), dt.date(2027, 2, 27)])
def test_round3_nanpa_snapshot_freshness(today) -> None:
    if today >= dt.date(2027, 2, 27):
        assert "273" in pv.DIRECT_PHONE_VERIFICATION_NANP_AREA_CODES


def test_round3_quebec_overlay_is_reviewed_now() -> None:
    assert not pv.phone_verification_requires_identity_first("+12735550123")


def test_round3_successful_begin_supersedes_refused_region() -> None:
    user = _user()
    user.phone_last_refused = "+18765550123"
    pv.begin(user, "+14165550123")
    pv.cancel_pending(user)
    assert not pv.console_phone_requires_identity(user)


@pytest.mark.parametrize("backend", ["spanner", "postgres"])
def test_round3_refused_destination_is_written_in_transaction(monkeypatch, backend) -> None:
    from dataclasses import asdict

    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_postgres import PostgresStore

    cls = SpannerBigtableStore if backend == "spanner" else PostgresStore
    store = object.__new__(cls)
    user = _user()
    code = pv.begin(user, "+14165550123")
    records = {user.id: asdict(user)}
    transaction = object()

    def read(self, tx, kind, key, model, **kwargs):
        assert tx is transaction and kind == "user"
        return model(**records[key]) if key in records else None

    def write(self, tx, kind, key, value):
        assert tx is transaction and kind == "user"
        records[key] = asdict(value)

    monkeypatch.setattr(cls, "_read_entity_tx", read)
    monkeypatch.setattr(cls, "_write_entity_tx", write)
    runner = "_run_in_transaction" if backend == "spanner" else "_run_transaction"
    monkeypatch.setattr(cls, runner, lambda self, callback: callback(transaction))
    store.set_user_phone_last_refused(user.id, "+18765550123")
    reloaded = User(**records[user.id])
    assert reloaded.phone_last_refused == "+18765550123"
    assert reloaded.pending_phone == "+14165550123"
    assert pv.confirm(reloaded, code).verified
    assert store.set_user_phone_last_refused("missing-round3", "+18765550123") is None


@pytest.mark.parametrize("action", ["confirm", "clear", "cancel_pending"])
def test_round4_terminal_phone_action_clears_refusal(action) -> None:
    user = _user()
    code = pv.begin(user, "+14165550123")
    user.phone_last_refused = "+18765550123"
    if action == "confirm":
        assert pv.confirm(user, code).verified
    else:
        getattr(pv, action)(user)
    assert user.phone_last_refused is None
    assert user.pending_phone is None
    assert not pv.console_phone_requires_identity(user)
