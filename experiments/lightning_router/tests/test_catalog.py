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
