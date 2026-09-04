from __future__ import annotations

import dataclasses
import re
from typing import Any, TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import dollars_to_microdollars, format_money_display
from trusted_router.oauth_app_policy import user_can_receive_creator_payouts
from trusted_router.routable_payouts import (
    MICRODOLLARS_PER_CENT,
    ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS,
    ROUTABLE_RELEASE_STATUSES,
    new_payout_id,
    normalize_routable_status,
    payout_idempotency_entity_id,
    payout_request_fingerprint,
    routable_company_external_id,
    routable_error_is_definitive_no_effect,
    routable_idempotency_key,
    routable_payable_external_id,
    routable_send_date,
    safe_routable_error_code,
)
from trusted_router.routes.console._shared import require_console_context
from trusted_router.routes.helpers import json_body
from trusted_router.services.routable_payouts import (
    RoutableAPIError,
    RoutableClient,
    invitation_url,
    valid_bank_payment_method,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import EarningsCashout, RoutablePayoutProfile, User, iso_now
from trusted_router.types import ErrorType

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_IDEMPOTENCY_KEY_MAX_LENGTH = 128


class _OnboardingValues(TypedDict):
    recipient_type: str
    country_code: str
    first_name: str
    last_name: str
    business_name: str | None


def register_payout_routes(router: APIRouter) -> None:
    @router.get("/payouts")
    async def list_payouts(
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        user = _resolved_user(request, settings)
        return await run_in_threadpool(payout_status, user, settings)

    @router.get("/payouts/{payout_id}")
    async def get_payout(
        payout_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        user = _resolved_user(request, settings)
        cashout = await run_in_threadpool(
            STORE.get_earnings_cashout,
            user.id,
            payout_id,
        )
        if cashout is None:
            raise api_error(404, "Payout not found", ErrorType.NOT_FOUND)
        return {"data": _cashout_shape(cashout)}

    @router.post("/payouts/{payout_id}/retry")
    async def retry_payout(
        payout_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> JSONResponse:
        user = _resolved_user(request, settings)
        _require_payout_identity(user)
        _require_routable(settings)
        cashout = await run_in_threadpool(
            STORE.get_earnings_cashout,
            user.id,
            payout_id,
        )
        if cashout is None:
            raise api_error(404, "Payout not found", ErrorType.NOT_FOUND)
        submitted = await _submit_or_reconcile(RoutableClient(settings), cashout)
        status_code = (
            202 if submitted.state in {"reserved", "submitting", "submission_unknown"} else 200
        )
        return JSONResponse({"data": _cashout_shape(submitted)}, status_code=status_code)

    @router.post("/payouts/onboarding")
    async def start_payout_onboarding(
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        user = _resolved_user(request, settings)
        _require_payout_identity(user)
        _require_routable(settings)
        values = _onboarding_values(await json_body(request), user)
        client = RoutableClient(settings)
        external_id = routable_company_external_id(user.id)
        company = await client.find_company(external_id)
        if company is None:
            company = await client.create_company(
                external_id=external_id,
                recipient_type=values["recipient_type"],
                country_code=values["country_code"],
                first_name=values["first_name"],
                last_name=values["last_name"],
                email=str(user.email),
                business_name=values["business_name"],
            )
        profile = await _save_company_profile(
            client=client,
            user=user,
            company=company,
            recipient_type=values["recipient_type"],
            country_code=values["country_code"],
        )
        if profile.company_status == "accepted" and profile.payment_method_id:
            return {
                "data": _profile_shape(profile),
                "onboarding_url": None,
            }
        redirect_url = (
            f"https://{settings.trusted_domain}/console/earnings?routable=return"
        )
        if profile.company_status == "added":
            response = await client.invite_company(
                profile.routable_company_id,
                confirmation_redirect_url=redirect_url,
            )
        else:
            response = await client.reinvite_company(
                profile.routable_company_id,
                confirmation_redirect_url=redirect_url,
            )
        url = invitation_url(response)
        if url is None:
            raise api_error(
                502,
                "Routable did not return a secure onboarding URL",
                ErrorType.INTERNAL_ERROR,
            )
        profile = dataclasses.replace(
            profile,
            company_status="invited",
            updated_at=iso_now(),
        )
        await run_in_threadpool(STORE.upsert_routable_payout_profile, profile)
        return {"data": _profile_shape(profile), "onboarding_url": url}

    @router.post("/payouts")
    async def create_payout(
        request: Request,
        settings: SettingsDep,
    ) -> JSONResponse:
        user = _resolved_user(request, settings)
        _require_payout_identity(user)
        _require_routable(settings)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
            raise api_error(
                400,
                "Idempotency-Key is required and must be at most 128 characters",
                ErrorType.BAD_REQUEST,
            )
        body = await json_body(request)
        unknown = set(body) - {"amount"}
        if unknown:
            raise api_error(400, "Unknown payout field", ErrorType.BAD_REQUEST)
        try:
            amount = dollars_to_microdollars(body.get("amount"))
        except ValueError as exc:
            raise api_error(400, "Invalid payout amount", ErrorType.BAD_REQUEST) from exc
        if amount < ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS:
            raise api_error(
                400,
                "USD cash-outs require at least $100.00",
                ErrorType.BAD_REQUEST,
            )
        if amount % MICRODOLLARS_PER_CENT:
            raise api_error(
                400,
                "USD cash-outs must use whole cents",
                ErrorType.BAD_REQUEST,
            )
        client = RoutableClient(settings)
        profile = await _refresh_profile(client, user)
        if profile.company_status != "accepted" or not profile.payment_method_id:
            raise api_error(
                409,
                "Complete payout onboarding before requesting a cash-out",
                ErrorType.CONFLICT,
            )
        payout_id = new_payout_id()
        fingerprint = payout_request_fingerprint(
            user_id=user.id,
            amount_microdollars=amount,
            routable_company_id=profile.routable_company_id,
            payment_method_id=profile.payment_method_id,
        )
        cashout = EarningsCashout(
            id=payout_id,
            user_id=user.id,
            amount_microdollars=amount,
            state="reserved",
            balance_status="reserved",
            idempotency_fingerprint=fingerprint,
            routable_idempotency_key=routable_idempotency_key(payout_id),
            external_id=routable_payable_external_id(payout_id),
            routable_company_id=profile.routable_company_id,
            payment_method_id=profile.payment_method_id,
        )
        outcome, reserved = await run_in_threadpool(
            STORE.reserve_earnings_cashout,
            cashout,
            idempotency_entity_id=payout_idempotency_entity_id(
                user.id, idempotency_key
            ),
        )
        if outcome == "conflict":
            raise api_error(
                409,
                "Idempotency-Key was already used for a different payout",
                ErrorType.CONFLICT,
            )
        if outcome == "insufficient" or reserved is None:
            raise api_error(400, "Available earnings are insufficient", ErrorType.BAD_REQUEST)
        submitted = await _submit_or_reconcile(client, reserved)
        response_status = 201 if submitted.state not in {"submission_unknown", "reserved"} else 202
        return JSONResponse({"data": _cashout_shape(submitted)}, status_code=response_status)


def _resolved_user(request: Request, settings: Settings) -> User:
    try:
        context = require_console_context(request, settings)
    except HTTPException as exc:
        if exc.status_code != 302:
            raise
        raise api_error(
            403,
            "Payout management requires a signed-in console session cookie",
            ErrorType.FORBIDDEN,
        ) from exc
    if not context.can_manage:
        raise api_error(403, "Payout management is not permitted", ErrorType.FORBIDDEN)
    return context.user


def _require_payout_identity(user: User) -> None:
    if not user_can_receive_creator_payouts(user):
        raise api_error(
            403,
            "Full identity verification is required before USD cash-outs",
            ErrorType.VERIFICATION_REQUIRED,
        )
    if not user.email or not user.email_verified:
        raise api_error(
            403,
            "A verified email address is required before USD cash-outs",
            ErrorType.VERIFICATION_REQUIRED,
        )


def _require_routable(settings: Settings) -> None:
    if not settings.routable_configured:
        raise api_error(
            503,
            "USD cash-outs are not enabled yet",
            ErrorType.SERVICE_UNAVAILABLE,
            headers={"Retry-After": "86400"},
        )


def _onboarding_values(body: dict[str, Any], user: User) -> _OnboardingValues:
    unknown = set(body) - {
        "recipient_type",
        "country_code",
        "first_name",
        "last_name",
        "business_name",
    }
    if unknown:
        raise api_error(400, "Unknown payout onboarding field", ErrorType.BAD_REQUEST)
    recipient_type = str(body.get("recipient_type") or "personal").strip().lower()
    if recipient_type not in {"personal", "business"}:
        raise api_error(400, "recipient_type must be personal or business", ErrorType.BAD_REQUEST)
    country_code = str(body.get("country_code") or "US").strip().upper()
    if not _COUNTRY_CODE.fullmatch(country_code):
        raise api_error(400, "country_code must be ISO 3166-1 alpha-2", ErrorType.BAD_REQUEST)
    fallback = str(user.identity_verified_name or "").strip().split(maxsplit=1)
    first_name = _name(body.get("first_name") or (fallback[0] if fallback else ""), "first_name")
    last_name = _name(
        body.get("last_name") or (fallback[1] if len(fallback) == 2 else ""),
        "last_name",
    )
    business_name = None
    if recipient_type == "business":
        business_name = _name(body.get("business_name"), "business_name", max_length=128)
    return {
        "recipient_type": recipient_type,
        "country_code": country_code,
        "first_name": first_name,
        "last_name": last_name,
        "business_name": business_name,
    }


def _name(value: object, field: str, *, max_length: int = 80) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise api_error(
            400,
            f"{field} is required and must be at most {max_length} characters",
            ErrorType.BAD_REQUEST,
        )
    return value.strip()


async def _save_company_profile(
    *,
    client: RoutableClient,
    user: User,
    company: dict[str, Any],
    recipient_type: str,
    country_code: str,
) -> RoutablePayoutProfile:
    company_id = str(company.get("id") or "")
    if not company_id:
        raise api_error(502, "Routable company response was incomplete", ErrorType.INTERNAL_ERROR)
    methods = await client.list_payment_methods(company_id)
    bank_method = next((method for method in methods if valid_bank_payment_method(method)), None)
    existing = await run_in_threadpool(STORE.get_routable_payout_profile, user.id)
    profile = RoutablePayoutProfile(
        user_id=user.id,
        routable_company_id=company_id,
        company_status=str(company.get("status") or "added").lower(),
        recipient_type=recipient_type,
        country_code=country_code,
        payment_method_id=(str(bank_method.get("id")) if bank_method else None),
        payment_method_type=(str(bank_method.get("type")) if bank_method else None),
        created_at=existing.created_at if existing is not None else iso_now(),
        updated_at=iso_now(),
    )
    return await run_in_threadpool(STORE.upsert_routable_payout_profile, profile)


async def _refresh_profile(
    client: RoutableClient,
    user: User,
) -> RoutablePayoutProfile:
    existing = await run_in_threadpool(STORE.get_routable_payout_profile, user.id)
    if existing is None:
        raise api_error(
            409,
            "Start payout onboarding before requesting a cash-out",
            ErrorType.CONFLICT,
        )
    company = await client.retrieve_company(existing.routable_company_id)
    return await _save_company_profile(
        client=client,
        user=user,
        company=company,
        recipient_type=existing.recipient_type,
        country_code=existing.country_code,
    )


async def _submit_or_reconcile(
    client: RoutableClient,
    cashout: EarningsCashout,
) -> EarningsCashout:
    if cashout.balance_status in {"paid", "released"}:
        return cashout
    try:
        payable = await client.find_payable(cashout.external_id)
        if payable is None:
            await run_in_threadpool(
                STORE.mark_earnings_cashout,
                cashout.user_id,
                cashout.id,
                state="submitting",
                increment_attempts=True,
            )
            payable = await client.create_payable(
                amount_microdollars=cashout.amount_microdollars,
                external_id=cashout.external_id,
                company_id=cashout.routable_company_id,
                payment_method_id=cashout.payment_method_id,
                idempotency_key=cashout.routable_idempotency_key,
                send_on=routable_send_date(cashout.created_at),
            )
    except RoutableAPIError as exc:
        if routable_error_is_definitive_no_effect(exc.status_code):
            _, updated = await run_in_threadpool(
                STORE.release_earnings_cashout,
                cashout.user_id,
                cashout.id,
                state="rejected",
                error_code=safe_routable_error_code(exc.code),
            )
            return updated or cashout
        updated = await run_in_threadpool(
            STORE.mark_earnings_cashout,
            cashout.user_id,
            cashout.id,
            state="submission_unknown",
            error_code=safe_routable_error_code(exc.code),
        )
        return updated or cashout
    payable_id = str(payable.get("id") or "")
    if not payable_id:
        updated = await run_in_threadpool(
            STORE.mark_earnings_cashout,
            cashout.user_id,
            cashout.id,
            state="submission_unknown",
            error_code="missing_payable_id",
        )
        return updated or cashout
    status = normalize_routable_status(payable.get("status"))
    if status in ROUTABLE_RELEASE_STATUSES:
        _, updated = await run_in_threadpool(
            STORE.release_earnings_cashout,
            cashout.user_id,
            cashout.id,
            state=status,
            routable_status=status,
            error_code="routable_terminal_failure",
        )
        return updated or cashout
    state = status or "submitted"
    updated = await run_in_threadpool(
        STORE.mark_earnings_cashout,
        cashout.user_id,
        cashout.id,
        state=state,
        routable_payable_id=payable_id,
        routable_status=status,
        error_code=None,
    )
    return updated or cashout


def payout_status(user: User, settings: Settings) -> dict[str, Any]:
    summary = STORE.earnings_summary(user.id, allow_stale=True)
    profile = STORE.get_routable_payout_profile(user.id)
    return {
        "data": [_cashout_shape(item) for item in STORE.list_earnings_cashouts(user.id)],
        "payouts_enabled": settings.routable_configured,
        "identity_verified": user_can_receive_creator_payouts(user),
        "email_verified": bool(user.email and user.email_verified),
        "minimum_cashout_microdollars": ROUTABLE_MINIMUM_CASHOUT_MICRODOLLARS,
        "minimum_cashout": "$100.00",
        "available_microdollars": summary["available"],
        "available": format_money_display(summary["available"]),
        "profile": _profile_shape(profile) if profile is not None else None,
    }


def _profile_shape(profile: RoutablePayoutProfile) -> dict[str, Any]:
    return {
        "company_status": profile.company_status,
        "recipient_type": profile.recipient_type,
        "country_code": profile.country_code,
        "payment_method_ready": bool(profile.payment_method_id),
        "payment_method_type": profile.payment_method_type,
        "updated_at": profile.updated_at,
    }


def _cashout_shape(cashout: EarningsCashout) -> dict[str, Any]:
    return {
        "id": cashout.id,
        "amount_microdollars": cashout.amount_microdollars,
        "amount": format_money_display(cashout.amount_microdollars),
        "currency": cashout.currency,
        "state": cashout.state,
        "balance_status": cashout.balance_status,
        "routable_status": cashout.routable_status,
        "created_at": cashout.created_at,
        "updated_at": cashout.updated_at,
    }
