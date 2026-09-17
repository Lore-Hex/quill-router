"""L402 payment proof for funding only; inference keeps normal API-key auth."""

import hashlib
import hmac
import re
import time
from typing import Any

from fastapi.responses import JSONResponse
from pymacaroons import Macaroon, Verifier

from .service import Funding

PATH = "/api/l402/funding"
LOCATION = "https://lightningrouter.ai" + PATH
PROOF_LIFETIME = 86400


class L402Funding:
    def __init__(self, funding: Funding) -> None:
        self.funding = funding

    @staticmethod
    def caveats(row: dict[str, Any], new: bool) -> list[str]:
        return [
            "purpose = lightningrouter-funding-v1", "method = POST", "path = " + PATH,
            "owner = " + row["key_hash"], "request = " + row["request_id"],
            "usd_cents = " + str(row["usd_cents"]), "new_account = " + str(new).lower(),
            "payment_hash = " + row["payment_hash"],
            "time < " + str(row["created_at"] + PROOF_LIFETIME),
        ]

    def macaroon(self, row: dict[str, Any], new: bool) -> str:
        token = Macaroon(location=LOCATION, identifier=row["id"], key=self.funding.credentials.l402_root_key())
        for caveat in self.caveats(row, new):
            token.add_first_party_caveat(caveat)
        return str(token.serialize())

    def verify(self, authorization: str, row: dict[str, Any], new: bool) -> bool:
        if len(authorization) > 4096 or not row["payment_hash"]:
            return False
        match = re.fullmatch(r"(?i:L402) ([A-Za-z0-9_+/=-]+):([a-fA-F0-9]{64})", authorization)
        if not match or int(time.time()) >= row["created_at"] + PROOF_LIFETIME:
            return False
        # Parsing failures are authentication failures, never upstream errors.
        try:
            token = Macaroon.deserialize(match[1])
            expected = self.caveats(row, new)
            if (token.identifier != row["id"] or token.location != LOCATION
                    or len(token.caveats) != len(expected)
                    or any(c.verification_key_id for c in token.caveats)
                    or {c.caveat_id for c in token.caveats} != set(expected)):
                return False
            verifier = Verifier()
            for caveat in expected:
                verifier.satisfy_exact(caveat)
            verifier.verify(token, self.funding.credentials.l402_root_key())
            return hmac.compare_digest(hashlib.sha256(bytes.fromhex(match[2])).hexdigest(), row["payment_hash"])
        except Exception:
            return False

    def respond(self, raw_key: str, request_id: str, cents: int, *, new: bool, authorization: str) -> JSONResponse:
        funding = self.funding
        owner = funding.credentials.fingerprint(raw_key)
        row = funding.store.by_request(owner, request_id)
        if row and row["usd_cents"] != cents:
            return JSONResponse({"error": "invoice_conflict"}, status_code=409)
        if authorization:
            if row is None or not self.verify(authorization, row, new):
                return JSONResponse({"error": "invalid_l402_proof"}, status_code=401)
            # Payment proof never overrides a revoked account key. Unfunded
            # checkouts are only ownership capabilities, not accounts yet.
            if funding.store.credit_account(owner) is not None:
                funding.account(raw_key)
            result = funding.refresh(row)
        else:
            result = funding.create(raw_key, request_id, cents, new=new)
        row = funding.store.by_request(owner, request_id)
        assert row is not None
        result.pop("qr", None)
        if result["credited"]:
            return JSONResponse(result)
        if result["state"] == "CANCELED":
            return JSONResponse({**result, "error": "invoice_canceled"}, status_code=410)
        if result["attention_required"] or not row["bolt11"]:
            return JSONResponse({**result, "error": "funding_pending"}, status_code=503, headers={"Retry-After": "10"})
        if authorization or result["state"] in {"ACCEPTED", "SETTLED"}:
            # A preimage is not permission to mint credits: only the existing
            # verified settlement/outbox path can do that. Never ask to pay twice.
            return JSONResponse(result, status_code=202, headers={"Retry-After": "10"})
        if result["expired"]:
            return JSONResponse({**result, "error": "invoice_expired"}, status_code=409)
        token = self.macaroon(row, new)
        return JSONResponse({**result, "macaroon": token, "proof_expires_at": row["created_at"] + PROOF_LIFETIME},
                            status_code=402, headers={"WWW-Authenticate": f'L402 macaroon="{token}", invoice="{row["bolt11"]}"'})
