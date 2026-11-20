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


def test_shared_sections_outside_docs_keep_their_plain_titles(client):
    soup = BeautifulSoup(client.get("/pricing").text, "html.parser")
    assert soup.select_one("#minimum-charges h2") and not soup.select_one(".docs-topic-link")
