"""Internal routes — Stripe webhook, attested-gateway authorize/settle/
refund, and the Sentry-test synthetic. Each concern lives in its own
module under this package; this __init__ wires them all together
under one register_internal_routes call."""

from __future__ import annotations

from fastapi import APIRouter

from . import broadcast_queue as broadcast_queue
from . import fetch_image as fetch_image
from . import gateway as gateway
from . import paypal as paypal
from . import reconcile as reconcile
from . import sentry as sentry
from . import synthetic as synthetic
from . import webhook as webhook


def register_internal_routes(router: APIRouter) -> None:
    webhook.register(router)
    paypal.register(router)
    broadcast_queue.register(router)
    gateway.register(router)
    fetch_image.register(router)
    reconcile.register(router)
    synthetic.register(router)
    sentry.register(router)


__all__ = ["register_internal_routes"]
