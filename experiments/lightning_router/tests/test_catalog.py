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


def test_catalog_distinguishes_reasoning_from_effort_control():
    rows = [model("a/effort"), model("b/reasoning-only"), model("c/plain")]
    rows[0]["supported_parameters"] += ["reasoning", "reasoning_effort"]
    rows[1]["supported_parameters"] += ["reasoning"]
    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": rows}))))
    assert [(row["id"], row["reasoning_effort"]) for row in catalog.current()] == [
        ("a/effort", True), ("b/reasoning-only", False), ("c/plain", False)]
