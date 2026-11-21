from bs4 import BeautifulSoup


def test_docs_topics_have_native_unambiguous_links(client):
    soup = BeautifulSoup(client.get("/docs").text, "html.parser")
    reader = soup.select_one(".docs-main-reader")
    for heading in reader.select("h2, h3"):
        guide = heading.find_parent("a", href=True)
        if guide:
            assert guide["href"].startswith(("/", "#"))
            continue
        link = heading.select_one("a.docs-topic-link")
        assert link and link["href"].startswith("#")
        target, = soup.find_all(id=link["href"][1:])
        assert target is heading or target in heading.parents
    topic = soup.find(id="deidentification")
    assert topic.h2.a["href"] == "#deidentification"
    assert topic.h2["id"] == "optional-de-identification"  # Older contents-menu bookmark.
    assert reader.select_one("a.card-as-link[href='#deidentification']")
    toc, = reader.select('nav[aria-label="Table of contents"]')
    links = [a["href"] for a in toc.select("a")]
    assert len(links) == len(set(links))
    assert set(links) == {a["href"] for a in reader.select(".docs-topic-link")}
    assert toc.select_one('ul[aria-label="Privacy"] a[href="#deidentification"]').text == "Deidentify"


def test_shared_sections_outside_docs_keep_their_plain_titles(client):
    soup = BeautifulSoup(client.get("/pricing").text, "html.parser")
    assert soup.select_one("#minimum-charges h2") and not soup.select_one(".docs-topic-link")


def test_request_audit_examples_use_control_plane_and_distinct_ids(client):
    soup = BeautifulSoup(client.get("/docs").text, "html.parser")
    guide = soup.select_one("#request-audit")
    billing, generation = [block.get_text() for block in guide.select("pre code")]
    assert "https://trustedrouter.com/v1/requests/${REQUEST_ID:?set REQUEST_ID}/billing" in billing
    assert ".data.generation_id" in generation
    assert "https://trustedrouter.com/v1/generation" in generation
    assert 'id=$GENERATION_ID' in generation
    for example in (billing, generation):
        assert "Authorization: Bearer $TRUSTEDROUTER_API_KEY" in example
        assert "api.trustedrouter.com" not in example
    for field in ("provider_name", "is_byok", "total_cost_microdollars"):
        assert field in generation
    assert "trustedrouter.routing.selected_endpoint" in guide.get_text()
    assert "GET https://trustedrouter.com/v1/activity?group_by=request" in guide.get_text()
    assert soup.select_one('nav a[href="#request-audit"]')
    for path in ("/docs/byok", "/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert "/docs#request-audit" in client.get(path).text, path
