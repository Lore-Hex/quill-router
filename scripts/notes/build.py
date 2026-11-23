"""Render only publication-ready posts from the content repository."""
import argparse
import json
import re
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml
from markdown_it import MarkdownIt

ORIGIN = "https://trustedrouter.com"
# The private editorial repo carries the same assets beside its preview renderer.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOKENS = ROOT / "docs/brand/nyte/v2/03_Design_Tokens/nyte-tokens.css"
BAND = ROOT / "sites/token-exchange/nyte-v2/band-sky.svg"
STYLE = (TOKENS if TOKENS.exists() else HERE / "nyte-tokens.css").read_text() + (HERE / "notes.css").read_text()
SKY_BAND = (BAND if BAND.exists() else HERE / "band-sky.svg").read_text()
md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def page(title, description, path, body, schema):
    data = json.dumps(schema, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} | TrustedRouter Notes</title><meta name="description" content="{escape(description)}">
<link rel="canonical" href="{ORIGIN}{path}"><meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}"><meta property="og:url" content="{ORIGIN}{path}">
<style>{STYLE}</style><script type="application/ld+json">{data}</script></head>
<body><nav aria-label="Primary"><div class="frame"><a class="brand" href="/">TrustedRouter</a><a href="/notes">Notes</a></div></nav>
<header class="frame"><h1>{escape(title)}</h1><p>{escape(description)}</p></header>
<div class="band" aria-hidden="true">{SKY_BAND}</div>
<main class="frame"><div class="{'listing' if path == '/notes' else 'story'}">{body}</div></main>
<footer><div class="frame"><a href="/notes">All notes</a><a href="/models">Browse models</a></div></footer></body></html>"""


def build(source, output, today):
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Missing posts directory")
    output.mkdir(parents=True, exist_ok=False)
    posts = []
    for file in sorted(source.glob("*.md")):
        if len(file.stem) > 160 or file.is_symlink() or file.stat().st_size > 250_000 or not re.fullmatch("[a-z0-9]+(-[a-z0-9]+)*", file.stem):
            raise ValueError(f"Invalid article path: {file.name}")
        front = re.fullmatch(r"---\n(.+?)\n---\n(.*)", file.read_text(), re.S)
        if not front:
            raise ValueError(f"Missing front matter: {file.name}")
        meta = yaml.safe_load(front[1])
        draft = meta.get("draft", True)
        if type(draft) is not bool:
            raise ValueError(f"Expected boolean draft: {file.name}")
        if draft:
            continue
        day = date.fromisoformat(str(meta["date"]))
        if day > today:
            continue
        title, desc = meta["title"], meta["description"]
        if not all(isinstance(x, str) and x.strip() for x in (title, desc, front[2])):
            raise ValueError(f"Incomplete article: {file.name}")
        path = f"/notes/{file.stem}"
        target = output / file.stem
        target.mkdir()
        schema = {"@context": "https://schema.org", "@type": "BlogPosting", "headline": title,
                  "description": desc, "datePublished": day.isoformat(), "url": ORIGIN + path,
                  "author": {"@type": "Organization", "name": "TrustedRouter"}}
        body = f'<time datetime="{day}">{day}</time><article>{md.render(front[2])}</article>'
        (target / "index.html").write_text(page(title, desc, path, body, schema))
        posts.append((day, path, title, desc))
    posts.sort(reverse=True)
    items = "".join(f'<article><time datetime="{day}">{day}</time><div><h2><a href="{p}">{escape(t)}</a></h2><p>{escape(d)}</p></div></article>' for day, p, t, d in posts)
    (output / "index.html").write_text(page("TrustedRouter Notes", "Practical notes on comparing, routing and paying for AI models.", "/notes", items,
                                           {"@context": "https://schema.org", "@type": "Blog", "url": ORIGIN + "/notes"}))
    sitemap = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for path in ["/notes", *(p for _, p, _, _ in posts)]:
        ET.SubElement(ET.SubElement(sitemap, "url"), "loc").text = ORIGIN + path
    ET.ElementTree(sitemap).write(output / "sitemap.xml", encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output, datetime.now(UTC).date())
