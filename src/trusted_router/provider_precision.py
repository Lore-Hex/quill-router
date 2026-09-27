"""Reviewed serving-code metadata, separate from per-request attestation.

Lookups are local and exact-route only. Neither provider branding nor another
host's deployment establishes a model's weight format on this endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from trusted_router.catalog_data import ModelEndpoint

logger = logging.getLogger(__name__)
_SNAPSHOT = Path(__file__).parent / "data/provider_precision.json"


@dataclass(frozen=True)
class PrecisionSource:
    title: str
    url: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("missing precision source title")
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise ValueError("invalid precision source URL")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256)
        ):
            raise ValueError("invalid precision source hash")


@dataclass(frozen=True)
class ProviderPrecision:
    provider: str
    model_id: str
    upstream_id: str
    quantization: Literal["bf16", "fp8", "nvfp4", "mxfp4", "int4"]
    label: str
    weight_formats: tuple[str, ...]
    kv_cache_dtype: str | None
    model_repository: str
    model_revision: str
    reviewed_on: date
    sources: tuple[PrecisionSource, ...]
    notes: str
    evidence_type: Literal["published_serving_config"] = "published_serving_config"
    runtime_verified: Literal[False] = False

    def __post_init__(self) -> None:
        for field in (self.provider, self.model_id, self.upstream_id, self.label,
                      self.model_repository, self.model_revision, self.notes):
            if not isinstance(field, str) or not field:
                raise ValueError("invalid precision field")
        if self.quantization not in ("bf16", "fp8", "nvfp4", "mxfp4", "int4"):
            raise ValueError("invalid weight format")
        if self.quantization not in self.weight_formats or not all(
            v in ("bf16", "fp4", "fp8", "nvfp4", "mxfp4", "int4") for v in self.weight_formats
        ):
            raise ValueError("invalid weight formats")
        if self.kv_cache_dtype is not None and not isinstance(self.kv_cache_dtype, str):
            raise ValueError("invalid KV cache dtype")
        if not re.fullmatch(r"[a-f0-9]{40}", self.model_revision) or len(self.sources) < 2:
            raise ValueError("missing pinned precision evidence")
        if self.runtime_verified is not False or self.evidence_type != "published_serving_config":
            raise ValueError("published configs are not runtime proofs")

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ProviderPrecision:
        values = dict(row)
        values["reviewed_on"] = date.fromisoformat(values["reviewed_on"])
        values["sources"] = tuple(PrecisionSource(**source) for source in values["sources"])
        values["weight_formats"] = tuple(values["weight_formats"])
        return cls(**values)

    def metadata(self) -> dict[str, Any]:
        result = asdict(self)
        for field in ("provider", "model_id", "upstream_id"):
            del result[field]
        result["reviewed_on"] = self.reviewed_on.isoformat()
        result["sources"] = list(result["sources"])
        result["weight_formats"] = list(result["weight_formats"])
        return result


@lru_cache(maxsize=1)
def _precision_index() -> dict[tuple[str, str, str], ProviderPrecision]:
    try:
        rows = json.loads(_SNAPSHOT.read_text())
        index = {}
        for row in rows:
            record = ProviderPrecision.from_dict(row)
            key = (record.provider, record.model_id, record.upstream_id)
            if key in index:
                raise ValueError("duplicate precision route")
            index[key] = record
        return index
    except (OSError, ValueError, TypeError, KeyError):
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
