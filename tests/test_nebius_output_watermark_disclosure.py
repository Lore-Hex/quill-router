"""Nebius Token Factory watermarks generated text (EU AI Act marking), rolled
out 2026-11-27 to 2026-12-01. The public catalog must say so, and the
disclosure must not move Nebius's retention flags or privacy tier:
watermarking changes what the model emits, not what Nebius keeps.
"""

from __future__ import annotations

from trusted_router.catalog import PROVIDERS
from trusted_router.catalog_data import PRIVACY_TIER_ZERO_RETENTION
from trusted_router.catalog_privacy import provider_privacy_tier


def test_nebius_policy_discloses_output_watermark() -> None:
    policy = PROVIDERS["nebius"].provider_policy
    assert "invisible statistical watermark" in policy
    assert "EU AI Act" in policy
    assert "2026-11-27" in policy and "2026-12-01" in policy
    # The notice says variety and speed may drop; the disclosure must not
    # claim outputs are otherwise unchanged.
    assert "output variety" in policy and "generation speed" in policy


def test_watermark_disclosure_leaves_nebius_privacy_posture_unchanged() -> None:
    nebius = PROVIDERS["nebius"]
    assert nebius.stores_content is False
    assert nebius.provider_zero_data_retention is True
    assert provider_privacy_tier(nebius) == PRIVACY_TIER_ZERO_RETENTION
