import pytest
from lightning_router.reasoning import PROFILES, reasoning_profile, register


@pytest.mark.parametrize("model_id", PROFILES)
def test_review_is_exact_sourced_and_isolated(model_id):
    profile = reasoning_profile({"id": model_id})
    assert profile["source"].startswith("https://")
    assert profile["reviewed_at"] == "2026-09-15"
    assert profile["setup_default"] is None or profile["setup_default"] in profile["setup_efforts"]
    profile["values"].append("not-a-level")
    assert "not-a-level" not in reasoning_profile({"id": model_id})["values"]
    assert reasoning_profile({"id": model_id + "-future"})["status"] == "unverified"


@pytest.mark.parametrize("model_id", ["unknown/reasoner", "deepseek/deepseek-v4-1-flash", "openai/gpt-6-future"])
def test_unknown_or_hosted_alias_does_not_inherit_native_controls(model_id):
    profile = reasoning_profile({"id": model_id, "supported_parameters": ["reasoning_effort"]})
    assert profile["setup_efforts"] == []
    assert profile["default"] is None


def test_distinct_levels_and_switches_are_not_generic_effort():
    def get(model):
        return reasoning_profile({"id": model})
    assert get("deepseek/deepseek-v4.1-flash")["values"] == ["low", "high", "max"]
    assert get("z-ai/glm-5.2")["values"] == ["high", "max"]
    assert get("z-ai/glm-5.3")["values"] == ["low", "high", "max"]
    assert get("minimax/minimax-m3")["values"] == ["disabled", "adaptive"]
    assert get("minimax/minimax-m2.5")["setup_efforts"] == []


def test_anthropic_native_levels_are_not_advertised_as_chat_budget_mapping():
    profile = reasoning_profile({"id": "anthropic/claude-opus-4.8"})
    assert profile["values"] == ["low", "medium", "high", "xhigh", "max"]
    assert profile["default"] == "high"
    assert profile["setup_default"] is None
    assert "1024/4096/8192" in profile["note"]
    assert "not mapped" in profile["note"]
    assert "max" not in reasoning_profile({"id": "anthropic/claude-opus-4.5"})["values"]


def test_duplicate_review_cannot_silently_replace_a_profile():
    model_id = "deepseek/deepseek-v4.1-flash"
    before = reasoning_profile({"id": model_id})
    with pytest.raises(ValueError, match="Duplicate reasoning review"):
        register((model_id,), field="wrong", values=(), default=None,
                 source="https://example.invalid", note="wrong")
    assert reasoning_profile({"id": model_id}) == before


def test_kimi_thinking_and_effort_are_model_specific():
    assert reasoning_profile({"id": "moonshotai/kimi-k2.6"})["values"] == ["disabled", "enabled"]
    assert reasoning_profile({"id": "moonshotai/kimi-k2.7-code"})["values"] == []
    assert reasoning_profile({"id": "moonshotai/kimi-k3"})["values"] == ["low", "high", "max"]
