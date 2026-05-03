from __future__ import annotations

from json import JSONDecodeError
from typing import Any

import pytest
from fastapi import HTTPException

from trusted_router.routes.helpers import (
    float_body_field,
    integer_body_field,
    json_body,
    money_body_field_microdollars,
)


class _FakeRequest:
    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    async def json(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.value


@pytest.mark.asyncio
async def test_json_body_rejects_malformed_and_non_object_payloads() -> None:
    malformed = _FakeRequest(error=JSONDecodeError("bad", "{", 0))
    array_body = _FakeRequest(value=["not", "object"])

    with pytest.raises(HTTPException) as malformed_exc:
        await json_body(malformed)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as array_exc:
        await json_body(array_body)  # type: ignore[arg-type]

    assert malformed_exc.value.status_code == 400
    assert malformed_exc.value.detail["error"]["message"] == "Malformed JSON"
    assert array_exc.value.status_code == 400
    assert array_exc.value.detail["error"]["message"] == "JSON body must be an object"


def test_numeric_body_helpers_preserve_tiny_values_and_stable_errors() -> None:
    assert integer_body_field({"max_tokens": "3"}, "max_tokens", default=1, minimum=1) == 3
    assert float_body_field({"temperature": "0.25"}, "temperature", default=0.0, minimum=0.0) == 0.25
    assert money_body_field_microdollars({"limit": "0.000001"}, "limit", default=1, minimum_microdollars=1) == 1

    with pytest.raises(HTTPException) as int_exc:
        integer_body_field({"max_tokens": "abc"}, "max_tokens", default=1, minimum=1)
    with pytest.raises(HTTPException) as float_exc:
        float_body_field({"temperature": "cold"}, "temperature", default=0.0, minimum=0.0)
    with pytest.raises(HTTPException) as money_exc:
        money_body_field_microdollars({"limit": "nan"}, "limit", default=1, minimum_microdollars=1)
    with pytest.raises(HTTPException) as min_exc:
        money_body_field_microdollars({"limit": "0"}, "limit", default=1, minimum_microdollars=1)

    assert int_exc.value.detail["error"]["message"] == "max_tokens must be an integer"
    assert float_exc.value.detail["error"]["message"] == "temperature must be a number"
    assert money_exc.value.detail["error"]["message"] == "limit must be a dollar amount"
    assert min_exc.value.detail["error"]["message"] == "limit must be at least 1e-06"
