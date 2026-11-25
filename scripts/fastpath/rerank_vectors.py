"""Separate Python authorize vectors for routes the edge must pass."""
from __future__ import annotations

import sys

import authorize_vectors as common


def cases():
    return [
        common.make_case("pass:rerank:known", "body.evaluator_input",
                         body={"route_type": "rerank", "max_output_tokens": 1}),
        common.make_case("pass:rerank:absent", "body.evaluator_input",
                         body={"model": "voyage/rerank-2", "route_type": "rerank", "max_output_tokens": 1}),
        common.make_case("pass:absent:chat", "evaluate.refused",
                         body={"model": "voyage/rerank-2", "route_type": "chat.completions"}),
        common.make_case("pass:absent:embeddings", "evaluate.refused",
                         body={"model": "voyage/rerank-2", "route_type": "embeddings"}),
    ]


def render():
    common.cases = cases
    return common.render_cases().replace(common.GENERATOR, "scripts/fastpath/rerank_vectors.py")


if __name__ == "__main__":
    rendered = render()
    if "--print" in sys.argv:
        sys.stdout.write(rendered)
    else:
        (common.ROOT / "fastpath/testdata/authorize_rerank_v1.json").write_text(rendered)
