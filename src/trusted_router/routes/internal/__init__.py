"""Internal routes — Stripe webhook, attested-gateway authorize/settle/
refund, and the Sentry-test synthetic. Each concern lives in its own
module under this package; this __init__ wires them all together
under one register_internal_routes call."""

from __future__ import annotations

from fastapi import APIRouter

from trusted_router.routes.internal import (
    broadcast_queue,
    fetch_image,
    gateway,
    paypal,
    sentry,
    synthetic,
    webhook,
)


def register_internal_routes(router: APIRouter) -> None:
    webhook.register(router)
    paypal.register(router)
    broadcast_queue.register(router)
    gateway.register(router)
    fetch_image.register(router)
    synthetic.register(router)
    sentry.register(router)


__all__ = ["register_internal_routes"]
