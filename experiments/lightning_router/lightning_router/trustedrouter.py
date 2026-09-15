"""HTTPS-only connection to TrustedRouter's narrow USD funding authority."""

from urllib.parse import urlsplit

import httpx

from .errors import FundingReviewRequired
from .money import usd


class TrustedRouterCredits:
    def __init__(self, endpoint: str, token: str, client: httpx.Client) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Funding authority must be an HTTPS origin")
        if parsed.path not in {"", "/"} or len(token) < 32:
            raise ValueError("Invalid funding authority configuration")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.client = client

    def _request(self, path: str, body: dict[str, object]) -> dict[str, object]:
        response = self.client.post(
            self.endpoint + "/internal/lightning/" + path,
            json=body, headers={"Authorization": "Bearer " + self.token},
            timeout=15, follow_redirects=False,
        )
        if response.status_code in {401, 403, 404}:
            raise KeyError("Funding account unavailable")
        if response.status_code in {400, 409, 422}:
            raise FundingReviewRequired("credit_rejected")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid funding authority response")
        return payload

    def resolve(self, raw_key: str, *, new: bool) -> str:
        payload = self._request("resolve", {"api_key": raw_key, "new": new})
        value = payload.get("account_id")
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("Invalid funding account")
        return value

    def health(self) -> None:
        if self._request("health", {}).get("ready") is not True:
            raise ValueError("Funding authority is not ready")

    def balance(self, account_id: str) -> int:
        value = self._request("balance", {"account_id": account_id}).get("available_microdollars")
        if type(value) is not int or value < 0:
            raise ValueError("Invalid USD balance")
        return value

    def credit(self, account_id: str, payment_hash: str, amount_microdollars: int) -> None:
        payload = self._request("credit", {
            "account_id": account_id, "payment_hash": payment_hash,
            "amount_microdollars": amount_microdollars,
        })
        if payload.get("committed") is not True:
            raise ValueError("USD credit not acknowledged")

    def usage(self, raw_key: str) -> dict[str, str | None]:
        # Self-introspection uses the customer's key, never funding authority.
        # Do not forward the response wholesale: it can include identifiers.
        response = self.client.get(self.endpoint + "/v1/key", headers={
            "Authorization": "Bearer " + raw_key,
        }, timeout=15, follow_redirects=False)
        if response.status_code in {401, 403, 404}:
            raise KeyError("Usage unavailable for this key")
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("Invalid key usage response")
        result: dict[str, str | None] = {}
        for field in ("usage", "byok_usage", "reserved", "limit", "limit_remaining"):
            value = data.get(field + "_microdollars")
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid key usage")
            result[field + "_usd"] = usd(value) if value is not None else None
        return result
