"""Phone ownership proof, as pure functions over a User record.

Kept backend-agnostic on purpose: every store persists `User` as a whole
record, so each backend only has to read-modify-write and the rules live in
exactly one place instead of being reimplemented three times and drifting.

WHY A PHONE AT ALL
------------------
Notifications go only to the account owner, so they cannot be aimed at a
stranger. What a verified phone actually buys is a cost floor on ACCOUNT
FARMING: TrustedRouter sends every customer's notifications from its own A2P
10DLC brand, which makes sender reputation a shared asset, and email — the
cheapest channel to abuse — is the one an unverified account would reach for.
So the phone gates every channel, including email.

A six-digit code is worth about 20 bits, which is nothing without limits, so
this file is mostly limits: a short expiry, a hard attempt cap that burns the
code rather than the clock, and a resend floor. The code is stored hashed with
the same primitives as an API key, because a leaked database should not hand
over the ability to verify someone else's phone.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from dataclasses import dataclass
from typing import Literal

from trusted_router.security import hash_api_key, new_hash_salt, verify_api_key
from trusted_router.storage_models import User, iso_now, utcnow

CODE_TTL_SECONDS = 600  # ten minutes: long enough for a slow SMS, short enough to matter
MAX_ATTEMPTS = 5
RESEND_FLOOR_SECONDS = 60

# Country calling codes whose numbers may complete phone verification before
# identity verification. This is the US/Canada NANP plus selected European regions.
# SMS-fraud policy, not a continent classifier: +375 (Belarus) and +7
# (Russia/Kazakhstan) require identity. Transcontinental/ambiguous +90 (Turkey),
# +995 (Georgia), +374 (Armenia), and +994 (Azerbaijan) also remain excluded.
DIRECT_PHONE_VERIFICATION_CALLING_CODES = frozenset(
    {
        "1",
        "298",  # Faroe Islands
        "299",  # Greenland
        "30",
        "31",
        "32",
        "33",
        "34",
        "350",
        "351",
        "352",
        "353",
        "354",
        "355",
        "356",
        "357",
        "358",
        "359",
        "36",
        "370",
        "371",
        "372",
        "373",
        "376",
        "377",
        "378",
        "380",
        "381",
        "382",
        "383",
        "385",
        "386",
        "387",
        "389",
        "39",
        "40",
        "41",
        "420",
        "421",
        "423",
        "43",
        "44",
        "45",
        "46",
        "47",
        "48",
        "49",
    }
)

# Caribbean/Atlantic and US Pacific territory NANP codes document why +1 alone cannot imply US/Canada.
# This is documentation, NOT the gate: the explicit allowlist below fails closed.
# 340/787/939 are US territories and are allowed, as are 670/671/684.
CARIBBEAN_NANP_AREA_CODES = frozenset(
    {
        "242",  # Bahamas
        "246",  # Barbados
        "264",  # Anguilla
        "268",  # Antigua and Barbuda
        "284",  # British Virgin Islands
        "340",  # US Virgin Islands
        "345",  # Cayman Islands
        "441",  # Bermuda
        "473",  # Grenada
        "649",  # Turks and Caicos Islands
        "658",  # Jamaica overlay
        "664",  # Montserrat
        "670",  # Northern Mariana Islands (US Pacific territory)
        "671",  # Guam (US Pacific territory)
        "684",  # American Samoa (US Pacific territory)
        "721",  # Sint Maarten
        "758",  # Saint Lucia
        "767",  # Dominica
        "784",  # Saint Vincent and the Grenadines
        "787",  # Puerto Rico
        "809",  # Dominican Republic
        "829",  # Dominican Republic overlay
        "849",  # Dominican Republic overlay
        "868",  # Trinidad and Tobago
        "869",  # Saint Kitts and Nevis
        "876",  # Jamaica
        "939",  # Puerto Rico overlay
    }
)

# In-service geographic US/Canada NPAs from NANPA's 2026-09-05 snapshot:
# https://reports.nanpa.com/public/npa_report.csv (USE=G, IN_SERVICE=Y).
# Includes US territories: USVI (340), Northern Mariana Islands (670), Guam
# (671), American Samoa (684), and Puerto Rico (787/939). Unknown, future, and
# non-geographic NPAs require identity until explicitly reviewed and added.
# Reviewed addition: 273, Quebec overlay entering service 2027-02-27.
DIRECT_PHONE_VERIFICATION_NANP_AREA_CODES = frozenset(
    "201 202 203 204 205 206 207 208 209 210 212 213 214 215 216 217 218 219 220 223 224 225 "
    "226 227 228 229 231 234 235 236 239 240 248 249 250 251 252 253 254 256 257 260 262 263 "
    "267 269 270 272 273 274 276 279 281 283 289 301 302 303 304 305 306 307 308 309 310 312 313 "
    "314 315 316 317 318 319 320 321 323 324 325 326 327 329 330 331 332 334 336 337 339 340 "
    "341 343 346 347 350 351 352 353 354 357 360 361 363 364 365 367 368 369 380 382 385 386 "
    "401 402 403 404 405 406 407 408 409 410 412 413 414 415 416 417 418 419 423 424 425 428 "
    "430 431 432 434 435 436 437 438 440 442 443 445 447 448 450 457 458 463 464 465 468 469 "
    "470 471 472 474 475 478 479 480 483 484 501 502 503 504 505 506 507 508 509 510 512 513 "
    "514 515 516 517 518 519 520 530 531 534 539 540 541 548 551 557 559 561 562 563 564 567 "
    "570 571 572 573 574 575 579 580 581 582 584 585 586 587 601 602 603 604 605 606 607 608 "
    "609 610 612 613 614 615 616 617 618 619 620 621 623 624 626 628 629 630 631 636 639 640 "
    "641 645 646 647 650 651 656 657 659 660 661 662 667 669 670 671 672 678 679 680 681 682 "
    "683 684 686 689 701 702 703 704 705 706 707 708 709 712 713 714 715 716 717 718 719 720 "
    "724 725 726 727 728 729 730 731 732 734 737 738 740 742 743 747 748 753 754 757 760 762 "
    "763 765 769 770 771 772 773 774 775 778 779 780 781 782 785 786 787 801 802 803 804 805 "
    "806 807 808 810 812 813 814 815 816 817 818 819 820 821 825 826 828 830 831 832 835 837 "
    "838 839 840 843 845 847 848 850 854 856 857 858 859 860 861 862 863 864 865 867 870 872 "
    "873 878 879 901 902 903 904 905 906 907 908 909 910 912 913 914 915 916 917 918 919 920 "
    "924 925 928 929 930 931 934 936 937 938 939 940 941 942 943 945 947 948 949 951 952 954 "
    "956 959 970 971 972 973 975 978 979 980 983 984 985 986 989 ".split()
)

ConfirmStatus = Literal[
    "ok", "no_pending", "expired", "too_many_attempts", "mismatch", "identity_required"
]


class PhoneNumberError(ValueError):
    """The number is not something a carrier will accept."""


class PhoneIdentityVerificationRequired(PermissionError):
    """The number's region requires identity verification before phone proof."""


