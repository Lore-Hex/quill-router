from __future__ import annotations

import json

import pytest

from trusted_router.services.contract_value_preview import safe_value_preview


@pytest.mark.parametrize(("path", "value", "expected"), [
    ("store", True, True),
    ("temperature", 0.7, 0.7),
    ("usage.include", "false", "false"),
    ("prompt_cache_retention", "24h", "24h"),
    ("reasoning.effort", "high", "high"),
    ("stream", None, None),
    ("usage", {"include": True}, {"include": True}),
    ("usage", {"include": True, "secret": "private-content"}, {"include": True, "_redacted": True}),
    ("temperature", 1e200, "[redacted:number]"),
    ("temperature", "private-content", "[redacted:string]"),
    ("usage.include", {"secret": "private-content"}, "[redacted:object]"),
    ("usage", ["private-content"], "[redacted:array]"),
    ("future", "private-content", "[redacted:string]"),
    ("future", 123456, "[redacted:number]"),
    ("future", True, "[redacted:boolean]"),
    ("messages", [{"content": "private-content"}], "[redacted:array]"),
    ("prompt", "private-content", "[redacted:string]"),
    ("metadata", {"secret": "private-content"}, "[redacted:object]"),
    ("api_key", "sk-tr-v1-private-secret", "[redacted:string]"),
    ("model", "private-content", "[redacted:string]"),
])
def test_sink_revalidates_configuration_only(path: str, value: object, expected: object) -> None:
    preview, truncated = safe_value_preview(path, json.dumps(value))
    assert preview is not None
    assert json.loads(preview) == expected
    assert not truncated
    assert len(preview) <= 100
    assert safe_value_preview(path, preview) == (preview, False)


@pytest.mark.parametrize("raw", ['"' + "private_" * 100 + '"', "{", "[" * 90, "NaN", "Infinity", "-Infinity"])
def test_bad_or_oversized_preview_is_dropped(raw: str) -> None:
    assert safe_value_preview("temperature", raw) == (None, False)


def test_redaction_expansion_is_still_bounded() -> None:
    raw = '{"allow_fallbacks":"x","require_parameters":"x","zdr":"x","usage":"x"}'
    preview, truncated = safe_value_preview("provider", raw)
    assert preview is not None and len(preview) <= 100 and truncated
    assert "x" not in preview
    assert json.loads(preview)
