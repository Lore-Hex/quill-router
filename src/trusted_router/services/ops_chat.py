from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx

from trusted_router.config import Settings


@dataclass(frozen=True)
class OpsChatSupportMessage:
    message_id: str
    name: str
    email: str
    subject: str
    message: str


@dataclass(frozen=True)
class OpsChatFanoutResult:
    configured: int
    accepted: int


def _destinations(settings: Settings) -> tuple[str, ...]:
    return tuple(
        value.strip().rstrip("/")
        for value in settings.ops_chat_webhook_urls.split(",")
        if value.strip()
    )


async def fanout_support_message(
    settings: Settings,
    message: OpsChatSupportMessage,
) -> OpsChatFanoutResult:
    destinations = _destinations(settings)
    secret = settings.ops_chat_webhook_secret or ""
    if not destinations:
        return OpsChatFanoutResult(configured=0, accepted=0)
    if not secret:
        return OpsChatFanoutResult(configured=len(destinations), accepted=0)

    body = json.dumps(
        {
            "message_id": message.message_id,
            "name": message.name,
            "email": message.email,
            "subject": message.subject,
            "message": message.message,
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    timeout = httpx.Timeout(settings.ops_chat_webhook_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def deliver(destination: str) -> bool:
            try:
                response = await client.post(
                    f"{destination}/hooks/support",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-ops-signature": signature,
                    },
                )
                return 200 <= response.status_code < 300
            except httpx.HTTPError:
                return False

        accepted = sum(await asyncio.gather(*(deliver(url) for url in destinations)))
    return OpsChatFanoutResult(configured=len(destinations), accepted=accepted)
