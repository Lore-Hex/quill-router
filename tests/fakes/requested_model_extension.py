"""Exact oracle comparison for the intentional requested-model metadata addition.

Frozen executable sources stay untouched. Only recognized generation JSON for
the fixture's one generation gets the independently expected new field. SQL,
RPC counts, ledger state and every pre-existing payload field still match.
"""

from __future__ import annotations

import json
from typing import Any


def compare_with_requested_model(
    left: Any, right: Any, *, generation_id: str, requested_model: str,
    path: tuple[Any, ...] = (),
) -> None:
    if isinstance(left, str) and isinstance(right, str) and left.startswith("{"):
        try:
            payload = json.loads(left)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and (
            payload.get("id") == generation_id or payload.get("generation_id") == generation_id
        ) and {"tokens_prompt", "tokens_completion", "total_cost_microdollars"} <= payload.keys():
            assert "requested_model" not in payload, "frozen baseline already has this extension"
            left = {**payload, "requested_model": requested_model}
            right = json.loads(right)
            # Preserve JSON number/boolean types as well as values: Python's
            # equality would otherwise accept 42.0 for integer microdollars.
            assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True), (path, left, right)
            return
    if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
        for key in left:
            compare_with_requested_model(
                left[key], right[key], generation_id=generation_id,
                requested_model=requested_model, path=(*path, key),
            )
    elif isinstance(left, (list, tuple)) and isinstance(right, type(left)) and len(left) == len(right):
        for index, (old, new) in enumerate(zip(left, right, strict=True)):
            compare_with_requested_model(
                old, new, generation_id=generation_id,
                requested_model=requested_model, path=(*path, index),
            )
    else:
        assert left == right, (path, left, right)
