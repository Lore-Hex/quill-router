from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.errors import error_response, not_supported


def register_compat_stub_routes(router: APIRouter) -> None:
    @router.post("/audio/speech")
    async def audio_speech() -> JSONResponse:
        return not_supported()

    @router.post("/audio/transcriptions")
    async def audio_transcriptions() -> JSONResponse:
        return not_supported()

    @router.post("/rerank")
    async def rerank() -> JSONResponse:
        return not_supported()

    @router.post("/videos")
    async def videos() -> JSONResponse:
        return not_supported()

    @router.get("/videos/models")
    async def video_models() -> JSONResponse:
        return not_supported()

    @router.get("/videos/{jobId}")
    async def video_status(jobId: str) -> JSONResponse:  # noqa: N803
        _ = jobId
        return not_supported()

    @router.get("/videos/{jobId}/content")
    async def video_content(jobId: str) -> JSONResponse:  # noqa: N803
        _ = jobId
        return not_supported()

    @router.get("/private/models/{author}/{slug}")
    async def private_model(author: str, slug: str) -> JSONResponse:
        _ = (author, slug)
        return error_response(404, "Private models are not supported", "private_models_not_supported")

    @router.get("/private/models/{author}/{slug}/endpoints")
    async def private_model_endpoints(author: str, slug: str) -> JSONResponse:
        _ = (author, slug)
        return error_response(404, "Private models are not supported", "private_models_not_supported")

    _add_guardrail_stubs(router)
    _add_current_openrouter_stubs(router)


def _add_guardrail_stubs(router: APIRouter) -> None:
    async def stub() -> JSONResponse:
        return not_supported()

    router.add_api_route("/guardrails", stub, methods=["GET"])
    router.add_api_route("/guardrails", stub, methods=["POST"])
    router.add_api_route("/guardrails/assignments/keys", stub, methods=["GET"])
    router.add_api_route("/guardrails/assignments/members", stub, methods=["GET"])
    router.add_api_route("/guardrails/{id}", stub, methods=["GET"])
    router.add_api_route("/guardrails/{id}", stub, methods=["PATCH"])
    router.add_api_route("/guardrails/{id}", stub, methods=["DELETE"])
    router.add_api_route("/guardrails/{id}/assignments/keys", stub, methods=["GET"])
    router.add_api_route("/guardrails/{id}/assignments/keys", stub, methods=["POST"])
    router.add_api_route("/guardrails/{id}/assignments/keys/remove", stub, methods=["POST"])
    router.add_api_route("/guardrails/{id}/assignments/members", stub, methods=["GET"])
    router.add_api_route("/guardrails/{id}/assignments/members", stub, methods=["POST"])
    router.add_api_route("/guardrails/{id}/assignments/members/remove", stub, methods=["POST"])


def _add_current_openrouter_stubs(router: APIRouter) -> None:
    async def stub() -> JSONResponse:
        return not_supported()

    routes = (
        ("/analytics/meta", "GET"),
        ("/analytics/query", "POST"),
        ("/benchmarks", "GET"),
        ("/byok", "GET"),
        ("/byok", "POST"),
        ("/byok/{id}", "DELETE"),
        ("/byok/{id}", "GET"),
        ("/byok/{id}", "PATCH"),
        ("/classifications/task", "GET"),
        ("/datasets/app-rankings", "GET"),
        ("/datasets/rankings-daily", "GET"),
        ("/files", "GET"),
        ("/files", "POST"),
        ("/files/{file_id}", "DELETE"),
        ("/files/{file_id}", "GET"),
        ("/files/{file_id}/content", "GET"),
        ("/generation/feedback", "POST"),
        ("/images", "POST"),
        ("/model/{author}/{slug}", "GET"),
        ("/observability/destinations", "GET"),
        ("/observability/destinations", "POST"),
        ("/observability/destinations/{id}", "DELETE"),
        ("/observability/destinations/{id}", "GET"),
        ("/observability/destinations/{id}", "PATCH"),
        ("/presets", "GET"),
        ("/presets/{slug}", "GET"),
        ("/presets/{slug}/chat/completions", "POST"),
        ("/presets/{slug}/messages", "POST"),
        ("/presets/{slug}/responses", "POST"),
        ("/presets/{slug}/versions", "GET"),
        ("/presets/{slug}/versions/{version}", "GET"),
        ("/scim/group-mappings", "GET"),
        ("/scim/group-mappings", "POST"),
        ("/scim/group-mappings/{id}", "DELETE"),
        ("/scim/group-mappings/{id}", "GET"),
        ("/scim/group-mappings/{id}", "PATCH"),
        ("/scim/groups", "GET"),
        ("/workspaces/{id}/budgets", "GET"),
        ("/workspaces/{id}/budgets/{interval}", "DELETE"),
        ("/workspaces/{id}/budgets/{interval}", "PUT"),
        ("/workspaces/{id}/members", "GET"),
    )
    for path, method in routes:
        router.add_api_route(path, stub, methods=[method])
