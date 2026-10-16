import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image

from trusted_router.dashboard import SEO_CORE_PATHS


def test_media_kit_is_public_canonical_and_discoverable(client: TestClient) -> None:
    response = client.get("/media-kit")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.h1 and page.h1.get_text() == "TrustedRouter media kit"
    assert page.select_one('link[rel="canonical"]')["href"] == "https://trustedrouter.com/media-kit"
    assert page.select_one('meta[property="og:image"]')
    assert "/media-kit" in SEO_CORE_PATHS
    assert client.head("/media-kit").status_code == 200
    assert client.get("/media-kit/").status_code == 200
    assert "Upstream provider retention" in response.text
    for path in ("/", "/resources", "/about"):
        assert 'href="/media-kit"' in client.get(path).text


def test_every_media_kit_download_is_public_and_in_the_archive(client: TestClient) -> None:
    page = BeautifulSoup(client.get("/media-kit").text, "html.parser")
    links = [str(a["href"]) for a in page.select("a[download]")]
    assert len(links) == 11
    archive_response = client.get("/static/media-kit/trustedrouter-media-kit.zip")
    assert archive_response.status_code == 200
    with ZipFile(BytesIO(archive_response.content)) as archive:
        assert len(archive.namelist()) == 10
        for link in links:
            response = client.get(link)
            assert response.status_code == 200, link
            name = Path(link).name
            if name.endswith(".zip"):
                continue
            assert archive.read(name) == response.content, name
            if name.endswith(".svg"):
                assert "image/svg+xml" in response.headers["content-type"]
                assert b"<svg" in response.content
                assert b"<script" not in response.content
            if name.endswith(".png"):
                assert "image/png" in response.headers["content-type"]
                with Image.open(BytesIO(response.content)) as image:
                    assert image.size == ((1200, 630) if "social" in name else (1024, 1024))
                    if "social" not in name:
                        assert image.mode == "RGBA"
                        assert image.getpixel((0, 0))[3] == 0
        assert b"help@trustedrouter.com" in archive.read("about-trustedrouter.txt")


def test_media_kit_uses_the_existing_approved_marks() -> None:
    static = Path(__file__).resolve().parents[1] / "src/trusted_router/static"
    for name, source in {
        "light": "trustedrouter-mark.svg",
        "dark": "trustedrouter-mark-dark.svg",
        "mono": "trustedrouter-mark-mono.svg",
    }.items():
        assert (static / "media-kit" / f"trustedrouter-{name}.svg").read_bytes() == (
            static / source
        ).read_bytes()


def test_token_exchange_footer_links_to_media_kit(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(  # noqa: S603 - fixed repository builder, isolated output directory
        [sys.executable, str(root / "sites/token-exchange/build.py"), "--output", str(tmp_path)],
        check=True, capture_output=True, timeout=60,
    )
    homepages = list(tmp_path.glob("*/index.html"))
    markets = json.loads((root / "sites/token-exchange/markets.json").read_text())
    assert {path.parent.name for path in homepages} == {market["slug"] for market in markets}
    for path in homepages:
        page = BeautifulSoup(path.read_text(), "html.parser")
        links = [link for link in page.select("footer .legal a") if link.get_text() == "Media kit"]
        expected = "https://nytokenexchange.com/media-kit" if path.parent.name == "new-york" else "https://trustedrouter.com/media-kit"
        assert len(links) == 1
        assert links[0]["href"] == expected
