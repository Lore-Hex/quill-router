"""Dedicated funding credential, never a gateway/observer/operator credential."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.services.lightning import LightningCredits
from trusted_router.storage import STORE
from trusted_router.storage_lightning import MAX_CREDIT_RECEIPT
from trusted_router.types import ErrorType


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resolve(Body):
    api_key: str = Field(min_length=16, max_length=256, repr=False)
    new: StrictBool = False


class Lookup(Body):
    api_key: str = Field(min_length=16, max_length=256, repr=False)


class Account(Body):
    account_id: str = Field(min_length=1, max_length=128)


class Credit(Account):
    payment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    amount_microdollars: StrictInt = Field(gt=0, le=MAX_CREDIT_RECEIPT)


def funding_authority(request: Request, settings: SettingsDep) -> None:
    if not settings.lightning_funding_token:
        raise api_error(403, "Lightning funding is disabled", ErrorType.FORBIDDEN)
    require_internal_gateway(request, settings)


Authority = Annotated[None, Depends(funding_authority)]


def register(router: APIRouter) -> None:
    @router.post("/internal/lightning/account")
    def account(body: Lookup, _authority: Authority) -> dict[str, Any]:
        try:
            value = LightningCredits(STORE).account(body.api_key)
        except ValueError as exc:
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED) from exc
        return {"account_id": value.account_id, "available_microdollars": value.available_microdollars,
                "support_eligible": value.support_eligible}

    @router.post("/internal/lightning/health")
    def health(_authority: Authority) -> dict[str, bool]:
        # Complete indexed key read only; never create an account or credit.
        STORE.get_workspace("ws_lightning_readiness")
        return {"ready": True}

    @router.post("/internal/lightning/resolve")
    def resolve(body: Resolve, _authority: Authority) -> dict[str, str]:
        try:
            account_id = LightningCredits(STORE).resolve(body.api_key, new=body.new)
        except ValueError as exc:
            raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED) from exc
        return {"account_id": account_id}

    @router.post("/internal/lightning/balance")
    def balance(body: Account, _authority: Authority) -> dict[str, int]:
        try:
            value = LightningCredits(STORE).balance(body.account_id)
        except ValueError as exc:
            raise api_error(404, "Credit account not found", ErrorType.NOT_FOUND) from exc
        return {"available_microdollars": value}

    @router.post("/internal/lightning/credit")
    def credit(body: Credit, _authority: Authority) -> dict[str, Any]:
        try:
            LightningCredits(STORE).credit(body.account_id, body.payment_hash, body.amount_microdollars)
        except ValueError as exc:
            raise api_error(409, "Lightning payment conflict", ErrorType.CONFLICT) from exc
        return {"committed": True}