@dataclass(frozen=True)
class ConfirmResult:
    status: ConfirmStatus
    attempts_remaining: int = 0

    @property
    def verified(self) -> bool:
        return self.status == "ok"


def normalize_phone(raw: str) -> str:
    """To E.164, or raise.

    Carriers reject anything else with a vague 400 rather than "your number has
    dots in it", so the shape is enforced here where the error can be useful.
    """
    if not raw or not raw.strip():
        raise PhoneNumberError("phone number is required")
    cleaned = re.sub(r"[\s().-]", "", raw.strip())
    if not cleaned.startswith("+"):
        raise PhoneNumberError("phone number must be E.164 and start with '+', e.g. +15551234567")
    digits = cleaned[1:]
    if not re.fullmatch(r"[0-9]{8,15}", digits):
        raise PhoneNumberError("phone number must contain 8 to 15 ASCII digits after '+'")
    if not 8 <= len(digits) <= 15:  # E.164 allows at most 15 digits
        raise PhoneNumberError("phone number must have between 8 and 15 digits")
    return "+" + digits


def phone_verification_requires_identity_first(phone: str) -> bool:
    """Whether an already-normalized E.164 number is outside the direct regions."""
    if phone.startswith("+1"):
        return len(phone) != 12 or phone[2:5] not in DIRECT_PHONE_VERIFICATION_NANP_AREA_CODES
    return not any(
        phone.startswith(f"+{calling_code}")
        for calling_code in DIRECT_PHONE_VERIFICATION_CALLING_CODES
        if calling_code != "1"
    )


def console_phone_requires_identity(user: User) -> bool:
    """Stored proof state wins over earlier refused destinations and URL errors."""
    if user.identity_verified or user.phone_verified:
        return False
    number = user.pending_phone or user.phone or user.phone_last_refused
    return bool(number and phone_verification_requires_identity_first(number))


