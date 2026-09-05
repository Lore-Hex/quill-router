"""POST /v1/notify — an agent reaches its own human.

The destination is not a parameter. It is resolved api_key -> workspace ->
owner, and only to contacts that owner has proved they control, which is what
makes this safe on an ordinary inference key: it cannot be aimed at a stranger,
so it is a self-notification primitive rather than a messaging API.

Charging follows the same rule the service does: reserve before sending, settle
only what was delivered, refund everything otherwise. A carrier outage must not
bill the customer — they asked for their human to be reached and their human was
not reached — and it must not make an outage look like revenue.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from trusted_router import phone_verification as pv
from trusted_router.auth import (
    AuthenticatedPrincipal,
    InferencePrincipal,
    Principal,
    SettingsDep,
)
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.phone_verification import CODE_TTL_SECONDS
from trusted_router.services.notify import (
    CHANNELS,
    NotifyOutcome,
    get_notify_service,
    price_for,
    send_verification_code,
)
from trusted_router.services.telephony import branded, spoken_text
from trusted_router.spend_windows import (
    KeyLimitExceeded,
    KeyWindowLimitExceeded,
    remember_spend_window_decision,
    spend_window_headers,
    spend_window_limit_error_message,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import User
from trusted_router.types import ErrorType, UsageType
from trusted_router.verification_gates import missing_phone_verification_requirements

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 1000
MAX_SUBJECT_CHARS = 120


def register_notify_public_routes(router: APIRouter) -> None:
    @router.api_route("/notify/texml", methods=["GET", "POST"])
    async def notify_texml(
        text: str = Query(default="", max_length=MAX_BODY_CHARS),
    ) -> Response:
        """Call instructions, fetched by the carrier when a call connects.

        Public and unauthenticated by necessity: a carrier fetches this from its
        own infrastructure holding no credential of ours. It is safe because it
        is a bounded pure function of the query string — it reads no state and
        reveals nothing — and the most an attacker gets is their own sentence
        read back. Keeping it on the public surface prevents carrier traffic or
        an anonymous flood from consuming the logged-in control pool.
        """
        spoken = spoken_text(branded(text))
        # XML metacharacters are already stripped by spoken_text; a document
        # that fails to parse is a call that connects and says nothing, which is
        # indistinguishable from a page that never arrived.
        document = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Say voice="alice">{spoken}</Say></Response>'
        )
        return Response(content=document, media_type="application/xml")


def register_notify_routes(router: APIRouter) -> None:
    @router.post("/notify")
    async def send_notification(
        request: Request,
        payload: dict[str, Any],
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        # This is a paid owner-notification channel. No v1 delegated scope
        # grants it; `inference` covers model execution and client telemetry.
        if principal.scopes:
            raise api_error(
                403,
                "No delegated scope grants owner notifications",
                ErrorType.INSUFFICIENT_SCOPE,
            )
        channel = str(payload.get("channel") or "push").strip().lower()
        if channel not in CHANNELS:
            raise api_error(
                400,
                f"channel must be one of {list(CHANNELS)}",
                ErrorType.BAD_REQUEST,
            )

        body = str(payload.get("body") or payload.get("message") or "")[:MAX_BODY_CHARS]
        subject = str(payload.get("subject") or "")[:MAX_SUBJECT_CHARS]
        preferred_carrier = payload.get("carrier")

        owner = _owner_of(principal)
        price = price_for(channel, settings)

        # Reserved BEFORE the send so a workspace cannot be pushed past its
        # limit by notifications in flight, and refunded in full when nothing
        # was delivered.
        ticket = _Charge.reserve(principal, price, request=request)
        try:
            outcome = get_notify_service(settings).send(
                owner=owner,
                channel=channel,
                subject=subject,
                body=body,
                preferred_carrier=str(preferred_carrier) if preferred_carrier else None,
                push_sender=_push_sender(settings),
            )
        except Exception:
            ticket.refund()
            raise

        charged = outcome.price_microdollars if (outcome.billable and ticket.billable) else 0
        ticket.settle(charged)

        body_out = _body(outcome)
        body_out["charged_microdollars"] = charged
        if not outcome.delivered:
            return JSONResponse(body_out, status_code=_status_for(outcome))
        return JSONResponse(body_out, status_code=200)

    @router.post("/notify/phone/start")
    async def start_phone_verification(
        payload: dict[str, Any],
        principal: AuthenticatedPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        """Send a code to a number the user claims. Session only.

        Not available to an api key: adding a phone changes who the account can
        page, so it belongs to whoever can log in, not to any key they minted.
        """
        user = principal.user
        if user is None:
            raise api_error(403, "sign in to manage your phone number", ErrorType.FORBIDDEN)

        missing_requirements = missing_phone_verification_requirements(user, settings)
        if missing_requirements:
            raise api_error(
                403,
                "Phone verification requirements are not met",
                ErrorType.VERIFICATION_REQUIRED,
                extra={
                    "missing_requirements": missing_requirements,
                    "verification_url": "/console/settings",
                },
            )

        requested = str(payload.get("channel") or "sms").strip().lower()
        if requested not in {"sms", "voice"}:
            raise api_error(400, "channel must be sms or voice", ErrorType.BAD_REQUEST)
        channel: Literal["sms", "voice"] = (
            "sms" if requested == "sms" and settings.notify_sms_available else "voice"
        )

        allowed, wait = pv.can_resend(user)
        if not allowed:
            # A floor here is what stops "start verification" from being a way
            # to ring someone else's phone repeatedly.
            raise api_error(
                429,
                f"a code was just sent; try again in {wait} seconds",
                ErrorType.RATE_LIMITED,
                headers={"retry-after": str(wait)},
            )

        try:
            phone = pv.normalize_phone(str(payload.get("phone") or ""))
        except pv.PhoneNumberError as exc:
            raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc

        started = STORE.begin_phone_verification(user.id, phone, channel)
        if started is None:
            raise api_error(404, "user not found", ErrorType.NOT_FOUND)
        code, _updated = started

        delivered, detail = send_verification_code(settings, phone, code, channel=channel)
        if not delivered:
            # Leave the pending code in place: the user may retry on the other
            # channel, and clearing it would make a carrier blip look like a
            # rejected number.
            return JSONResponse(
                {"sent": False, "channel": channel, "detail": detail}, status_code=502
            )
        return JSONResponse({"sent": True, "channel": channel, "expires_in": CODE_TTL_SECONDS})

    @router.post("/notify/phone/confirm")
    async def confirm_phone_verification(
        payload: dict[str, Any],
        principal: AuthenticatedPrincipal,
    ) -> JSONResponse:
        user = principal.user
        if user is None:
            raise api_error(403, "sign in to manage your phone number", ErrorType.FORBIDDEN)

        status, updated = STORE.confirm_phone_verification(user.id, str(payload.get("code") or ""))
        if status == "ok":
            return JSONResponse({"verified": True, "phone": updated.phone if updated else None})

        # 400 for a wrong code, 409 for a state problem the user must restart.
        code = 400 if status == "mismatch" else 409
        return JSONResponse({"verified": False, "status": status}, status_code=code)

def _owner_of(principal: Principal) -> User | None:
    """api_key -> workspace -> owner. The one place a destination is chosen."""
    workspace = principal.workspace
    owner_id = getattr(workspace, "owner_user_id", None)
    if not owner_id:
        return None
    return STORE.get_user(owner_id)


def _status_for(outcome: NotifyOutcome) -> int:
    # A refused send is the caller's problem to fix (verify a phone, install the
    # app); a failed send is ours. Distinguishing them keeps a retry loop from
    # hammering a request that can never succeed.
    if outcome.refusal in {"unknown_channel", "empty_body"}:
        return 400
    if outcome.refusal is not None:
        return 409
    return 502


def _body(outcome: NotifyOutcome) -> dict[str, Any]:
    data: dict[str, Any] = {
        "delivered": outcome.delivered,
        "channel": outcome.channel,
        "detail": outcome.detail,
        # Reported in microdollars and only when actually charged, so a caller
        # can reconcile against its own spend without inferring pricing.
        "charged_microdollars": outcome.price_microdollars if outcome.billable else 0,
    }
    if outcome.carrier:
        data["carrier"] = outcome.carrier
    if outcome.refusal:
        data["refusal"] = outcome.refusal
    return data


def _push_sender(settings: Settings) -> Any:
    """Push delivery, if this deployment has it.

    Injected rather than imported by the service so notify stays free of
    device-token plumbing; None simply reports push unavailable.
    """
    return None


class _Charge:
    """Reserve-then-settle around a single notification.

    Mirrors what inference does with a QuotaTicket, without borrowing that path:
    reserved_quota() is built around a Model, and a notification has none.
    """

    def __init__(
        self,
        key_hash: str | None,
        reservation_id: str | None,
        amount: int,
        *,
        key_reserved_microdollars: int = 0,
        billable: bool = True,
    ) -> None:
        self._key_hash = key_hash
        self._reservation_id = reservation_id
        self._amount = amount
        self._key_reserved_microdollars = key_reserved_microdollars
        self._finalized = False
        # False when no credit hold could be taken. The send still happens; the
        # response must then report zero rather than a price nobody charged.
        self.billable = billable

    @classmethod
    def reserve(
        cls,
        principal: Principal,
        amount: int,
        *,
        request: Request | None = None,
    ) -> _Charge:
        if amount <= 0 or principal.api_key is None:
            # Push is free, so there is nothing to hold and nothing to release.
            return cls(None, None, 0)

        key_hash = principal.api_key.hash
        try:
            key_limit_reservation = STORE.reserve_key_limit(
                key_hash,
                amount,
                usage_type=UsageType.CREDITS,
            )
            window_decision = key_limit_reservation.window_decision
            remember_spend_window_decision(request, window_decision)
        except KeyWindowLimitExceeded as exc:
            remember_spend_window_decision(request, exc.decision)
            raise api_error(
                429,
                spend_window_limit_error_message(exc.decision),
                ErrorType.KEY_WINDOW_LIMIT_EXCEEDED,
                headers=spend_window_headers(exc.decision, retry_after=True),
            ) from exc
        except KeyLimitExceeded as exc:
            remember_spend_window_decision(request, exc.decision)
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc
        except ValueError as exc:
            raise api_error(
                402, "API key spend limit exceeded", ErrorType.KEY_LIMIT_EXCEEDED
            ) from exc

        # Production runs the TYPED billing backend, where STORE.reserve() is a
        # removed legacy path that raises RuntimeError. The suite runs against
        # InMemoryStore, which still implements the old surface, so 3757 green
        # tests could not reach this line. The first real request 500'd on it
        # and Sentry found it before any test did.
        #
        # Charging correctly means authorize_gateway_typed, which is shaped
        # entirely around inference — model id, provider, candidate endpoints —
        # and would file notifications into the same counters and analytics as
        # model traffic. That deserves its own change rather than a
        # plausible-looking argument list invented at the call site. So the
        # typed backend takes the key-limit hold only and reports zero.
        #
        # Delivering free beats refusing to deliver, and zero is the honest
        # number: the caller is told what happened instead of being billed by a
        # path nobody has verified.
        try:
            reservation = STORE.reserve(principal.workspace.id, key_hash, amount)
        except ValueError as exc:
            STORE.refund_key_limit(
                key_hash,
                key_limit_reservation.reserved_microdollars,
                usage_type=UsageType.CREDITS,
            )
            raise api_error(402, "Insufficient credits", ErrorType.INSUFFICIENT_CREDITS) from exc
        except RuntimeError:
            log.warning(
                "notify: credit reservation unavailable on this backend; "
                "delivering unbilled (workspace=%s)", principal.workspace.id,
            )
            return cls(
                key_hash,
                None,
                amount,
                key_reserved_microdollars=key_limit_reservation.reserved_microdollars,
                billable=False,
            )

        return cls(
            key_hash,
            reservation.id,
            amount,
            key_reserved_microdollars=key_limit_reservation.reserved_microdollars,
        )

    def settle(self, actual: int) -> None:
        if self._finalized or self._key_hash is None:
            return
        self._finalized = True
        if self._reservation_id is not None:
            STORE.settle(self._reservation_id, actual)
        STORE.settle_key_limit(
            self._key_hash,
            self._key_reserved_microdollars,
            actual,
            usage_type=UsageType.CREDITS,
        )

    def refund(self) -> None:
        if self._finalized or self._key_hash is None:
            return
        self._finalized = True
        if self._reservation_id is not None:
            STORE.refund(self._reservation_id)
        STORE.refund_key_limit(
            self._key_hash,
            self._key_reserved_microdollars,
            usage_type=UsageType.CREDITS,
        )
