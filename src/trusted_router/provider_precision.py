"""Reviewed serving-code metadata, separate from per-request attestation.

Lookups are local and exact-route only. Neither provider branding nor another
host's deployment establishes a model's weight format on this endpoint.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trusted_router.catalog_data import ModelEndpoint

logger = logging.getLogger(__name__)
_SNAPSHOT = Path(__file__).parent / "data/provider_precision.json"


class PrecisionSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    url: str = Field(pattern=r"^https://")
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ProviderPrecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_id: str
    upstream_id: str
    quantization: Literal["bf16", "fp8", "nvfp4", "mxfp4", "int4"]
    label: str
    weight_formats: tuple[str, ...]
    kv_cache_dtype: str | None = None
    model_repository: str
    model_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    reviewed_on: date
    sources: tuple[PrecisionSource, ...] = Field(min_length=2)
    notes: str
    evidence_type: Literal["published_serving_config"] = "published_serving_config"
    runtime_verified: Literal[False] = False

    def metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"provider", "model_id", "upstream_id"})


@lru_cache(maxsize=1)
def _precision_index() -> dict[tuple[str, str, str], ProviderPrecision]:
    try:
        rows = json.loads(_SNAPSHOT.read_text())
        index = {}
        for row in rows:
            record = ProviderPrecision.model_validate(row)
            key = (record.provider, record.model_id, record.upstream_id)
            if key in index:
                raise ValueError("duplicate precision route")
            index[key] = record
        return index
    except (OSError, ValueError, TypeError):
        # Informational evidence must not take catalog or inference offline.
        logger.warning("provider_precision_snapshot_invalid")
        return {}


def endpoint_precision(endpoint: ModelEndpoint) -> ProviderPrecision | None:
    return _precision_index().get((endpoint.provider, endpoint.model_id, endpoint.upstream_id or ""))


def endpoint_precision_metadata(endpoint: ModelEndpoint) -> dict[str, Any] | None:
    precision = endpoint_precision(endpoint)
    return precision.metadata() if precision else None


def endpoint_quantization(endpoint: ModelEndpoint) -> str | None:
    precision = endpoint_precision(endpoint)
    return precision.quantization if precision else None
