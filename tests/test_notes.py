import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("path", "filename", "media"),
    [
        ("/notes", "index.html", "text/html"),
        ("/notes/first-note", "first-note/index.html", "text/html"),
        ("/sitemap-notes.xml", "sitemap.xml", "application/xml"),
    ],
)
def test_notes_served_from_published_output(client: TestClient, monkeypatch, path, filename, media):
    def fetch(url, **kwargs):
        assert url == f"https://raw.githubusercontent.com/Lore-Hex/quill-router/published-notes/{filename}"
        return httpx.Response(200, content=b"published", request=httpx.Request("GET", url))

    monkeypatch.setattr("trusted_router.routes.notes.httpx.get", fetch)
    response = client.get(path)
    assert response.content == b"published"
    assert response.headers["content-type"].startswith(media)
    assert client.head(path).content == b""


def test_sitemap_index_lists_notes(client: TestClient):
    assert "<loc>https://trustedrouter.com/sitemap-notes.xml</loc>" in client.get("/sitemap.xml").text
