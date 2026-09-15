import httpx
from lightning_router.app import Catalog


def model(model_id, **overrides):
    return {"id": model_id, "name": model_id,
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["tools"],
            "trustedrouter": {"prepaid_available": True, "supports_chat": True, **overrides}}


def test_catalog_only_lists_public_prepaid_chat_tool_candidates():
    rows = [model("a/good"), model("b/byok", prepaid_available=False),
            model("c/monitor", internal_only=True), model("d/hidden", configuration_hidden=True),
            model("e/image", supports_chat=False), model("x';&/injected")]
    calls = []
    def handle(request):
        calls.append(request)
        assert not request.headers.get("authorization")
        return httpx.Response(200, json={"data": rows})
    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(handle)))
    assert [row["id"] for row in catalog.current()] == ["a/good"]
    catalog.current()
    assert len(calls) == 1


def test_catalog_flag_does_not_invent_effort_values():
    rows = [model("a/effort"), model("b/reasoning-only"), model("c/plain")]
    rows[0]["supported_parameters"] += ["reasoning", "reasoning_effort"]
    rows[1]["supported_parameters"] += ["reasoning"]
    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": rows}))))
    for row in catalog.current():
        assert "reasoning_effort" not in row
        assert row["reasoning"]["status"] == "unverified"
        assert row["reasoning"]["setup_efforts"] == []


def test_catalog_includes_exact_reviewed_model_controls():
    rows = [model("deepseek/deepseek-v4.1-flash"), model("anthropic/claude-opus-4.8")]
    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": rows}))))
    profiles = {row["id"]: row["reasoning"] for row in catalog.current()}
    assert profiles[rows[0]["id"]]["setup_efforts"] == ["low", "high", "max"]
    assert profiles[rows[1]["id"]]["field"] == "output_config.effort"
    assert profiles[rows[1]["id"]]["setup_efforts"] == []
