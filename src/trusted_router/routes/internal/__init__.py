"""Internal routes — Stripe webhook, attested-gateway authorize/settle/
refund, and the Sentry-test synthetic. Each concern lives in its own
module under this package; this __init__ wires them all together
under one register_internal_routes call."""

from __future__ import annotations

from fastapi import APIRouter

from . import adyen as adyen
from . import broadcast_queue as broadcast_queue
from . import chat_browser_key as chat_browser_key
from . import federation as federation
from . import fetch_image as fetch_image
from . import gateway as gateway
from . import paypal as paypal
from . import reconcile as reconcile
from . import sentry as sentry
from . import synthetic as synthetic
from . import veriff as veriff
from . import video_jobs as video_jobs
from . import webhook as webhook


def register_external_webhook_routes(router: APIRouter) -> None:
    """Register signed callbacks that must remain reachable from providers."""
    webhook.register(router)
    paypal.register(router)
    adyen.register(router)
    veriff.register(router)


def register_control_internal_routes(router: APIRouter) -> None:
    """Register private helpers owned by the authenticated control surface."""
    chat_browser_key.register(router)


def register_gateway_internal_routes(router: APIRouter) -> None:
    """Register token-authenticated billing and federation authority routes."""
    broadcast_queue.register(router)
    gateway.register(router)
    video_jobs.register(router)
    fetch_image.register(router)
    reconcile.register(router)
    federation.register(router)


def register_observer_internal_routes(router: APIRouter) -> None:
    """Register synthetic-monitor and observability-only internal routes."""
    synthetic.register(router)
    sentry.register(router)


def register_internal_routes(router: APIRouter) -> None:
    """Legacy local/test composite containing every internal route group."""
    register_external_webhook_routes(router)
    register_control_internal_routes(router)
    register_gateway_internal_routes(router)
    register_observer_internal_routes(router)


__all__ = [
    "register_control_internal_routes",
    "register_external_webhook_routes",
    "register_gateway_internal_routes",
    "register_internal_routes",
    "register_observer_internal_routes",
]
