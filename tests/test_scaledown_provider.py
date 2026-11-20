"""ScaleDown task contracts: exact input billing, native canaries, no guessed APIs."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import scaledown
from scripts.pricing.refresh import PROVIDER_SLUGS
from tests.pinned_manifests import serve_manifest_rows
from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, PROVIDERS
from trusted_router.catalog_ingest import _supplemental_provider_models_and_endpoints
from trusted_router.money import token_cost_microdollars
from trusted_router.pricing import _customer_price, provider_manifest_price_profile_is_valid
from trusted_router.provider_contracts import provider_model_requires_exact_global_settlement
from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS
from trusted_router.services.inference_errors import default_provider_secret_ref
from trusted_router.storage import STORE

PRICE = "## Pricing\nPublic API: $0.05 per 1M tokens\n- All four models included\n- Flat pricing, no tiers\n"
FAQ = "ScaleDown does not charge for output tokens. We exclusively charge for input tokens"
RESULTS = {
    "compress": {
        "input_tokens": 30,
        "successful": True,
        "results": {"success": True, "compressed_prompt": "Friday"},
    },
    "summarize": {"input_tokens": 83, "summary": "Acme will review feedback Friday."},
    "extract": {"input_tokens": 170, "entities": []},
    "classify": {"input_tokens": 394, "top_label": "billing"},
}


def _discovered_task_rows(monkeypatch, directory: Path) -> list[dict]:
    """ScaleDown's task rows as its hourly discovery writes them once every
    native canary passes (test_native_canaries_and_manifest checks the same
    pipeline)."""
    path = directory / "discovered" / "scaledown.json"
    path.parent.mkdir(parents=True)
    real_client = httpx.Client

    def handle(req):
        task = next(task for task, spec in scaledown.TASKS.items() if spec["path"] == req.url.path)
        return httpx.Response(200, json=RESULTS[task])

    with monkeypatch.context() as patch:
        patch.setenv("SCALEDOWN_API_KEY", "test-only")
        patch.setattr(scaledown, "fetch_html", lambda url: PRICE)
        patch.setattr(scaledown, "_website_code", lambda: FAQ)
        patch.setattr(scaledown, "MANIFEST_PATH", path)
        patch.setattr(scaledown, "_ROWS", scaledown._ROWS)
        patch.setattr(
            scaledown.httpx,
            "Client",
            lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
        )
        scaledown.write_provider_manifest(scaledown.fetch())
    return json.loads(path.read_text())["models"]


@pytest.fixture
def task_routes(monkeypatch, tmp_path) -> None:
    """ScaleDown's four task routes, built from the rows its discovery writes:
    the task contracts below hold whatever ScaleDown lists today."""
    serve_manifest_rows(
        monkeypatch, tmp_path, "scaledown", _discovered_task_rows(monkeypatch, tmp_path)
    )


def test_exact_input_only_price():
    assert scaledown._input_price(PRICE, FAQ) == ModelPrice(50_000, 0)
    assert scaledown._input_price(PRICE.replace("0.05", "0.055"), FAQ) == ModelPrice(55_000, 0)


@pytest.mark.parametrize(
    "source,faq",
    [
        ("", FAQ),
        (PRICE, ""),
        (PRICE.replace("Flat pricing, no tiers", "Tiered"), FAQ),
        (PRICE.replace("0.05", "0.0000001"), FAQ),
        (PRICE.replace("0.05", "NaN"), FAQ),
        (PRICE.replace("0.05", "0"), FAQ),
        (PRICE + "Public API: $0.10 per 1M tokens\n", FAQ),
    ],
)
def test_ambiguous_or_changed_billing_contract_fails_closed(source, faq):
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, faq)


FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
LLMS_2026_11_19 = (FIXTURES / "scaledown_llms_2026-11-19.txt").read_text(encoding="utf-8")
SITE_2026_11_19 = (FIXTURES / "scaledown_site_bundle_2026-11-19.js").read_text(encoding="utf-8")


def test_live_2026_11_19_per_modality_pricing_bills_the_text_rate():
    # Refresh run 44601888558 held ScaleDown on "missing or ambiguous flat
    # task pricing": llms.txt replaced "Public API: $0.05 per 1M tokens" and
    # "All four models included" with per-modality Text and Image input
    # rates. The routes are text only. Four-model coverage now comes from the
    # Public API block's context-window line, which names all four models.
    assert scaledown._input_price(LLMS_2026_11_19, SITE_2026_11_19) == ModelPrice(50_000, 0)
    cheaper_image = LLMS_2026_11_19.replace("- Image: $0.15", "- Image: $0.01")
    assert scaledown._input_price(cheaper_image, SITE_2026_11_19) == ModelPrice(50_000, 0)
    # The Image rate is validated, and must appear exactly once in llms.txt.
    with pytest.raises(RuntimeError, match="an Image rate must appear exactly once"):
        scaledown._input_price(LLMS_2026_11_19.replace("- Image: $0.15\n", ""), SITE_2026_11_19)
    # The bundle is read only for the output-is-free FAQ, never for coverage.
    assert scaledown._input_price(LLMS_2026_11_19, FAQ) == ModelPrice(50_000, 0)


ALL_FOUR = "- 1M-token context window on Summarize, Extract, and Classify; Compress has no limit"


@pytest.mark.parametrize(
    "old,new,site_old,site_new",
    [
        (ALL_FOUR + "\n", "", "", ""),
        (ALL_FOUR, ALL_FOUR.replace("Summarize, Extract, and Classify", "Summarize and Extract"), "", ""),
        ("", "", "ScaleDown does not charge for output tokens", "Output is billed"),
        ("- Text: $0.05\n", "", "", ""),
        ("- Text: $0.05", "- Text: $0.05\n- Text: $0.10", "", ""),
        ("- Image: $0.15", "- Image: $0.15\n- Image: $0.20", "", ""),
        ("- Text: $0.05", "- Text: $0.05\n- Text (Summarize): $0.10", "", ""),
        ("- Image: $0.15", "- Audio: $0.15", "", ""),
        ("- Text: $0.05", "- Text: from $0.05", "", ""),
        ("- Text: $0.05", "- Text: $0", "", ""),
        ("- Text: $0.05", "- Text: $0.0000001", "", ""),
        ("per 1M input tokens:", "per 1K input tokens:", "", ""),
        ("Public API, per", "Public API, per 1M input tokens:\nPublic API, per", "", ""),
        ("- Text: $0.05", "- Text: $0.05\nPublic API: $0.05 per 1M tokens", "", ""),
        ("Flat pricing, no tiers or seats", "Volume tiers", "", ""),
    ],
)
def test_per_modality_pricing_changes_fail_closed(old, new, site_old, site_new):
    source = LLMS_2026_11_19.replace(old, new) if old else LLMS_2026_11_19
    site = SITE_2026_11_19.replace(site_old, site_new) if site_old else SITE_2026_11_19
    assert source != LLMS_2026_11_19 or site != SITE_2026_11_19
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, site)


ENTERPRISE = "Enterprise: dedicated or licensed deployments, custom pricing\n"


@pytest.mark.parametrize(
    "source",
    [
        # A rate under the Enterprise heading is not the Public API rate.
        LLMS_2026_11_19.replace("- Text: $0.05\n", "").replace(ENTERPRISE, ENTERPRISE + "- Text: $0.01\n"),
        LLMS_2026_11_19.replace(ENTERPRISE, ENTERPRISE + "- Text: $0.01\n"),
        # A heading inside the block ends it; its rows are per 1K, not per 1M.
        LLMS_2026_11_19.replace("- Text: $0.05", "### Batch API, per 1K input tokens:\n- Text: $0.05"),
        LLMS_2026_11_19.replace("- Image: $0.15\n", "- Image: $0.15\n\n- Text: $0.01\n"),
    ],
    ids=["text-moved-to-enterprise", "second-text-under-enterprise", "batch-heading-in-block", "after-blank-line"],
)
def test_rates_are_bound_to_the_public_api_block(source):
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, SITE_2026_11_19)


@pytest.mark.parametrize(
    "bullet",
    [
        "- Summarize: USD 0.20 per 1M input tokens",
        "- Audio: USD 0.15 per 1M input tokens",
        "- Batch: 50% off",
        "- Classify only",
    ],
)
def test_every_public_api_bullet_must_be_a_known_line(bullet):
    source = LLMS_2026_11_19.replace("- Image: $0.15", "- Image: $0.15\n" + bullet)
    with pytest.raises(RuntimeError, match="unknown or repeated line in the Pricing section"):
        scaledown._input_price(source, SITE_2026_11_19)


# Round-2 review: the self-serve card span could borrow the Enterprise card's
# "All four models included" when the CTA moved after it.
ENTERPRISE_BORROWED_CARD = SITE_2026_11_19.replace(
    '["All four models included"', '["Three models included"'
).replace(
    '["Dedicated instance"', '["All four models included","Dedicated instance"'
).replace('cta:"get_api_key"', 'cta:"none"').replace(
    '"Dedicated support and SLAs"]', '"Dedicated support and SLAs"],cta:"get_api_key"'
)


@pytest.mark.parametrize(
    "site",
    [
        ENTERPRISE_BORROWED_CARD,
        FAQ + '{plan:"Public API",features:["Three models included"]},'
        '{plan:"Enterprise",features:["All four models included"]}',
        SITE_2026_11_19,
    ],
    ids=["enterprise-borrowed-card", "coverage-on-other-plan", "live-bundle"],
)
@pytest.mark.parametrize(
    "source",
    [
        LLMS_2026_11_19.replace(ALL_FOUR + "\n", ""),
        LLMS_2026_11_19.replace(ALL_FOUR + "\n", "").replace(ENTERPRISE, ENTERPRISE + ALL_FOUR + "\n"),
        LLMS_2026_11_19.replace(ALL_FOUR, "- " + "All four models included"),
    ],
    ids=["no-all-four-line", "all-four-line-under-enterprise", "card-phrase-instead"],
)
def test_four_model_coverage_comes_only_from_the_public_api_block(source, site):
    assert "All four models included" in site
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, site)


@pytest.mark.parametrize(
    "source",
    [
        LLMS_2026_11_19.replace("- Image: $0.15", "- Image: $0.15\n- Summarize: 20¢ / 1M input tokens"),
        LLMS_2026_11_19.replace(ENTERPRISE, ENTERPRISE + "- Summarize: 20¢ / 1M input tokens\n"),
        LLMS_2026_11_19.replace(ENTERPRISE, ENTERPRISE + "- Summarize: INR 2 / 1M input tokens\n"),
        LLMS_2026_11_19.replace(ENTERPRISE, ENTERPRISE + "- Batch: 0.03 / 1K calls\n"),
        LLMS_2026_11_19.replace(ENTERPRISE, ENTERPRISE + "Summarize is billed at 0.04 per 1,000 input tokens\n"),
        LLMS_2026_11_19.replace("- Text: $0.05\n", "").replace("- Image: $0.15", "- Image: $0.15\n  - Text: $0.01"),
        LLMS_2026_11_19.replace("- Image: $0.15", "- Image: $0.15\n  - Text: $0.05"),
        LLMS_2026_11_19.replace("- Text: $0.05", " - Text: $0.05"),
        LLMS_2026_11_19.replace("- Fine-tuning on your data", "- Fine-tuning on your data\n- Classify: $0.01"),
        LLMS_2026_11_19.replace("Enterprise: dedicated", "Enterprise: per-seat, dedicated"),
    ],
    ids=["cent-sign-in-block", "cent-sign-under-enterprise", "inr", "slash-1k", "per-1000-prose",
         "nested-text-replaces-rate", "nested-text-duplicate", "indented-text", "enterprise-tariff",
         "changed-enterprise-heading"],
)
def test_every_pricing_line_must_be_an_exact_known_line(source):
    assert source != LLMS_2026_11_19
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, SITE_2026_11_19)


SECOND_PRICING = (
    "\n## Pricing\n\nPublic API, per 1M input tokens:\n- Text: $0.01\n- Flat pricing, no tiers or seats\n"
)


@pytest.mark.parametrize("first_cheaper", [False, True])
def test_two_pricing_sections_fail_closed(first_cheaper):
    source = SECOND_PRICING + LLMS_2026_11_19 if first_cheaper else LLMS_2026_11_19 + SECOND_PRICING
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(source, SITE_2026_11_19)
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(PRICE + PRICE.replace("0.05", "0.01"), FAQ)


@pytest.mark.parametrize(
    "heading",
    ["## Pricing ##", "## Pricing #", "# Pricing", "### PRICING", "##   pricing   ##", "Pricing\n=======",
     "Pricing\n-------"],
)
def test_any_second_pricing_heading_fails_closed_in_both_layouts(heading):
    new = LLMS_2026_11_19 + "\n" + heading + "\n\nPublic API: $0.01 per 1M tokens\n"
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(new, SITE_2026_11_19)
    legacy = PRICE + "\n" + heading + "\nPublic API: $0.01 per 1M tokens\n"
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(legacy, FAQ)


NEXT_SECTION = "## Compliance and Security"


@pytest.mark.parametrize(
    "inserted",
    [
        # Round-3 review: read as a setext heading, this ended Pricing early
        # and the conflicting rate was never validated.
        "> Summarize: $0.20 per 1M input tokens\n---",
        "> Summarize: $0.20 per 1M input tokens\n===",
        "Summarize: $0.20 per 1M input tokens\n---",
        "> - Text: $0.01",
        "> Enterprise pricing is custom",
        "---",
        "=====",
        "### Batch API, per 1K input tokens:\n- Text: $0.01",
    ],
    ids=["blockquote-setext-dash", "blockquote-setext-equals", "plain-setext", "blockquoted-text-rate",
         "blockquote-prose", "lone-dash-underline", "lone-equals-underline", "deeper-heading"],
)
def test_only_atx_headings_end_pricing_and_containers_fail_closed(inserted):
    assert LLMS_2026_11_19.count(NEXT_SECTION) == 1
    for anchor in (NEXT_SECTION, ENTERPRISE):
        source = LLMS_2026_11_19.replace(anchor, inserted + "\n\n" + anchor, 1)
        with pytest.raises(RuntimeError):
            scaledown._input_price(source, SITE_2026_11_19)


@pytest.mark.parametrize(
    "heading",
    [
        "## **Pricing**",
        "## [Pricing](#pricing)",
        "## Pricing details",
        "### Enterprise pricing",
        "#Pricing",
        "> ## Pricing",
        "**Pricing**\n---",
        "*Pricing*\n=====",
        "[Pricing](#pricing)\n-------",
        "P-r-i-c-i-n-g\n---",
    ],
)
def test_formatted_pricing_headings_count_as_duplicates_in_both_layouts(heading):
    new = LLMS_2026_11_19 + "\n" + heading + "\n\nPublic API, per 1M input tokens:\n- Text: $0.01\n"
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(new, SITE_2026_11_19)
    legacy = PRICE + "\n" + heading + "\nPublic API: $0.01 per 1M tokens\n"
    with pytest.raises(RuntimeError, match="ambiguous public pricing sections"):
        scaledown._input_price(legacy, FAQ)


# Verbatim lines 24, 27, 69 and 136 of the live llms.txt (2026-11-19):
# "token" without currency, and currency or "per" without a per-token unit.
# They must not count as rates.
LIVE_MARKETING = (
    "## Models\n\nSend in a long prompt or document. Get back a semantically compressed version that "
    "preserves every fact relevant to your query, at a fraction of the token count.\n\n"
    "Real impact: A workflow costing $10/day drops to $4/day at 60% compression\n\n"
    "## Benchmarks\n\nScaleDown is benchmarked against frontier models from OpenAI, Anthropic, Google, "
    "xAI, DeepSeek, and Qwen on public benchmarks: FinanceBench (Compress, F1), QMSum (Summarize, "
    "ROUGE-L), CUAD (Extract, F1), and CLINC OOS (Classify, accuracy). Cost is per 1,000 calls; "
    "latency is mean response time per call.\n\n"
)
LIVE_BLOG_LINE = (
    "  Classify vs. GPT-5.4 Mini/Nano on intent classification for LLM routing: 90.53% accuracy "
    "at $0.0008, beating both baselines on accuracy and cost.\n"
)
LIVE_WITH_MARKETING = LIVE_MARKETING + LLMS_2026_11_19 + "\n## Blog\n\n" + LIVE_BLOG_LINE


def test_live_marketing_lines_still_price():
    assert "$10/day" in LIVE_WITH_MARKETING and "$0.0008" in LIVE_WITH_MARKETING
    assert scaledown._input_price(LIVE_WITH_MARKETING, SITE_2026_11_19) == ModelPrice(50_000, 0)


# Round-4 review: heading forms the section scanner does not recognise.
HIDDEN_HEADINGS = ["Pricing\nfor Public API\n---", "> Pricing\n> ---", "- ## Pricing", "## Pr&#105;cing"]


@pytest.mark.parametrize("heading", HIDDEN_HEADINGS)
@pytest.mark.parametrize(
    "block",
    [
        "Public API, per 1M input tokens:\n- Text: $0.20\n- Flat pricing, no tiers or seats\n",
        "- Text: $0.20\n",
        "> Public API, per 1M input tokens:\n> - Text: $0.20\n",
        "&#45; Text: $0.20\n",
        "Public API: $0.20 per 1M tokens\n",
    ],
    ids=["full-block", "text-only", "blockquoted-block", "entity-encoded-text", "legacy-rate-line"],
)
def test_second_price_block_behind_hidden_heading_fails_closed(heading, block):
    for source in (LIVE_WITH_MARKETING + "\n" + heading + "\n\n" + block,
                   heading + "\n\n" + block + "\n" + LIVE_WITH_MARKETING):
        with pytest.raises(RuntimeError):
            scaledown._input_price(source, SITE_2026_11_19)


@pytest.mark.parametrize("heading", HIDDEN_HEADINGS)
@pytest.mark.parametrize(
    "block", ["Public API: $0.20 per 1M tokens\n", "> Public API: $0.20 per 1M tokens\n", "- Text: $0.20 per 1M tokens\n"]
)
def test_second_price_block_behind_hidden_heading_fails_closed_in_legacy_layout(heading, block):
    with pytest.raises(RuntimeError):
        scaledown._input_price(PRICE + "\n" + heading + "\n" + block, FAQ)


@pytest.mark.parametrize(
    "line",
    [
        "Batch API: $0.01 per 1K input tokens",
        "> Summarize costs $0.01 per 1K input tokens",
        "Summarize is $0.04 per 1,000.",
        "Classify: 0.01/1M",
        "Cheaper per million input tokens than any frontier model",
        "Extract: 2 &#162; per 1,000 input tokens",
    ],
)
def test_any_other_per_token_unit_in_the_document_fails_closed(line):
    for at in ("start", "end"):
        source = line + "\n\n" + LIVE_WITH_MARKETING if at == "start" else LIVE_WITH_MARKETING + "\n" + line + "\n"
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, SITE_2026_11_19)
    with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
        scaledown._input_price(line + "\n\n" + PRICE, FAQ)


# Round-5 review: a tariff wrapped across physical lines.
WRAPPED_TARIFFS = [
    "Summarize costs $0.20 per\n1M input tokens.",
    "Batch is $0.01 per\n1K tokens",
    "> Summarize costs $0.20 per\n> 1M input tokens.",
    "- Summarize costs $0.20\n  per 1M\n  input tokens",
]


@pytest.mark.parametrize("wrapped", WRAPPED_TARIFFS)
def test_tariff_wrapped_across_lines_fails_closed_in_both_layouts(wrapped):
    for source in (LIVE_WITH_MARKETING + "\n" + wrapped + "\n", wrapped + "\n\n" + LIVE_WITH_MARKETING):
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, SITE_2026_11_19)
    for source in (PRICE + "\n" + wrapped + "\n", PRICE + wrapped + "\n"):
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, FAQ)


@pytest.mark.parametrize("line", ["- **Text:** $0.20", "- **Image:** $0.30", "* `Text:` $0.20", "- _Image:_ $0.30"])
def test_emphasised_rate_labels_count_in_both_layouts(line):
    for source in (LIVE_WITH_MARKETING + "\n" + line + "\n", line + "\n\n" + LIVE_WITH_MARKETING):
        with pytest.raises(RuntimeError, match="must appear exactly once"):
            scaledown._input_price(source, SITE_2026_11_19)
    with pytest.raises(RuntimeError, match="must not appear in the legacy layout"):
        scaledown._input_price(PRICE + "\n" + line + "\n", FAQ)


def test_per_1k_calls_shorthand_holds_for_review_in_both_layouts():
    # Accepted trade-off (round 6): any K, M, thousand, million or billion
    # quantity after "per" is a unit, whatever word follows, so this harmless
    # shorthand edit of live line 69 holds ScaleDown for review. Excluding
    # "calls" or "users" let "per 1M user supplied input tokens" through.
    shorthand = "Cost is per 1K calls; latency is mean response time per call."
    edited = LIVE_WITH_MARKETING.replace("Cost is per 1,000 calls; latency is mean response time per call.", shorthand)
    assert shorthand in edited
    with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
        scaledown._input_price(edited, SITE_2026_11_19)
    with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
        scaledown._input_price(shorthand + "\n\n" + PRICE, FAQ)
    # The bare-number form on the live page is not a unit and still prices.
    assert scaledown._input_price(LIVE_WITH_MARKETING, SITE_2026_11_19) == ModelPrice(50_000, 0)
    for unit in ("per 1K input tokens", "per 1M", "per million tokens", "per 1M image tokens"):
        assert scaledown._PER_TOKEN_UNIT.search(unit), unit


@pytest.mark.parametrize(
    "line",
    [
        "Summarize: $0.20 per 1M user supplied input tokens",
        "Extract: $0.20 per one million image input tokens",
        "Classify: $0.20 per **1M** input tokens",
        "Compress: $0.20 per a million tokens",
        "Summarize: $0.20 per `1K` calls",
        "Summarize: $0.20 per _1M_ input tokens",
        "Summarize: $0.20 per 1K\ncalls",
    ],
    ids=["user-supplied", "one-million-image", "bold-quantity", "a-million", "code-quantity", "underscore-quantity",
         "per-1k-wrapped-calls"],
)
def test_any_k_m_quantity_is_a_unit_whatever_follows_in_both_layouts(line):
    for source in (LIVE_WITH_MARKETING + "\n" + line + "\n", line + "\n\n" + LIVE_WITH_MARKETING):
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, SITE_2026_11_19)
    for source in (line + "\n\n" + PRICE, PRICE + "\n" + line + "\n"):
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, FAQ)


# Round-7 review: priced token text the unit grammar does not describe.
PRICED_TOKEN_TEXT = {
    "five-modifiers": "Summarize: $0.20 per 1,000 billable uncached text input tokens.",
    "four-modifiers": "Extract: $0.20 per 1,000 billable cached batch text tokens",
    "link-wrapped-line": "[Summarize: $0.20 per 1,000 billable uncached text input tokens](https://scaledown.ai/)",
    "strong-wrapped": "<strong>Summarize: $0.20</strong> per 1,000 billable uncached text <em>input tokens</em>",
    "no-per": "Summarize: USD 0.20 for every 1,000 billable tokens",
    "wrapped-lines": "Summarize: $0.20 for every 1,000 billable\nuncached text input tokens.",
    "entity-dollar": "Summarize: &#36;0.20 per 1,000 billable uncached text input tokens.",
    # Round-8 review: a tag between words, a unit in the heading, and
    # currency words without a numeral.
    "br-tag": "Summarize: $0.20 per 1,000 billable uncached text input<br>tokens.",
    "br-self-closing": "Summarize: $0.20 for every 1,000 input<br/>tokens",
    "heading-unit": "## Cost for 1M input tokens\n\n- Summarize: $0.20",
    "setext-heading-unit": "Cost for 1M input tokens\n---\n\n- Summarize: $0.20",
    "twenty-cents": "Summarize costs twenty cents for every million input tokens",
    "two-dollars": "Two dollars buys a million tokens of Extract",
    "one-euro": "One euro for every million input tokens",
}
# Caught first by the per-token unit rule once links and tags are flattened
# (the wrapped line ends in "per 1,000", a bare number ending a phrase).
UNIT_TEXT = {
    "link-unit": "Summarize: $0.20 per [1M input tokens](https://scaledown.ai/#pricing)",
    "strong-unit": "Summarize: $0.20 per <strong>1M</strong> input tokens",
    "dollars-words": "Classify costs 2 dollars per million tokens",
    "wrapped-per-1000": "Summarize: $0.20 per 1,000\nbillable uncached text input tokens.",
}


def _both_layout_sources(text: str) -> list[tuple[str, str]]:
    return [
        (LIVE_WITH_MARKETING + "\n" + text + "\n", SITE_2026_11_19),
        (text + "\n\n" + LIVE_WITH_MARKETING, SITE_2026_11_19),
        (text + "\n\n" + PRICE, FAQ),
        (PRICE + "\n" + text + "\n", FAQ),
    ]


@pytest.mark.parametrize("text", list(PRICED_TOKEN_TEXT.values()), ids=list(PRICED_TOKEN_TEXT))
def test_priced_token_text_outside_pricing_fails_closed_in_both_layouts(text):
    for source, site in _both_layout_sources(text):
        with pytest.raises(RuntimeError, match=r"^scaledown: priced token text outside the Pricing section; review$"):
            scaledown._input_price(source, site)


@pytest.mark.parametrize("text", list(UNIT_TEXT.values()), ids=list(UNIT_TEXT))
def test_units_inside_links_or_html_fail_closed_in_both_layouts(text):
    for source, site in _both_layout_sources(text):
        with pytest.raises(RuntimeError, match="per-token unit outside the single public rate line"):
            scaledown._input_price(source, site)


def test_token_text_without_currency_and_percent_still_prices():
    # "percent" is not "cents", and a heading without currency or tokens does
    # not make a paragraph priced token text.
    source = LIVE_WITH_MARKETING + "\n## Savings\n\nCompression saves 60 percent of input tokens.\n"
    assert scaledown._input_price(source, SITE_2026_11_19) == ModelPrice(50_000, 0)


def test_encoded_line_break_fails_closed():
    source = LLMS_2026_11_19.replace("- Text: $0.05", "- Text: $0.05&#10;- Text: $0.20")
    with pytest.raises(RuntimeError):
        scaledown._input_price(source, SITE_2026_11_19)
    with pytest.raises(RuntimeError, match="encoded line break"):
        scaledown._input_price(LIVE_WITH_MARKETING + "\nSummarize&#10;costs more\n", SITE_2026_11_19)


def test_live_pricing_section_has_one_pricing_heading_and_ends_at_the_next_section():
    lines = LLMS_2026_11_19.splitlines()
    assert scaledown._pricing_headings(lines) == [0]
    section = scaledown._pricing_section(LLMS_2026_11_19)
    assert section[-1] == "" and NEXT_SECTION not in section
    assert scaledown._input_price(LLMS_2026_11_19, SITE_2026_11_19) == ModelPrice(50_000, 0)


@pytest.mark.parametrize("bad", [None, 0, -1, True, 1.5, "30", 1 << 40])
def test_bad_usage_is_not_a_healthy_canary(bad):
    for task, result in RESULTS.items():
        assert not scaledown._valid_result(task, {**result, "input_tokens": bad})


def test_native_canaries_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALEDOWN_API_KEY", "test-only")
    monkeypatch.setattr(scaledown, "fetch_html", lambda url: PRICE)
    monkeypatch.setattr(scaledown, "_website_code", lambda: FAQ)
    monkeypatch.setattr(scaledown, "MANIFEST_PATH", tmp_path / "scaledown.json")
    real_client = httpx.Client
    requests = []

    def handle(req):
        assert req.url.host == "api.scaledown.xyz"
        assert req.headers["x-api-key"] == "test-only"
        assert "authorization" not in req.headers
        task = next(task for task, spec in scaledown.TASKS.items() if spec["path"] == req.url.path)
        assert json.loads(req.content) == scaledown.TASKS[task]["input"]
        requests.append(task)
        return httpx.Response(200, json=RESULTS[task])

    monkeypatch.setattr(
        scaledown.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
    )
    result = scaledown.fetch()
    scaledown.write_provider_manifest(result)
    manifest = json.loads(scaledown.MANIFEST_PATH.read_text())
    assert len(requests) == 4 and len(manifest["models"]) == 4
    for row in manifest["models"]:
        assert row["output_token_price_per_m"] == 0
        assert row["input_token_price_per_m"] == 50_000
        assert row["routable"] is True
        assert row["documentation"]["example_input"]
        assert provider_manifest_price_profile_is_valid(row)


def test_failed_canary_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setenv("SCALEDOWN_API_KEY", "test-only")
    monkeypatch.setattr(scaledown, "fetch_html", lambda url: PRICE)
    monkeypatch.setattr(scaledown, "_website_code", lambda: FAQ)
    monkeypatch.setattr(scaledown, "MANIFEST_PATH", tmp_path / "scaledown.json")
    real_client = httpx.Client
    monkeypatch.setattr(
        scaledown.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(403, json={"error": "not authorized"})
            ),
            **kw,
        ),
    )
    scaledown.write_provider_manifest(scaledown.fetch())
    assert all(
        r["routable"] is False for r in json.loads(scaledown.MANIFEST_PATH.read_text())["models"]
    )


@pytest.mark.usefixtures("task_routes")
def test_task_health_probes_use_catalog_examples_and_skip_throughput():
    from trusted_router.synthetic.probes import _rotation_prompt, rotation_candidates
    from trusted_router.synthetic.throughput import throughput_candidates

    pool = rotation_candidates()
    assert set(pool["scaledown"]) == {f"scaledown/{task}" for task in scaledown.TASKS}
    for task, spec in scaledown.TASKS.items():
        assert json.loads(_rotation_prompt("scaledown", f"scaledown/{task}")) == spec["input"]
    assert "scaledown" not in rotation_candidates(include_input_only=False)
    assert all(provider != "scaledown" for provider, _ in throughput_candidates())


@pytest.mark.usefixtures("task_routes")
def test_task_models_cannot_be_selected_as_general_chat():
    from trusted_router.catalog import MODELS
    from trusted_router.routing_candidates import (
        InvalidAutoModelOrder,
        _is_regular_chat_model,
        validate_auto_model_order,
    )

    for task in scaledown.TASKS:
        model_id = f"scaledown/{task}"
        assert not _is_regular_chat_model(MODELS[model_id])
        with pytest.raises(InvalidAutoModelOrder, match="task"):
            validate_auto_model_order(model_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("task", list(scaledown.TASKS))
@pytest.mark.usefixtures("task_routes")
async def test_task_rotation_request_passes_native_input(task):
    from trusted_router.synthetic.probes import SyntheticTarget, provider_rotation_probe

    def handle(request):
        body = json.loads(request.content)
        assert body["provider"] == {"only": ["scaledown"]}
        assert json.loads(body["messages"][0]["content"]) == scaledown.TASKS[task]["input"]
        event = {"choices": [{"delta": {"content": json.dumps(RESULTS[task])}}]}
        return httpx.Response(200, content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sample = await provider_rotation_probe(
            client,
            SyntheticTarget("test", "https://api.example/v1", "us-central1"),
            monitor_region="us-central1",
            api_key="test-only",
            provider="scaledown",
            model=f"scaledown/{task}",
        )
    assert sample.status == "success"


def test_free_output_is_a_reviewed_contract_not_a_generic_loophole():
    row = {
        "id": "scaledown/extract",
        "input_token_price_per_m": 50_000,
        "output_token_price_per_m": 0,
    }
    assert provider_manifest_price_profile_is_valid(row)
    for change in (
        {"id": "unknown/free"},
        {"input_token_price_per_m": 0},
        {"output_token_price_per_m": 1},
        {"price_tiers": []},
        {"cached_input_token_price_per_m": 0},
    ):
        assert not provider_manifest_price_profile_is_valid({**row, **change})
    assert _customer_price(0) > 0  # Ordinary provider floor unchanged.


def test_provider_privacy_and_hourly_discovery_contracts():
    p = PROVIDERS["scaledown"]
    assert p.supports_prepaid and not p.supports_byok
    assert p.provider_zero_data_retention and not p.stores_content and not p.provider_e2ee
    assert "scaledown" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "scaledown" in EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert "scaledown" in PROVIDER_SLUGS
    assert default_provider_secret_ref("scaledown") == "env://SCALEDOWN_API_KEY"
    root = Path(__file__).resolve().parents[1]
    assert (
        "SCALEDOWN_API_KEY:trustedrouter-scaledown-api-key"
        in (root / ".github/workflows/refresh-prices.yml").read_text()
    )


def test_live_manifest_publishes_four_credits_routes_with_free_output():
    # Each routable task row of the committed manifest becomes one Credits
    # route; a row the refresh tombstoned is simply not expected. The input
    # price a route bills is checked on the rows discovery writes
    # (test_task_routes_bill_the_listed_input_price_plus_markup); the price
    # ScaleDown lists today is test_scaledown_still_charges_its_input_price.
    raw = json.loads(scaledown.MANIFEST_PATH.read_text())
    routable = [row["id"] for row in raw["models"] if row.get("routable") is not False]
    models, endpoints = _supplemental_provider_models_and_endpoints()
    routes = [e for e in endpoints.values() if e.provider == "scaledown"]
    assert sorted(e.model_id for e in routes) == sorted(routable)
    for e in routes:
        assert e.usage_type == "Credits"
        assert e.completion_price_microdollars_per_million_tokens == 0
        assert e.price_tiers[0].completion_price_microdollars_per_million_tokens == 0
        assert models[e.model_id].documentation.example_input
        assert provider_model_requires_exact_global_settlement(e.provider, e.model_id)


@pytest.mark.usefixtures("task_routes")
def test_task_routes_bill_the_listed_input_price_plus_markup():
    # Discovery read $0.05 per million input tokens off the pricing page: each
    # task route bills that plus the 5.5% markup, and nothing for output.
    from trusted_router.catalog import MODEL_ENDPOINTS

    for task in scaledown.TASKS:
        route = MODEL_ENDPOINTS[f"scaledown/{task}@scaledown/prepaid"]
        assert route.prompt_price_microdollars_per_million_tokens == 52_750
        assert route.completion_price_microdollars_per_million_tokens == 0


@pytest.mark.provider_health
def test_scaledown_still_charges_its_input_price():
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    _models, endpoints = _supplemental_provider_models_and_endpoints()
    routes = [e for e in endpoints.values() if e.provider == "scaledown"]
    for e in routes:
        assert e.prompt_price_microdollars_per_million_tokens == _customer_price(50_000)


@pytest.mark.provider_health
def test_scaledown_serves_its_four_task_routes():
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    from trusted_router.catalog import MODEL_ENDPOINTS

    for task in scaledown.TASKS:
        assert f"scaledown/{task}@scaledown/prepaid" in MODEL_ENDPOINTS


@pytest.mark.usefixtures("task_routes")
def test_gateway_settles_provider_preprocessing_exactly_once(client):
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "scaledown-test@example.com"},
        json={"name": "task billing"},
    )
    assert created.status_code == 201, created.text
    key = created.json()["data"]
    auth = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": "scaledown/classify",
            "estimated_input_tokens": 10,
            "max_output_tokens": 64,
        },
    )
    assert auth.status_code == 200, auth.text
    authorization = auth.json()["data"]
    assert authorization["provider"] == "scaledown"
    payload = {
        "authorization_id": authorization["authorization_id"],
        "actual_input_tokens": 394,
        "actual_output_tokens": 0,
        "request_id": "scaledown-billing-test",
        "elapsed_seconds": 0.5,
    }
    first = client.post("/v1/internal/gateway/settle", json=payload)
    assert first.status_code == 200, first.text
    expected = token_cost_microdollars(394, _customer_price(50_000))
    assert first.json()["data"]["cost_microdollars"] == expected
    generation = STORE.get_generation(first.json()["data"]["generation_id"])
    assert generation.tokens_prompt == 394 and generation.tokens_completion == 0
    second = client.post("/v1/internal/gateway/settle", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["generation_id"] == generation.id
    assert second.json()["data"]["already_settled"]


@pytest.mark.usefixtures("task_routes")
def test_public_pages_and_usage_examples(client):
    assert client.get("/providers/scaledown").status_code == 200
    for task in scaledown.TASKS:
        page = client.get(f"/models/scaledown/{task}")
        assert page.status_code == 200
        assert "input-only" in page.text.lower()


@pytest.mark.parametrize("task", scaledown.TASKS)
@pytest.mark.usefixtures("task_routes")
def test_free_output_is_visible_in_all_model_prices(task, monkeypatch):
    from trusted_router.catalog import MODELS
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.dashboard import _model_detail_view, _model_route_evidence, _model_view

    # This checks price formatting, not freshness: the hourly refresh's
    # validation run must not fail because this manifest reached its deadline.
    monkeypatch.setattr(ModelEndpoint, "catalog_is_current", lambda _self, *, at=None: True)
    model = MODELS[f"scaledown/{task}"]
    listing = _model_view(model, test_mode=True)
    detail = _model_detail_view(model, test_mode=True)
    evidence = _model_route_evidence(model, test_mode=True)
    assert listing["completion_price"] == "$0/1M"
    assert listing["completion_price_sort"] == 0
    assert detail["completion_price"] == "$0/1M"
    assert all(endpoint["completion_price"] == "$0/1M" for endpoint in detail["endpoints"])
    assert evidence["lowest_completion_price"] == "$0/1M"


@pytest.mark.usefixtures("task_routes")
def test_unknown_zero_prices_still_mean_selected_route():
    from dataclasses import replace

    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.dashboard import _endpoint_price_range, _price

    # Formatting needs any real endpoint, current or not: the hourly refresh's
    # validation run must not fail because this manifest reached its deadline.
    endpoint = next(e for e in MODEL_ENDPOINTS.values() if e.model_id == "scaledown/compress")
    attr = "completion_price_microdollars_per_million_tokens"
    assert _price(0) == "selected route"
    assert _price(-1, include_zero=True) == "selected route"
    assert _endpoint_price_range((replace(endpoint, provider="unknown"),), attr) == "selected route"
    assert _endpoint_price_range((replace(endpoint, model_id="unknown"),), attr) == "selected route"
    assert (
        _endpoint_price_range(
            (
                endpoint,
                replace(endpoint, completion_price_microdollars_per_million_tokens=1_000_000),
            ),
            attr,
        )
        == "$0/1M to $1/1M"
    )
