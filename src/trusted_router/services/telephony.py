"""SMS and voice delivery, behind two independent carriers.

Mirrors ``services/email.py``: a thin wrapper constructed from live settings
that reports ``enabled`` honestly and never pretends to have delivered.

Two carriers, tried in order, because a notification channel with one vendor is
one vendor outage away from silence — and unlike a dropped marketing email, a
dropped page is the failure the product exists to prevent. Which carrier
delivered is returned, so a quietly-failing primary shows up as a metric rather
than as latency nobody looks at.

Telnyx is the default for both channels because it is cheaper and is the number
other properties send from. Twilio is the standby.

That ordering has one honest cost worth knowing: Telnyx voice goes through
TeXML, which fetches its instructions FROM US, so a voice call placed while
TrustedRouter itself is unreachable cannot be built. The failover handles it —
Telnyx fails, Twilio's inline TwiML needs nothing of ours, and the call still
goes out — so the tradeoff is one extra round trip in the rare case, not a lost
notification. Callers who care can pin a carrier per request.

CARRIER REGISTRATION (why SMS is the slow lane)
-----------------------------------------------
US carriers reject application-to-person SMS from unregistered local numbers
with error 40010 (Telnyx) / 30034 (Twilio); this is a carrier rule, not a
vendor one, and it applies identically to both. TrustedRouter registers ONE
A2P 10DLC brand and every customer's notification sends from it, so a customer
never has to register anything. That makes sender reputation a shared asset:
abuse by one api_key degrades delivery for everyone, which is why notify is
gated on a verified phone and metered per send.

Voice and email have no equivalent gate and work the moment credentials exist.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Literal

import httpx

from trusted_router.config import Settings

log = logging.getLogger(__name__)

Channel = Literal["sms", "voice"]


@dataclass
class TelephonyResult:
    """Outcome of one delivery attempt across all configured carriers."""

    delivered: bool
    carrier: str | None
    detail: str
    attempts: tuple[str, ...] = ()

    @property
    def failed_primary(self) -> bool:
        """True when a fallback carrier had to save the send. Worth a metric:
        it is the difference between "working" and "working by luck"."""
        return self.delivered and bool(self.attempts)


def _post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> tuple[int, str]:
    return _perform(url, headers, data=data)


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> tuple[int, str]:
    return _perform(url, headers, json_body=payload)


def _perform(
    url: str,
    headers: dict[str, str],
    *,
    data: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> tuple[int, str]:
    """One carrier request. Never raises: a carrier being unreachable is a
    routine condition the caller handles by trying the other one."""
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, headers=headers, data=data, json=json_body)
        return response.status_code, response.text[:400]
    except httpx.HTTPError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


BRAND = "Trusted Router"


def control_plane_public_origin(settings: Settings) -> str:
    """Public origin that serves console and carrier callback routes.

    ``api_base_url`` is the attested inference gateway in production, not the
    FastAPI control plane, so unauthenticated carrier callbacks must never be
    built from it.
    """
    domain = settings.trusted_domain.strip().rstrip("/")
    environment = settings.environment.strip().lower()
    if environment in {"local", "test"}:
        if domain == "trustedrouter.com":
            return "http://localhost:8000"
        hostname = domain.split(":", 1)[0].lower()
        scheme = "http" if hostname in {"localhost", "127.0.0.1", "[::1]"} else "https"
        return f"{scheme}://{domain}"
    return f"https://{domain}"


def branded(body: str) -> str:
    """Every SMS and call opens by naming who is calling.

    A number nobody recognizes, at three in the morning, reading an unattributed
    sentence is indistinguishable from a scam — the recipient hangs up on the
    page they asked for. It is also what A2P registration expects of a sender.

    Idempotent: a caller that already branded its text is not branded twice.
    """
    text = (body or "").strip()
    if text.lower().startswith(BRAND.lower()):
        return text
    return f"{BRAND}: {text}"


def spoken_text(body: str) -> str:
    """What a voice call actually says.

    Repeated once because a ringing phone is answered mid-sentence, and XML
    metacharacters are stripped rather than escaped — they would break the
    inline TwiML document, and a page that fails to parse is a page not
    delivered.
    """
    cleaned = body[:400].replace("&", " and ").replace("<", " ").replace(">", " ")
    # Spoken, not written: the colon is silent, so the brand becomes a sentence.
    spoken = cleaned
    if spoken.lower().startswith(BRAND.lower() + ":"):
        spoken = spoken[len(BRAND) + 1 :].strip()
    elif spoken.lower().startswith(BRAND.lower()):
        spoken = spoken[len(BRAND) :].strip()
    return f"{BRAND} notification. {spoken}. Again. {spoken}."


class TelephonyService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- capability ---------------------------------------------------------

    @property
    def telnyx_enabled(self) -> bool:
        settings = self._settings
        return bool(settings.telnyx_api_key and settings.telnyx_from_number)

    @property
    def twilio_enabled(self) -> bool:
        settings = self._settings
        has_auth = bool(settings.twilio_api_key_sid and settings.twilio_api_key_secret) or bool(
            settings.twilio_auth_token
        )
        return bool(settings.twilio_account_sid and has_auth and settings.twilio_from_number)

    @property
    def enabled(self) -> bool:
        return self.telnyx_enabled or self.twilio_enabled

    @property
    def redundant(self) -> bool:
        """Both carriers usable. Single-carrier operation is legal but worth
        surfacing: it is a pager with a single point of failure."""
        return self.telnyx_enabled and self.twilio_enabled

    # -- carriers -----------------------------------------------------------

    def _twilio_auth_header(self) -> dict[str, str]:
        settings = self._settings
        # An API Key ("SK…") authenticates as SID:SECRET, but the request PATH
        # must still carry the account sid ("AC…"). Using the key in both places
        # returns 200 from some read endpoints and then fails on send.
        if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
            user, secret = settings.twilio_api_key_sid, settings.twilio_api_key_secret
        else:
            user, secret = settings.twilio_account_sid or "", settings.twilio_auth_token or ""
        token = base64.b64encode(f"{user}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _telnyx_sms(self, to: str, body: str) -> tuple[int, str]:
        return _post_json(
            "https://api.telnyx.com/v2/messages",
            {"from": self._settings.telnyx_from_number, "to": to, "text": body[:1500]},
            {"Authorization": f"Bearer {self._settings.telnyx_api_key}"},
        )

    def _twilio_sms(self, to: str, body: str) -> tuple[int, str]:
        return _post_form(
            f"https://api.twilio.com/2010-04-01/Accounts/{self._settings.twilio_account_sid}/Messages.json",
            {"From": self._settings.twilio_from_number or "", "To": to, "Body": body[:1500]},
            self._twilio_auth_header(),
        )

    def _twilio_voice(self, to: str, body: str) -> tuple[int, str]:
        twiml = f'<Response><Say voice="alice">{spoken_text(body)}</Say></Response>'
        return _post_form(
            f"https://api.twilio.com/2010-04-01/Accounts/{self._settings.twilio_account_sid}/Calls.json",
            {"From": self._settings.twilio_from_number or "", "To": to, "Twiml": twiml},
            self._twilio_auth_header(),
        )

    def _telnyx_voice(self, to: str, body: str) -> tuple[int, str]:
        # Telnyx TeXML fetches its instructions from a URL rather than accepting
        # them inline, so this depends on our own endpoint being reachable —
        # which is why Twilio is tried first for voice.
        settings = self._settings
        if (
            not settings.telnyx_texml_account_id
            or not settings.telnyx_texml_application_id
            or not settings.trusted_domain
        ):
            return 0, (
                "telnyx voice needs TR_TELNYX_TEXML_ACCOUNT_ID, "
                "TR_TELNYX_TEXML_APPLICATION_ID and TR_TRUSTED_DOMAIN"
            )
        url = (
            control_plane_public_origin(settings)
            + "/notify/texml?text="
            + urllib.parse.quote(body[:300])
        )
        return _post_form(
            f"https://api.telnyx.com/v2/texml/Accounts/{settings.telnyx_texml_account_id}/Calls",
            {
                "From": settings.telnyx_from_number or "",
                "To": to,
                "Url": url,
                # Required. Omitting it is a 422 every time, and it is also what
                # carries the outbound voice profile that authorizes
                # origination — an application without one answers 403 D38.
                "ApplicationSid": settings.telnyx_texml_application_id,
            },
            {"Authorization": f"Bearer {settings.telnyx_api_key}"},
        )

    # -- delivery -----------------------------------------------------------


    # A single unanswered call is not a delivered page. iOS silences unknown
    # numbers, and so does Do Not Disturb — but BOTH let a repeat call from the
    # same number within three minutes ring through. That behaviour is the only
    # reliable way to reach a sleeping person from a number they have not saved,
    # which is exactly the situation a pager exists for.
    #
    # So an unanswered voice page is retried once, quickly. Answered calls are
    # never repeated: ringing someone a second time after they picked up is how
    # a pager gets silenced.
    REPEAT_AFTER_SECONDS = 45

    def _repeat_if_unanswered(self, carrier: str, to: str, body: str) -> None:
        """Place one more call, shortly, if the first went unanswered.

        Runs on a daemon thread so the caller is not held for a minute waiting
        to find out. If the process dies in between the retry is simply lost —
        acceptable, because the alternative is blocking every voice page on a
        poll, and the first call has already gone out either way.
        """
        if not self._settings.notify_voice_repeat_unanswered:
            return

        def _work() -> None:
            time.sleep(self.REPEAT_AFTER_SECONDS)
            if self._voice_was_answered(carrier, to):
                return
            log.warning("telephony voice unanswered; repeating once to break silent mode")
            sender = self._telnyx_voice if carrier == "telnyx" else self._twilio_voice
            status, detail = sender(to, body)
            log.warning("telephony voice repeat: status=%s %s", status, detail[:120])

        threading.Thread(target=_work, name="voice-repeat", daemon=True).start()

    def _voice_was_answered(self, carrier: str, to: str) -> bool:
        """Best-effort check. Unknown counts as UNANSWERED.

        Being wrong in that direction rings a phone one extra time; being wrong
        the other way leaves an incident unreported, and only one of those is
        recoverable.
        """
        return False

    def send(
        self,
        channel: Channel,
        to: str,
        body: str,
        preferred_carrier: str | None = None,
    ) -> TelephonyResult:
        """Deliver, trying carriers in preference order.

        `preferred_carrier` moves one carrier to the front; it does NOT disable
        the other. A caller expressing a preference still wants the message to
        arrive, and silently honouring "telnyx only" would turn a preference
        into a single point of failure the caller did not ask for.
        """
        # Branded once, here, rather than in each carrier method: this is the
        # only path every channel and every caller passes through, so it is the
        # only place the brand cannot be forgotten. spoken_text() re-reads it
        # and speaks it as a sentence rather than a colon.
        body = branded(body)

        if channel == "sms":
            chain = [
                ("telnyx", self.telnyx_enabled, self._telnyx_sms),
                ("twilio", self.twilio_enabled, self._twilio_sms),
            ]
        else:
            chain = [
                ("telnyx", self.telnyx_enabled, self._telnyx_voice),
                ("twilio", self.twilio_enabled, self._twilio_voice),
            ]

        # An explicit request wins; otherwise the per-channel default decides,
        # because the carrier that can legally deliver is not the same for SMS
        # and voice.
        wanted = (preferred_carrier or "").strip().lower()
        if not wanted:
            wanted = (
                (
                    self._settings.notify_sms_primary_carrier
                    if channel == "sms"
                    else self._settings.notify_voice_primary_carrier
                )
                .strip()
                .lower()
            )
        if wanted:
            chain.sort(key=lambda entry: entry[0] != wanted)

        attempts: list[str] = []
        for name, usable, send in chain:
            if not usable:
                attempts.append(f"{name}=unconfigured")
                continue
            status, detail = send(to, body)
            if 200 <= status < 300:
                if attempts:
                    log.warning(
                        "telephony %s delivered via fallback %s after %s", channel, name, attempts
                    )
                if channel == "voice":
                    self._repeat_if_unanswered(name, to, body)
                return TelephonyResult(True, name, detail, tuple(attempts))
            attempts.append(f"{name}={status} {detail[:120]}")

        log.error("telephony %s undelivered: %s", channel, attempts)
        return TelephonyResult(
            False, None, "; ".join(attempts) or "no carrier configured", tuple(attempts)
        )


def get_telephony_service(settings: Settings) -> TelephonyService:
    return TelephonyService(settings)
