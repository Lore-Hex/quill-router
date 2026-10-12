"""Temporary model admission holds for staggered gateway releases."""

from __future__ import annotations

import os

from trusted_router.regions import cloud_for_region


def unavailable_gateway_model_ids() -> frozenset[str]:
    # QCP bfac8577 adds the native Azure decision protocol. Only the GCP
    # gateway fleet has that release; secondary control planes update alone.
    # Match Settings.effective_serving_region without loading credentials.
    region = os.environ.get("TR_SERVING_REGION") or os.environ.get("TR_PRIMARY_REGION")
    if region and cloud_for_region(region) == "gcp":
        return frozenset()
    if not region and os.environ.get("TR_ENVIRONMENT", "local").lower() in {"local", "test"}:
        return frozenset()
    return frozenset({"microsoft/microsoft-decision-1"})
