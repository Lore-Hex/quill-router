"""The browser pickers treat the catalog's variable-price sentinel as unknown.

trustedrouter/synth and synth-code publish OpenRouter's ``"-1"`` because their
cost depends on the models the caller configures. ``/v1/models/picker``
forwards it unchanged, so the shared normalizer (model_catalog.js) and the
chat.js fallback must not turn it into ``$-1000000.00/M``, rank it as the
cheapest model, or sum it into the input-cost preview.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.catalog import SYNTH_MODEL_ID

STATIC = Path("src/trusted_router/static")


def _node(script: str) -> object:
    node = shutil.which("node")
    assert node is not None, "Node is required to exercise the actual picker code"
    result = subprocess.run(  # noqa: S603 - local JS, no network or user input
        [node, "-e", script], check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _function_source(js: str, name: str) -> str:
    """Source of a top-level-in-IIFE `function name(...) {...}` by brace matching."""
    start = js.index(f"function {name}(")
    depth = 0
    for index in range(js.index("{", start), len(js)):
        if js[index] == "{":
            depth += 1
        elif js[index] == "}":
            depth -= 1
            if depth == 0:
                return js[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


ROWS = """[
  {id: 'trustedrouter/synth', pricing: {prompt: '-1', completion: '-1'}, trustedrouter: {}},
  {id: 'fixture/free', pricing: {prompt: '0', completion: '0'}, trustedrouter: {}},
  {id: 'fixture/paid', pricing: {prompt: '0.000001', completion: '0.000002'}, trustedrouter: {}},
  {id: 'fixture/cheap', pricing: {prompt: '0.0000001', completion: '0.0000002'}, trustedrouter: {}},
  {id: 'fixture/unpriced', trustedrouter: {}}
]"""


def test_picker_projection_forwards_the_published_sentinel(client: TestClient) -> None:
    rows = {row["id"]: row for row in client.get("/v1/models/picker").json()["data"]}
    assert rows[SYNTH_MODEL_ID]["pricing"] == {"prompt": "-1", "completion": "-1"}


def test_shared_normalizer_marks_variable_price_unknown_and_ranks_it_last() -> None:
    script = f"""
global.window = {{}};
require('./{STATIC}/model_catalog.js');
const c = window.TrustedRouterModelCatalog;
const models = {ROWS}.map(c.normalizeModel);
const byId = Object.fromEntries(models.map(m => [m.id, m]));
const synth = byId['trustedrouter/synth'];
process.stdout.write(JSON.stringify({{
  synth: [synth.input_per_m, synth.output_per_m, synth.total_per_m, synth.price_varies, synth.free],
  free: [byId['fixture/free'].total_per_m, byId['fixture/free'].free, byId['fixture/free'].price_varies],
  unpriced: [byId['fixture/unpriced'].total_per_m, byId['fixture/unpriced'].price_varies],
  sorted: models.slice().sort(c.compareByPrice).map(m => m.id),
  cheap: c.cheapestModels(models, 30).map(m => m.id),
  synthHtml: c.priceMetaHtml(synth),
  paidHtml: c.priceMetaHtml(byId['fixture/paid']),
  perM: [c.pricePerMillion('-1'), c.pricePerMillion('0'), c.pricePerMillion(null), c.pricePerMillion('abc')],
}}));
"""
    out = _node(script)
    assert out["synth"] == [None, None, None, True, False]
    assert out["free"] == [0, True, False]
    assert out["unpriced"] == [None, False]
    assert out["sorted"][:3] == ["fixture/free", "fixture/cheap", "fixture/paid"]
    assert set(out["sorted"][3:]) == {"trustedrouter/synth", "fixture/unpriced"}
    assert out["cheap"] == ["fixture/free", "fixture/cheap", "fixture/paid"]
    assert "Price varies" in out["synthHtml"] and "$" not in out["synthHtml"]
    assert out["paidHtml"] == "<span>$1.00/M in</span><span>$2.00/M out</span>"
    assert out["perM"] == [None, 0, None, None]


def test_chat_fallback_normalizer_and_cost_preview_handle_variable_price() -> None:
    js = (STATIC / "chat.js").read_text()
    sources = "\n".join(
        _function_source(js, name)
        for name in ("formatCost", "normalizeModel", "inputCostEstimate")
    )
    script = f"""
global.window = {{}};
function inferCapabilities() {{ return []; }}
{sources}
const models = {ROWS}.map(normalizeModel);
const byId = Object.fromEntries(models.map(m => [m.id, m]));
const synth = byId['trustedrouter/synth'];
process.stdout.write(JSON.stringify({{
  synth: [synth.input_per_m, synth.total_per_m, synth.price_varies, synth.free],
  paidOnly: inputCostEstimate(1000, [byId['fixture/paid']]).text,
  mixed: inputCostEstimate(1000, [byId['fixture/paid'], synth]).text,
  synthOnly: inputCostEstimate(1000, [synth]).text,
  free: inputCostEstimate(1000, [byId['fixture/free']]).text,
  notLoaded: inputCostEstimate(1000, [null]).text,
  noTokens: inputCostEstimate(0, [synth]).text,
}}));
"""
    out = _node(script)
    assert out["synth"] == [None, None, True, False]
    assert out["paidOnly"] == "≈0.10¢"
    assert out["mixed"] == "at least 0.10¢ (price varies)"
    assert out["synthOnly"] == "price varies"
    assert out["free"] == ""
    assert out["notLoaded"] == ""
    assert out["noTokens"] == ""


def test_picker_cheap_filters_use_the_price_aware_ranking() -> None:
    # No picker may sort by `total_per_m || 0`, which ranks an unknown price
    # as $0 (cheapest).
    for name in ("model_catalog.js", "chat.js", "fusion.js"):
        source = (STATIC / name).read_text()
        assert not re.search(r"total_per_m \|\| 0", source), name
