"""Backend-neutral encoding helpers shared by every storage adapter.

``json_body`` used to live in :mod:`trusted_router.storage_gcp_codec` beside
the Spanner/Bigtable key-shape helpers, but it is not GCP-specific in any
way: it is a deterministic JSON dumper that happens to understand dataclass
defaults.  The Postgres adapter already writes its entity bodies with it, and
the Postgres operational-analytics outbox needs it too.  Keeping it in a
``storage_gcp_*`` module would force non-GCP code to import from the GCP
namespace to get a ``json.dumps`` wrapper.

:mod:`trusted_router.storage_gcp_codec` re-exports it, so every existing
import site keeps working unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict
from typing import Any

_SENTINEL = object()


def json_body(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        data = asdict(value)
        for field in dataclasses.fields(value):
            field_value = data.get(field.name, _SENTINEL)
            if field_value is None and field.default is None:
                data.pop(field.name)
            elif field_value == [] and field.default_factory is list:
                data.pop(field.name)
            elif field_value == {} and field.default_factory is dict:
                data.pop(field.name)
        value = data
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
