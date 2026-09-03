from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from trusted_router.config import Settings
from trusted_router.routable_payouts import routable_amount, safe_routable_error_code

_DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_WEBHOOK_MAX_AGE_SECONDS = 5 * 60


@dataclass(frozen=True)
class RoutableAPIError(RuntimeError):
    code: str
    status_code: int | None = None
    request_id: str | None = None

    def __str__(self) -> str:
        status = "transport" if self.status_code is None else str(self.status_code)
        return f"Routable request failed ({status}, {self.code})"


class RoutableClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> None:
        if not settings.routable_credentials_configured:
            raise ValueError("Routable payouts are not configured")
        self._token = str(settings.routable_api_token)
        self._base_url = settings.routable_api_base_url.rstrip("/")
        self._team_member_id = str(settings.routable_team_member_id)
        self._withdraw_from_account_id = str(
            settings.routable_withdraw_from_account_id
        )
        self._transport = transport
        self._timeout = timeout

    async def find_company(self, external_id: str) -> dict[str, Any] | None:
        payload = await self._request(
            "GET",
            "/v1/companies",
            params={"external_id": external_id},
        )
        return _first_result(payload)

    async def retrieve_company(self, company_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/companies/{company_id}")

    async def create_company(
        self,
        *,
        external_id: str,
        recipient_type: str,
        country_code: str,
        first_name: str,
        last_name: str,
        email: str,
        business_name: str | None = None,
    ) -> dict[str, Any]:
        contact = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "allow_for_multiple_companies": False,
            "default_contact_for_company_management": "actionable",
            "default_contact_for_payable_and_receivable": "none",
        }
        body: dict[str, Any] = {
            "acting_team_member": self._team_member_id,
            "collect_tax_form": True,
            "contacts": [contact],
            "country_code": country_code,
            "external_id": external_id,
            "is_customer": False,
            "is_vendor": True,
            "type": recipient_type,
        }
        if recipient_type == "business":
            body["business_name"] = business_name
        else:
            body["display_name"] = f"{first_name} {last_name}".strip()
        return await self._request("POST", "/v1/companies", json_body=body)

    async def invite_company(
        self,
        company_id: str,
        *,
        confirmation_redirect_url: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/companies/{company_id}/invite",
            json_body={
                "acting_team_member": self._team_member_id,
                "confirmation_redirect_url": confirmation_redirect_url,
                "get_links": True,
                "send_invite_email": False,
            },
        )

    async def reinvite_company(
        self,
        company_id: str,
        *,
        confirmation_redirect_url: str,
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/v1/companies/{company_id}/invite",
            json_body={
                "acting_team_member": self._team_member_id,
                "confirmation_redirect_url": confirmation_redirect_url,
                "get_links": True,
                "request_payment_method": True,
                "request_tax_form": True,
                "send_invite_email": False,
            },
        )

    async def list_payment_methods(self, company_id: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/v1/companies/{company_id}/payment-methods",
            params={"archival_status": "not_archived", "is_valid": "true"},
        )
        return _results(payload)

    async def find_payable(self, external_id: str) -> dict[str, Any] | None:
        payload = await self._request(
            "GET",
            "/v1/payables",
            params={"external_id": external_id},
        )
        return _first_result(payload)

    async def retrieve_payable(self, payable_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/payables/{payable_id}")

    async def create_payable(
        self,
        *,
        amount_microdollars: int,
        external_id: str,
        company_id: str,
        payment_method_id: str,
        idempotency_key: str,
        send_on: str,
    ) -> dict[str, Any]:
        amount = routable_amount(amount_microdollars)
        return await self._request(
            "POST",
            "/v1/payables",
            headers={"Idempotency-Key": idempotency_key},
            json_body={
                "type": "ach",
                "acting_team_member": self._team_member_id,
                "amount": amount,
                "currency_code": "USD",
                "delivery_method": "ach_standard",
                "external_id": external_id,
                "line_items": [
                    {
                        "amount": amount,
                        "description": "TrustedRouter creator earnings",
                        "quantity": "1",
                        "style": "item",
                        "unit_price": amount,
                    }
                ],
                "pay_to_company": company_id,
                "pay_to_payment_method": payment_method_id,
                "reference": external_id,
                "send_on": send_on,
                "withdraw_from_account": self._withdraw_from_account_id,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    headers=request_headers,
                    json=json_body,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RoutableAPIError("transport_error") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RoutableAPIError(
                _response_error_code(response),
                status_code=response.status_code,
                request_id=response.headers.get("Request-ID"),
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RoutableAPIError(
                "invalid_json",
                status_code=response.status_code,
                request_id=response.headers.get("Request-ID"),
            ) from exc
        if not isinstance(payload, dict):
            raise RoutableAPIError(
                "invalid_response",
                status_code=response.status_code,
                request_id=response.headers.get("Request-ID"),
            )
        return payload


def valid_bank_payment_method(method: Mapping[str, Any]) -> bool:
    if method.get("type") != "bank" or bool(method.get("is_archived", False)):
        return False
    if method.get("is_valid") is False:
        return False
    verification = str(method.get("verification_status") or "").lower()
    return verification not in {"failed", "invalid", "unverified"}


def invitation_url(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("external_flow_url") or payload.get("invitation_url")
    if isinstance(direct, str) and _is_secure_routable_url(direct):
        return direct
    contacts = payload.get("contacts")
    if isinstance(contacts, Mapping):
        for contact in contacts.get("results") or []:
            if not isinstance(contact, Mapping):
                continue
            links = contact.get("links")
            if not isinstance(links, Mapping):
                continue
            value = links.get("invitation_url")
            if isinstance(value, str) and _is_secure_routable_url(value):
                return value
    return None


def _is_secure_routable_url(value: str) -> bool:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and (hostname == "routable.com" or hostname.endswith(".routable.com"))
    )


def verify_routable_webhook(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    secret = settings.routable_webhook_secret
    company_id = settings.routable_company_id
    signature = headers.get("Routable-Signature") or headers.get(
        "routable-signature"
    )
    timestamp_raw = headers.get("Routable-Signature-Timestamp") or headers.get(
        "routable-signature-timestamp"
    )
    if not secret or not company_id or not signature or not timestamp_raw:
        return None
    expected = hmac.new(
        secret.encode(),
        timestamp_raw.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - timestamp.astimezone(UTC)).total_seconds()
    if age < 0 or age > _WEBHOOK_MAX_AGE_SECONDS:
        return None
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    required = ("event_name", "event_resource", "company_id", "object_id")
    if any(not isinstance(payload.get(field), str) or not payload[field] for field in required):
        return None
    if payload["company_id"] != company_id:
        return None
    return payload


def _results(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _first_result(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    results = _results(payload)
    return results[0] if results else None


def _response_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if isinstance(payload, Mapping):
        value = payload.get("code") or payload.get("type") or payload.get("title")
        if value:
            return safe_routable_error_code(value)
    return f"http_{response.status_code}"
