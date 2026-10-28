"""Build the public NYTE press page and deterministic download bundle."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path
from string import Template
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

HERE = Path(__file__).resolve().parent
ASSET_URL = "/assets/press"
ASSETS = (
    ("nyte-wordmark-light", "Wordmark on light", "light", "svg", "png"),
    ("nyte-wordmark-dark", "Wordmark on dark", "dark", "svg", "png"),
    ("nyte-wordmark-mono", "Monochrome wordmark", "light", "svg", "png"),
    ("nyte-mark-light", "Globe mark on light", "light", "svg", "png"),
    ("nyte-mark-dark", "Globe mark on dark", "dark", "svg", "png"),
    ("nyte-mark-mono", "Monochrome mark", "light", "svg", "png"),
)
USAGE = (
    "Use these assets to identify NY Token Exchange in editorial coverage, "
    "integrations and partner materials. Keep proportions and colors intact; do not "
    "stretch, recolor, crop or add effects to the logos. Leave clear space at least "
    "half the mark's height around each lockup. Use a contrasting background. "
    "The globe mark and NY Token Exchange wordmark are NYTE's brand assets. "
    "Use the wordmark on first reference. Write NY Token Exchange (NYTE), then NYTE. "
    "Link to https://nytokenexchange.com. Logos are not certifications or endorsements. "
    "All trademark rights remain with Lore Hex Corp. Credit screenshots and social "
    "artwork to NY Token Exchange. The river image in historical screenshots is symbolic "
    "generated artwork, not a photograph of company facilities or New York. "
    "Screenshots are dated illustrations; do not present captured prices or "
    "availability as current. This kit does not license third-party provider logos."
)


def build_press(output: Path) -> None:
    data = json.loads((HERE / "press.json").read_text())
    assets = output / "assets" / "press"
    assets.mkdir(parents=True, exist_ok=True)
    # Explicit allowlist: a stray file in a reused build must never enter the ZIP.
    names = [f"{name}.{ext}" for name, _, _, *formats in ASSETS for ext in formats]
    names.extend(("nyte-homepage.png", "nyte-social.png", "nyte-social.svg"))
    for name in names:
        shutil.copyfile(HERE / "press-assets" / name, assets / name)
    shutil.copyfile(HERE / "media/nyte-routing-demo-poster.jpg", assets / "nyte-demo-still.jpg")
    about = (
        f"{data['name']} ({data['short_name']})\nUpdated: {data['updated']}\n\n"
        + "\n".join(f"{label}: {value}" for label, value in data["facts"])
        + f"\n\nONE SENTENCE\n{data['one_sentence']}\n\nBOILERPLATE\n{data['boilerplate']}"
        + f"\n\nFOUNDER\n{data['founder_bio']}\n\nEDITORIAL QUESTIONS\n"
        + "\n\n".join(f"{question}\n{answer}" for question, answer in data["questions"])
        + f"\n\nASSET USAGE\n{USAGE}\n"
    )
    (assets / "nyte-fact-sheet.txt").write_text(about)
    (assets / "nyte-fact-sheet.json").write_text(json.dumps(data, indent=2) + "\n")
    (assets / "README.txt").write_text(
        f"NY TOKEN EXCHANGE - PUBLIC PRESS KIT\nUpdated: {data['updated']}\n\n"
        "Source: https://nytokenexchange.com/media-kit\nContact: help@trustedrouter.com\n\n"
        "Wordmarks: outlined SVG and transparent 2400 x 480 PNG.\n"
        "Globe marks: SVG and transparent 2048 x 1024 PNG.\n"
        "Social image: 1200 x 630 PNG. Homepage: 1920 x 1080 PNG, captured 2026-10-15.\n"
        "Demo still: 1280 x 720 JPEG, from the public illustrative routing demo.\n"
        "Full demo (not included in this ZIP): https://nytokenexchange.com/assets/nyte-routing-demo.mp4\n\n"
        + USAGE + "\n"
    )
    names.extend(("nyte-demo-still.jpg", "nyte-fact-sheet.txt", "nyte-fact-sheet.json", "README.txt"))
    files = sorted(assets / name for name in names)
    archive_path = assets / "nyte-media-kit.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            entry = ZipInfo(path.name, date_time=(2026, 10, 28, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, path.read_bytes())
    escape = html.escape
    cards = []
    for name, title, background, *formats in ASSETS:
        links = "".join(
            f'<a class="download" href="{ASSET_URL}/{name}.{ext}" download '
            f'aria-label="Download {escape(title)} {ext.upper()}">{ext.upper()} <span aria-hidden="true">↓</span></a>'
            for ext in formats
        )
        cards.append(
            f'<article class="asset"><div class="asset-preview {background}">'
            f'<img src="{ASSET_URL}/{name}.svg" alt="NY Token Exchange: {escape(title)}" '
            + ('width="1000" height="500"' if "-mark-" in name else 'width="600" height="120"')
            + (' class="mark"' if "-mark-" in name else "")
            + f'></div><div class="asset-caption"><h3>{escape(title)}</h3><div>{links}</div></div></article>'
        )
    version = hashlib.sha256(
        (HERE / "press.css").read_bytes() + (HERE / "press.js").read_bytes()
    ).hexdigest()[:12]
    values = {key: escape(value) for key, value in data.items() if isinstance(value, str)}
    values.update(
        version=version,
        kit_size=f"{archive_path.stat().st_size / 1024 / 1024:.1f} MB",
        asset_cards="".join(cards),
        facts="".join(f"<div><dt>{escape(k)}</dt><dd>{escape(v)}</dd></div>" for k, v in data["facts"]),
        questions="".join(f"<details><summary>{escape(q)}</summary><p>{escape(a)}</p></details>" for q, a in data["questions"]),
        usage=escape(USAGE),
        schema=json.dumps({
            "@context": "https://schema.org", "@type": "WebPage",
            "name": "NY Token Exchange media kit",
            "url": "https://nytokenexchange.com/media-kit", "dateModified": data["updated"],
            "description": data["one_sentence"],
            "publisher": {"@type": "Organization", "name": "Lore Hex Corp", "url": "https://trustedrouter.com"},
        }).replace("<", "\\u003c"),
    )
    rendered = Template((HERE / "press.html").read_text()).substitute(values)
    for name in ("media-kit", "media-kit.html"):
        (output / name).write_text(rendered)
    for name in ("press.css", "press.js"):
        shutil.copyfile(HERE / name, output / "assets" / name)
