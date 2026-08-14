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


def spoken_text(body: str) -> str:
    """What a voice call actually says.

    Repeated once because a ringing phone is answered mid-sentence, and XML
    metacharacters are stripped rather than escaped — they would break the
    inline TwiML document, and a page that fails to parse is a page not
    delivered.
    """
    cleaned = body[:400].replace("&", " and ").replace("<", " ").replace(">", " ")
    return f"Trusted Router notification. {cleaned}. Again. {cleaned}."


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
        if not settings.telnyx_texml_account_id or not settings.api_base_url:
            return 0, "telnyx voice needs TR_TELNYX_TEXML_ACCOUNT_ID and an api base url"
        url = (
            settings.api_base_url.rstrip("/")
            + "/notify/texml?text="
            + urllib.parse.quote(body[:300])
        )
        return _post_form(
            f"https://api.telnyx.com/v2/texml/Accounts/{settings.telnyx_texml_account_id}/Calls",
            {"From": settings.telnyx_from_number or "", "To": to, "Url": url},
            {"Authorization": f"Bearer {settings.telnyx_api_key}"},
        )

    # -- delivery -----------------------------------------------------------

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

        if preferred_carrier:
            wanted = preferred_carrier.strip().lower()
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
                return TelephonyResult(True, name, detail, tuple(attempts))
            attempts.append(f"{name}={status} {detail[:120]}")

        log.error("telephony %s undelivered: %s", channel, attempts)
        return TelephonyResult(False, None, "; ".join(attempts) or "no carrier configured",
                               tuple(attempts))


def get_telephony_service(settings: Settings) -> TelephonyService:
    return TelephonyService(settings)
