"""The published comparison must preserve its evidence and privacy qualifiers."""

from __future__ import annotations

import json
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image

from trusted_router.content.blog import BLOG_POSTS_BY_SLUG

SLUG = "trev-zev-lev-private-decisions"
ROOT = Path(__file__).resolve().parents[1]


def test_decision_blog_scores_and_links_match_captured_evidence() -> None:
    evidence = json.loads((ROOT / "docs/blog-evidence/decision-models-2026-09-29.json").read_text())
    post = BLOG_POSTS_BY_SLUG[SLUG]
    soup = BeautifulSoup(post.body_html, "html.parser")
    rows = soup.select("#decision-benchmark tbody tr")
    assert len(rows) == len(evidence["models"]) == 4
    for row, model in zip(rows, evidence["models"], strict=True):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
        assert cells == [
            model["label"], f'{model["passed"]} / {model["problems"]}',
            f'{100 * model["passed"] / model["problems"]:.1f}%',
            f'${model["mean_model_cost_usd"]}',
            f'{model["mean_eval_elapsed_seconds"]:.1f} s',
        ]
        assert row.select_one("a")["href"] == f'{evidence["source"]}/model/{model["id"]}'
    assert post.published_date == evidence["retrieved_date"]
    assert "first run of each problem" in post.body_html
    assert "do not isolate provider effects" in post.body_html
    assert "whole eval run times" in post.body_html
    assert "excluding its safety monitors" in post.body_html


def test_decision_blog_example_has_hard_privacy_floor_and_no_live_key() -> None:
    body = BLOG_POSTS_BY_SLUG[SLUG].body_html
    soup = BeautifulSoup(body, "html.parser")
    command = soup.select_one("pre code").get_text()
    example = json.loads(command.split("-d '", 1)[1].removesuffix("'"))
    assert "https://api.trustedrouter.com/v1/decide" in command
    assert "$TRUSTEDROUTER_API_KEY" in command
    assert "sk-tr-" not in command
    assert example["model"] == "trustedrouter/zev-1.0"
    assert example["provider"] == {"only": ["baseten"], "min_privacy": "zdr"}
    assert "unrestricted named model chains should not be described as universally ZDR" in body
    assert "TypeSafe offers enterprise ZDR on request" in body
    assert "We have not configured that enterprise ZDR arrangement" in body
    assert 'href="https://trust.trustedrouter.com/"' in body
    assert "does not attest its GPUs" in body
    assert not any(dash in soup.get_text() for dash in ("\u2013", "\u2014"))


def test_decision_blog_renders_and_has_its_own_social_image(client: TestClient) -> None:
    page = client.get(f"/blog/{SLUG}")
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    post = BLOG_POSTS_BY_SLUG[SLUG]
    assert soup.select_one("h1").get_text() == post.title
    assert soup.select_one('meta[property="og:image"]')["content"].endswith(post.og_image)
    assert f'href="/blog/{SLUG}"' in client.get("/blog").text
    with Image.open(ROOT / "src/trusted_router" / post.og_image.removeprefix("/")) as image:
        assert image.size == (1200, 630)
        assert image.getbbox() == (0, 0, 1200, 630)