def new_code() -> str:
    """Six digits, uniformly random, leading zeros preserved."""
    return f"{secrets.randbelow(1_000_000):06d}"


def can_resend(user: User, *, now: dt.datetime | None = None) -> tuple[bool, int]:
    """(allowed, seconds_to_wait). A resend floor keeps a retry loop — or
    someone else's phone — from being texted continuously."""
    now = now or utcnow()
    sent_at = getattr(user, "phone_code_sent_at", None)
    if not sent_at:
        return True, 0
    try:
        previous = dt.datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        return True, 0
    elapsed = (now - previous).total_seconds()
    if elapsed >= RESEND_FLOOR_SECONDS:
        return True, 0
    return False, int(RESEND_FLOOR_SECONDS - elapsed)


def begin(
    user: User,
    phone: str,
    *,
    channel: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Stage `phone` against the user and return the code to send.

    The number is held in `pending_phone` rather than `phone`: an unconfirmed
    number must never be reachable, or "start verification" would itself be a
    way to send someone a message.
    """
    now = now or utcnow()
    normalized = normalize_phone(phone)
    if (
        phone_verification_requires_identity_first(normalized)
        and not user.identity_verified
    ):
        raise PhoneIdentityVerificationRequired(
            "Complete identity verification before verifying a phone number from this region"
        )
    code = new_code()
    salt = new_hash_salt()

    user.phone_last_refused = None
    user.pending_phone = normalized
    user.phone_code_salt = salt
    user.phone_code_hash = hash_api_key(code, salt)
    user.phone_code_expires_at = (
        (now + dt.timedelta(seconds=CODE_TTL_SECONDS)).isoformat().replace("+00:00", "Z")
    )
    user.phone_code_attempts = 0
    user.phone_code_sent_at = now.isoformat().replace("+00:00", "Z")
    user.phone_code_channel = channel if channel in {"sms", "voice"} else None
    return code


def confirm(user: User, code: str, *, now: dt.datetime | None = None) -> ConfirmResult:
    """Check a code and, on success, promote the pending number."""
    now = now or utcnow()

    if not user.pending_phone or not user.phone_code_hash or not user.phone_code_salt:
        return ConfirmResult("no_pending")

    # Revalidate legacy pending records as well as newly submitted numbers.
    normalize_phone(user.pending_phone)

    if (
        phone_verification_requires_identity_first(user.pending_phone)
        and not user.identity_verified
    ):
        return ConfirmResult("identity_required")

    expires_at = user.phone_code_expires_at
    if expires_at:
        try:
            deadline = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            deadline = now
        if now >= deadline:
            _clear_pending(user)
            return ConfirmResult("expired")

    if (user.phone_code_attempts or 0) >= MAX_ATTEMPTS:
        _clear_pending(user)
        return ConfirmResult("too_many_attempts")

    if not verify_api_key((code or "").strip(), user.phone_code_salt, user.phone_code_hash):
        user.phone_code_attempts = (user.phone_code_attempts or 0) + 1
        remaining = MAX_ATTEMPTS - user.phone_code_attempts
        if remaining <= 0:
            # Burn the code, not just the clock: 20 bits of entropy survives
            # unlimited guessing, so exhausting the attempts must invalidate it.
            _clear_pending(user)
            return ConfirmResult("too_many_attempts")
        return ConfirmResult("mismatch", attempts_remaining=remaining)

    user.phone_last_refused = None
    user.phone = user.pending_phone
    user.phone_verified = True
    user.phone_verified_at = iso_now()
    _clear_pending(user)
    return ConfirmResult("ok")


def clear(user: User) -> None:
    """Forget the phone entirely. Used when a user removes it."""
    user.phone_last_refused = None
    user.phone = None
    user.phone_verified = False
    user.phone_verified_at = None
    _clear_pending(user)


def cancel_pending(user: User) -> None:
    """Discard an in-flight proof without changing a verified number.

    Deliberately keeps `phone_code_sent_at`: the resend floor is a floor on
    how often this account may make a phone ring, not on how long a code
    stays pending. If cancelling reset it, "cancel, then start again" would
    be a way to ring any number continuously — the exact loop the floor
    exists to stop.
    """
    user.phone_last_refused = None
    _clear_pending(user)


def _clear_pending(user: User) -> None:
    user.pending_phone = None
    user.phone_code_hash = None
    user.phone_code_salt = None
    user.phone_code_expires_at = None
    user.phone_code_attempts = 0
    user.phone_code_channel = None
