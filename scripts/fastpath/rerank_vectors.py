"""Separate Python authorize vectors for routes the edge must pass."""
from __future__ import annotations

import sys

import authorize_vectors as common

from trusted_router.catalog_ingest import _build_endpoints
from trusted_router.pricing import _flat_tier

RERANK_ID = "voyage/rerank-3"


def rerank_catalog_addition(model_id):
    assert model_id == RERANK_ID
    model = common.Model(
        id=model_id, name="Voyage Rerank 3", provider="voyage", upstream_id="rerank-3",
        context_length=32_000, supports_chat=False, supports_rerank=True,
        prepaid_available=True, byok_available=False,
        prompt_price_microdollars_per_million_tokens=52_750,
        completion_price_microdollars_per_million_tokens=0,
        published_prompt_price_microdollars_per_million_tokens=50_000,
        published_completion_price_microdollars_per_million_tokens=0,
        price_tiers=_flat_tier(52_750, 0), published_price_tiers=_flat_tier(50_000, 0),
    )
    return model, next(iter(_build_endpoints({model_id: model}).values()))


def cases():
    return [
        common.make_case("pass:rerank:accepted", "body.evaluator_input",
                         body={"model": RERANK_ID, "route_type": "rerank", "max_output_tokens": 1},
                         catalog=(RERANK_ID,)),
        common.make_case("pass:rerank:chat", "body.evaluator_input",
                         body={"route_type": "rerank", "max_output_tokens": 1}),
        common.make_case("pass:rerank:absent", "body.evaluator_input",
                         body={"model": "absent/model", "route_type": "rerank", "max_output_tokens": 1}),
    ]


def render():
    with common.patched(common, "cases", cases), common.patched(common, "_catalog_addition", rerank_catalog_addition):
        return common.render_cases().replace(common.GENERATOR, "scripts/fastpath/rerank_vectors.py")


if __name__ == "__main__":
    rendered = render()
    if "--print" in sys.argv:
        sys.stdout.write(rendered)
    else:
        (common.ROOT / "fastpath/testdata/authorize_rerank_v1.json").write_text(rendered)
