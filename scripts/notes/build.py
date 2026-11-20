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
STYLE = """body{max-width:44rem;margin:3rem auto;padding:0 1.25rem;color:#192522;font:1.12rem/1.65 system-ui}
nav,footer,time{font-size:.95rem}a{color:#075d4a}h1,h2{line-height:1.18}h1{font-size:clamp(2rem,6vw,3.3rem)}
nav,footer{padding:1rem 0;border-block-end:1px solid #bbb}pre{overflow:auto}article{padding-block:1rem}"""
md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")


def page(title, description, path, body, schema):
    data = json.dumps(schema, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} | TrustedRouter Notes</title><meta name="description" content="{escape(description)}">
<link rel="canonical" href="{ORIGIN}{path}"><meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}"><meta property="og:url" content="{ORIGIN}{path}">
<style>{STYLE}</style><script type="application/ld+json">{data}</script>
<body><nav><a href="https://trustedrouter.com/">TrustedRouter</a> / <a href="/notes">Notes</a></nav>
<main><h1>{escape(title)}</h1>{body}</main><footer><a href="https://trustedrouter.com/blog">TrustedRouter blog</a> · <a href="https://trustedrouter.com/models">Browse models</a></footer></body></html>"""


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
    items = "".join(f'<article><h2><a href="{p}">{escape(t)}</a></h2><p>{escape(d)}</p></article>' for _, p, t, d in posts)
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
