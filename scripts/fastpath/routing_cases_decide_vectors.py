"""Hosted decide vectors over a separate synthetic snapshot; no live inputs.

Run as a script in its own process, like routing_cases_vectors.py. The real
candidate pipeline and the gateway's actual prepare_model function supply all
answers, including its pre-pipeline spelling refusal.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from typing import Any

import routing_cases_vectors as common

ROOT = common.ROOT
OUTPUT = ROOT / "fastpath/testdata/routing_cases_decide_v1.json"
SNAPSHOT = "fastpath/testdata/routing_snapshot_decide_v3.body.json"
GENERATOR = "scripts/fastpath/routing_cases_decide_vectors.py"
fixture = common._snapshot_vectors()
SINGLE, MULTI, BYOK = fixture.DECIDE_SINGLE, fixture.DECIDE_MULTI, fixture.DECIDE_BYOK


def prepare_model(body: common.GatewayAuthorizeRequest) -> None:
    """Execute the nested gateway guard without running authentication or storage.

    Read its AST rather than duplicate its conditions or error messages. All
    referenced module globals are the gateway's; only its two closure inputs
    come from this fixture request. Fail if the function is renamed or moved.
    """
    from trusted_router.routes.internal import gateway

    source = ROOT / "src/trusted_router/routes/internal/gateway.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    guards = [node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and node.name == "prepare_model"]
    assert len(guards) == 1
    module = ast.Module(body=guards, type_ignores=[])
    namespace = {**vars(gateway), "body": body, "requested_model_id": body.model}
    exec(compile(module, str(source), "exec"), namespace)  # noqa: S102 - repository function
    namespace["prepare_model"]()


def cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, fields: dict[str, Any] | None = None, *,
            model: str = MULTI, byok: tuple[str, ...] = ()) -> None:
        request = {"model": model, "route_type": "decide", "estimated_input_tokens": 1234,
                   "stream": False, **({"region": "us-central1"} if model == SINGLE else {}),
                   **(fields or {})}
        request["output_tokens"] = next(
            (request[key] for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
             if request.get(key) is not None), 512,
        )
        out.append({"name": name, "request": request, "at": common.INSTANTS["peak"],
                    "byok_providers": list(byok)})

    add("single", model=SINGLE)
    add("multi")
    add("empty_models", {"models": []})
    for name, provider in (
        ("order", {"order": ["together", "fireworks"]}),
        ("only", {"only": ["together"]}),
        ("ignore", {"ignore": ["fireworks"]}),
        ("ignore_all", {"ignore": ["fireworks", "together"]}),
        ("no_fallbacks", {"allow_fallbacks": False}),
        ("order_no_fallbacks", {"order": ["together"], "allow_fallbacks": False}),
        ("disjoint_order", {"order": ["together"], "only": ["fireworks"], "allow_fallbacks": False}),
        ("deny", {"data_collection": "deny"}),
        ("zdr", {"zdr": True}),
        ("zdr_all_removed", {"only": ["fireworks"], "zdr": True}),
        ("privacy", {"min_privacy": "zdr"}),
        ("privacy_all_removed", {"min_privacy": "trusted"}),
        ("deny_all_removed", {"data_collection": "deny", "min_privacy": "no_store", "only": ["fireworks"]}),
        ("deny_soft", {"data_collection": "deny", "only": ["fireworks"]}),
        ("max_price", {"max_price": {"prompt": 1, "completion": 0}}),
        ("max_price_all_removed", {"max_price": {"prompt": 0}}),
        ("performance_latency", {"preferred_max_latency": 2}),
        ("performance_throughput", {"preferred_min_throughput": 10}),
        ("unknown_option", {"fixture_option": True}),
        ("unknown_provider", {"only": ["fixture-provider"]}),
        ("invalid_privacy", {"min_privacy": "secret"}),
        ("invalid_parameters", {"require_parameters": "yes"}),
    ):
        add(name, {"provider": provider})
    for sort in ("price", "latency", "throughput"):
        add("sort_" + sort, {"provider": {"sort": sort}})
        add("sort_global_" + sort, {"provider": {"sort": {"by": sort, "partition": "none"}}})
    add("top_level_no_fallbacks", {"allow_fallbacks": False})
    # Only Together declares tools. Decide does not derive capability
    # parameters, and require_parameters still gives the fixed refusal.
    add("parameters_fixed_error", {"provider": {"only": ["fireworks"], "require_parameters": True},
                                   "requested_parameters": ["tools"]})
    add("parameters", {"provider": {"require_parameters": True}, "requested_parameters": ["tools"]})
    add("parameters_not_described", {"requested_parameters": ["tools"]})
    add("modality_empty", {"input_modalities": ["image"]})
    for region in common.REGIONS:
        add("region:" + region, {"region": region}, model=SINGLE)
    add("region_missing", {"region": None}, model=SINGLE)
    add("region_unknown", {"region": "fixture-region"}, model=SINGLE)
    add("byok_absent", model=BYOK)
    add("byok_present", model=BYOK, byok=("google-ai-studio",))
    add("byok_only_absent", {"provider": {"usage": "byok"}}, model=BYOK)
    add("byok_only_present", {"provider": {"usage": "byok"}}, model=BYOK, byok=("google-ai-studio",))
    add("byok_before_truncation", {"provider": {"order": ["google-ai-studio", "together"],
                                              "allow_fallbacks": False}}, model=BYOK)
    for tier in ("default", "auto", "priority", " DEFAULT ", "flex"):
        add("tier:" + tier, {"service_tier": tier})
    add("priority_large", {"service_tier": "priority", "estimated_input_tokens": 272001})
    for tokens in (0, 1, 1234, 10_000_000):
        add(f"input:{tokens}", {"estimated_input_tokens": tokens}, model=SINGLE)
    add("output_one", {"max_output_tokens": 1})
    add("output_hold", {"max_output_tokens": 20000})
    add("over_context", {"estimated_input_tokens": 17, "max_tokens": 20000})
    add("stream", {"stream": True})
    for route in ("chat.completions", "embeddings", None):
        add("route:" + str(route), {"route_type": route})
    add("route_absent")
    del out[-1]["request"]["route_type"]
    add("variant", model=MULTI + ":nitro")
    add("dated", model=MULTI + "-2026-10-01")
    add("spaced_id", model=" " + MULTI + " ")
    add("non_ascii", model="\t" + MULTI)
    add("models_decision", {"models": [SINGLE]})
    add("models_chat", {"models": [common.GLM]})
    add("models_no_fallbacks", {"models": [common.GLM], "allow_fallbacks": False})
    add("native_chat_decide", model=common.FLASH)
    add("embeddings_only", model=fixture.EMBEDDING_ONLY)
    # A model that is both a chat model and a decision model keeps the chat
    # arm, context window included, whatever the route.
    add("chat_and_decision", model=fixture.DECIDE_CHAT)
    add("chat_and_decision_fits", {"estimated_input_tokens": 4, "max_output_tokens": 1},
        model=fixture.DECIDE_CHAT)
    add("chat_and_decision_chat_route", {"route_type": "chat.completions", "estimated_input_tokens": 4,
                                         "max_output_tokens": 1}, model=fixture.DECIDE_CHAT)
    # A decision-only model named as a chat model's alternative is not a
    # hosted decision: the request's own arm is the chat arm.
    add("chat_then_decision", {"models": [MULTI]}, model=common.FLASH)
    add("chat_then_decision_chat_route", {"models": [MULTI], "route_type": "chat.completions"},
        model=common.FLASH)
    return out


def render() -> str:
    lines = [json.dumps({**case, "expect": common.evaluate(
        case, hosted_decide=True, prepare_model=prepare_model,
    )}, sort_keys=True, separators=(",", ":")) for case in cases()]
    header = {
        "generator": GENERATOR, "snapshot": SNAPSHOT,
        "snapshot_sha256": hashlib.sha256((ROOT / SNAPSHOT).read_bytes()).hexdigest(),
        "settings": {"regions": list(common.REGIONS), "primary_region": common.PRIMARY_REGION},
        "stage_d_inputs": common.STAGE_D_INPUTS, "rerank": None,
    }
    return json.dumps(header, sort_keys=True, separators=(",", ":"))[:-1] + ',\n "cases": [\n  ' + ",\n  ".join(lines) + "\n ]}\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
