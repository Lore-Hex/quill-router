from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.routes import public as public_routes
from trusted_router.services.email import EmailMessage

PDF_HASHES = {
    "TrustedRouter-Security-Deck.pdf": "8108d1381a5ad0ff18936e1e55b0c332db829857c8d9fd21545f1befb079f55d",
    "TrustedRouter-Security-Whitepaper.pdf": "1681a6726fa753f89a4630cf9065c3c414070a2a06c222e0b21cae12769a7232",
}


@pytest.fixture(autouse=True)
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    public_routes._INQUIRY_HITS.clear()
    public_routes._INQUIRY_GLOBAL_HITS.clear()
    messages: list[EmailMessage] = []

    class Mail:
        def send(self, message: EmailMessage) -> bool:
            messages.append(message)
            return True

    monkeypatch.setattr(public_routes, "get_email_service", lambda _: Mail())
    return messages


@pytest.mark.parametrize("path", ["/token-exchange/security", "/token-exchange/security/"])
def test_security_page_and_discovery(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select("h1")) == 1
    assert soup.select_one('link[rel="canonical"]')["href"] == "https://trustedrouter.com/token-exchange/security"
    form = soup.select_one("#enterprise-brief-form")
    assert form["action"] == "/token-exchange/brief"
    assert form["data-resource"] == "security"
    assert soup.select_one('input[name="email"]')["required"] == ""
    assert "18 slides" in response.text and "19 pages" in response.text
    assert "does not subscribe you to a newsletter" in response.text
    assert not soup.select('a[href$=".pdf"], a[href$=".zip"]')
    assert "/token-exchange/security" in client.get("/sitemap-core.xml").text
    for source in ("/token-exchange", "/security", "/trust"):
        assert '/token-exchange/security"' in client.get(source).text
    for img in soup.select(".ts-cover img"):
        assert img["alt"]
        assert client.get(img["src"]).status_code == 200
    assert client.get(soup.select_one('meta[property="og:image"]')["content"]).status_code == 200


def test_security_pack_contains_exact_owner_supplied_pdfs(
    client: TestClient, sent_messages: list[EmailMessage], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(public_routes, "log_browser_funnel_event", lambda _, event: events.append(event))
    with caplog.at_level(logging.INFO):
        response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert 'attachment; filename="TrustedRouter-Security-Pack.zip"' == response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == sorted(PDF_HASHES)
        assert archive.testzip() is None
        for name, digest in PDF_HASHES.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert len(sent_messages) == 1
    assert sent_messages[0].to == "enterprise@trustedrouter.com"
    assert sent_messages[0].reply_to == "ada@example.com"
    assert "security pack requested" in sent_messages[0].subject
    assert "https://trustedrouter.com/token-exchange/security" in sent_messages[0].text_body
    assert sent_messages[0].mail_class == "enterprise_brief"
    assert events == ["enterprise_security_pack_delivered"]
    assert "ada@example.com" not in caplog.text


@pytest.mark.parametrize("resource", [None, "", "../../config.py", "security.pdf", [], {}, 1])
def test_resource_is_allowlisted(client: TestClient, sent_messages: list[EmailMessage], resource: object) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": resource})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_resource"
    assert not sent_messages


@pytest.mark.parametrize("email", [None, "", "bad", "a@example.com\r\nBcc: b@example.com", [], 7])
def test_security_requires_valid_email(client: TestClient, sent_messages: list[EmailMessage], email: object) -> None:
    response = client.post("/token-exchange/brief", json={"email": email, "resource": "security"})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_email"
    assert not sent_messages


@pytest.mark.parametrize("headers", [{"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_security_gate_rejects_cross_site(client: TestClient, sent_messages: list[EmailMessage], headers: dict[str, str]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security"}, headers=headers)
    assert response.status_code == 403
    assert not sent_messages


def test_downloads_share_rate_limit_and_honeypot(client: TestClient, sent_messages: list[EmailMessage]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security", "website": "bot"})
    assert response.json() == {"ok": True}
    assert not sent_messages
    for resource in ("brochure", "security", "brochure", "security", "security"):
        assert client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": resource}).status_code == 200
    assert client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security"}).status_code == 429
    assert len(sent_messages) == 5


@pytest.mark.parametrize("raises", [False, True])
def test_pack_is_not_released_if_lead_delivery_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raises: bool,
) -> None:
    class Mail:
        def send(self, message: EmailMessage) -> bool:
            if raises:
                raise RuntimeError(f"Reflected {message.reply_to}")
            return False

    monkeypatch.setattr(public_routes, "get_email_service", lambda _: Mail())
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security"})
    assert response.status_code == 503
    assert response.json()["error"] == "delivery_unavailable"
    assert "ada@example.com" not in caplog.text + response.text


def test_missing_security_pack_never_collects_lead(
    client: TestClient, sent_messages: list[EmailMessage], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(public_routes, "_SECURITY_PACK", tmp_path / "missing.zip")
    assert client.post("/token-exchange/brief", json={"email": "ada@example.com", "resource": "security"}).status_code == 503
    assert not sent_messages


@pytest.mark.parametrize("filename", [*PDF_HASHES, "TrustedRouter-Security-Pack.zip"])
def test_security_download_is_not_static_or_gettable(client: TestClient, filename: str) -> None:
    assert client.get("/token-exchange/brief?resource=security&email=ada@example.com").status_code == 405
    assert client.get(f"/static/enterprise/{filename}").status_code == 404
    assert client.get(f"/data/enterprise/{filename}").status_code == 404
