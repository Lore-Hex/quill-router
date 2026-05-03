from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.errors import api_error
from trusted_router.schemas import SignupRequest
from trusted_router.storage import STORE
from trusted_router.types import ErrorType


def register_signup_routes(router: APIRouter) -> None:
    @router.post("/signup")
    async def signup(body: SignupRequest) -> JSONResponse:
        result = STORE.signup(email=str(body.email).lower(), workspace_name=body.name)
        if result is None:
            raise api_error(
                409,
                "This email is already registered. Sign in with your saved management key.",
                ErrorType.ALREADY_REGISTERED,
            )
        return JSONResponse(
            {
                "data": {
                    "key": result.raw_key,
                    "key_id": result.api_key.hash,
                    "user_id": result.user.id,
                    "email": result.user.email,
                    "workspace_id": result.workspace.id,
                    "workspace_name": result.workspace.name,
                    "trial_credit_microdollars": result.trial_credit_microdollars,
                    "management": True,
                }
            },
            status_code=201,
        )
