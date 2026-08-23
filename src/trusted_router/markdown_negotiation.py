"""Serve the public pages as markdown when an agent asks for markdown.

WHY THIS EXISTS

An agent fetching a marketing or docs page gets a full HTML document: nav,
inline SVG, script tags, and a stylesheet's worth of class attributes wrapped
around a few hundred words it actually wants. It either burns context on the
markup or runs a converter of its own and hopes the result is faithful. The
acceptmarkdown.com convention says the origin should do that work, because the
origin is the only party that knows which parts of its own markup are content.

THE CONTRACT, as implemented here

  * `Accept: text/markdown` (or any Accept in which markdown outranks HTML by
    q-value) on a public page returns `text/markdown; charset=utf-8`.
  * Every response on a negotiated path carries `Accept` in `Vary` — the HTML
    variant too, not only the markdown one. Gzip appends `Accept-Encoding`,
    giving the `Vary: Accept, Accept-Encoding` the convention asks for. This is the half that is
    easy to skip and the half that breaks caches: a CDN that has stored the HTML
    variant under a key that does not include Accept will hand that HTML to the
    next agent asking for markdown, and vice versa. The variant that "wins" is
    then whichever request happened to miss cache first.
  * A request that does not prefer markdown is untouched: same bytes, same
    content type, same status.

WHAT THE CONVERSION IS AND IS NOT

`html_to_markdown` is a deliberately small, lossy converter aimed at THIS
site's markup, not a general one. It keeps headings, paragraphs, lists, links,
emphasis, code, tables and horizontal rules, and drops everything whose purpose
is presentation. It is not a round trip: converting the markdown back to HTML
will not reproduce the page. That is the intended trade — an agent asking for
markdown is asking for the content, not for a reproduction of the document.

Scope limit worth stating plainly: this negotiates the PUBLIC pages only. API
routes under /v1 and friends already speak JSON and are excluded by
`is_public_page_path`, which is shared with main.py's 404 handling so the two
cannot drift apart.
"""

from __future__ import annotations

import re
from typing import Any, Final

from bs4 import BeautifulSoup, NavigableString, Tag

MARKDOWN_CONTENT_TYPE: Final = "text/markdown; charset=utf-8"
# This middleware contributes ONLY "Accept". Starlette's GZipMiddleware wraps
# this one and appends "Accept-Encoding" with a plain string concat that does
# not deduplicate, so setting the full "Accept, Accept-Encoding" here produces
# "Accept, Accept-Encoding, Accept-Encoding" on every compressed response.
# Contributing one token and letting gzip add its own yields the required
# "Accept, Accept-Encoding" exactly once.
MARKDOWN_VARY: Final = "Accept"

# One source of truth, imported by main.py's 404 negotiation too. A second
# hand-maintained copy of this list is how the two answers drift apart.
NON_PUBLIC_PREFIXES: Final = (
    "/v1",
    "/internal",
    "/api",
    "/auth",
    "/oauth",
    "/console",
    "/static",
    "/webhooks",
    "/.well-known",
)

# Elements whose entire subtree is presentation or navigation chrome.
_DROP_TAGS: Final = frozenset(
    {"script", "style", "svg", "noscript", "template", "form", "button", "iframe"}
)

_HEADINGS: Final = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


def is_public_page_path(path: str) -> bool:
    """Is this a public page rather than an API or asset route?"""
    return not any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in NON_PUBLIC_PREFIXES
    )


def _parse_accept(accept: str) -> list[tuple[str, float]]:
    """(media type, q) pairs, most preferred first.

    Malformed q values are treated as q=1 rather than raising: an Accept header
    is client input, and the safe reading of a broken one is the default the
    RFC gives, not a 500.
    """
    parsed: list[tuple[str, float]] = []
    for index, raw in enumerate(accept.split(",")):
        part = raw.strip()
        if not part:
            continue
        pieces = part.split(";")
        media = pieces[0].strip().lower()
        quality = 1.0
        for parameter in pieces[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() != "q":
                continue
            try:
                quality = float(value.strip())
            except ValueError:
                quality = 1.0
        # Index keeps the sort stable so equal-q entries hold header order.
        parsed.append((media, quality))
        del index
    return sorted(parsed, key=lambda item: item[1], reverse=True)


def _quality_for(parsed: list[tuple[str, float]], *candidates: str) -> float:
    """Best q the client offered for any of `candidates`. Wildcards do NOT count.

    `*/*` deliberately scores 0 here. Nearly every HTTP client in existence
    sends `Accept: */*`, so treating it as "markdown is fine" would flip the
    default representation of the whole site for curl, monitoring probes, and
    every SDK that does not set an Accept header. Markdown is served when it is
    asked for by name.
    """
    best = 0.0
    for media, quality in parsed:
        if media in candidates and quality > best:
            best = quality
    return best


def prefers_markdown(accept: str | None) -> bool:
    """Does this client want markdown MORE than it wants HTML?

    Ties go to HTML. A browser sending
    `text/html,application/xhtml+xml,...,*/*;q=0.8` must keep getting the page,
    and an agent sending `text/markdown` (q=1, HTML absent at q=0) gets markdown.
    A client that lists both at the same q has expressed no preference, and the
    existing representation is the safer answer.
    """
    if not accept:
        return False
    parsed = _parse_accept(accept)
    markdown = _quality_for(parsed, "text/markdown", "text/x-markdown")
    if markdown <= 0.0:
        return False
    html = _quality_for(parsed, "text/html", "application/xhtml+xml")
    return markdown > html


def _inline_text(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in _DROP_TAGS:
        return ""
    inner = "".join(
        _inline_text(child) for child in node.children if isinstance(child, (Tag, NavigableString))
    )
    if name in {"strong", "b"}:
        return f"**{inner.strip()}**" if inner.strip() else ""
    if name in {"em", "i"}:
        return f"*{inner.strip()}*" if inner.strip() else ""
    if name == "code":
        return f"`{inner.strip()}`" if inner.strip() else ""
    if name == "br":
        return "\n"
    if name == "a":
        href = str(node.get("href") or "").strip()
        label = inner.strip()
        if not label:
            return ""
        # A bare anchor with no destination is just text.
        if not href or href.startswith("javascript:"):
            return label
        return f"[{label}]({href})"
    return inner


def _table_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = [
            _inline_text(cell).strip().replace("|", "\\|") for cell in row.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _block_markdown(node: Tag, depth: int = 0) -> list[str]:
    name = node.name.lower()
    if name in _DROP_TAGS:
        return []
    if name in _HEADINGS:
        text = _inline_text(node).strip()
        return [f"{_HEADINGS[name]} {text}"] if text else []
    if name == "p":
        text = _inline_text(node).strip()
        return [text] if text else []
    if name == "pre":
        code = node.get_text()
        return ["```", code.strip("\n"), "```"] if code.strip() else []
    if name in {"ul", "ol"}:
        ordered = name == "ol"
        items: list[str] = []
        for index, item in enumerate(node.find_all("li", recursive=False), start=1):
            marker = f"{index}." if ordered else "-"
            text = _inline_text(item).strip()
            if text:
                items.append(f"{'  ' * depth}{marker} {text}")
            for nested in item.find_all(["ul", "ol"], recursive=False):
                items.extend(_block_markdown(nested, depth + 1))
        return items
    if name == "table":
        rendered = _table_markdown(node)
        return [rendered] if rendered else []
    if name == "hr":
        return ["---"]
    if name == "blockquote":
        text = _inline_text(node).strip()
        return [f"> {text}"] if text else []

    blocks: list[str] = []
    for child in node.children:
        if isinstance(child, Tag):
            blocks.extend(_block_markdown(child, depth))
        elif isinstance(child, NavigableString):
            text = re.sub(r"\s+", " ", str(child)).strip()
            if text:
                blocks.append(text)
    return blocks


def html_to_markdown(html: str) -> str:
    """Content of an HTML page as markdown. Lossy by design; see module docstring."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(list(_DROP_TAGS)):
        tag.decompose()

    root = soup.find("main") or soup.find("body") or soup
    if not isinstance(root, Tag):
        return ""

    blocks = [block.strip() for block in _block_markdown(root) if block and block.strip()]

    deduped: list[str] = []
    for block in blocks:
        # Collapse the blank runs that nested divs produce.
        if deduped and deduped[-1] == block:
            continue
        deduped.append(block)
    return "\n\n".join(deduped).strip() + "\n"


class MarkdownNegotiationMiddleware:
    """Pure ASGI, deliberately not `BaseHTTPMiddleware`.

    The first version of this was an `@app.middleware("http")` function, which
    Starlette implements with `BaseHTTPMiddleware`. That wrapper turns EVERY
    response into a streaming one, including responses this middleware returns
    untouched. Starlette's GZipMiddleware then takes its streaming branch,
    which emits chunked transfer-encoding and no `Content-Length`.

    MEASURED: the browser smoke suite caught it on a static PNG.
    `/static/claude-code-hero.png` arrived with no Content-Length, so a test
    asserting the hero image is over 10 KB read 0 and failed. Nothing here ever
    looks at `/static`; merely being in the chain as a BaseHTTPMiddleware was
    enough to change how every asset on the site was framed on the wire.

    As raw ASGI, a request that is not a public page reaches the inner app with
    no wrapping at all, so its response is framed exactly as it would be
    without this installed. Only public page responses are intercepted, and
    only their headers unless markdown was actually asked for.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") not in {"GET", "HEAD"}
            or not is_public_page_path(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        want_markdown = prefers_markdown(_accept_header(scope))
        held_start: dict[str, Any] | None = None
        body = bytearray()

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal held_start
            if message["type"] == "http.response.start":
                original = list(message.get("headers", []))
                content_type = b""
                for key, value in original:
                    if key.lower() == b"content-type":
                        content_type = value.lower()
                if not content_type.startswith(b"text/html"):
                    # Not a page. Adding Vary to a JSON body or an asset would
                    # claim a negotiation that does not happen here.
                    await send(message)
                    return
                headers = [(k, v) for k, v in original if k.lower() != b"vary"]
                headers.append((b"vary", MARKDOWN_VARY.encode()))
                if want_markdown:
                    # Hold the start: the new content-length is not known
                    # until the body has been converted.
                    held_start = {**message, "headers": headers}
                    return
                await send({**message, "headers": headers})
                return

            if message["type"] == "http.response.body" and held_start is not None:
                body.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                payload = html_to_markdown(bytes(body).decode("utf-8", "replace")).encode("utf-8")
                headers = [
                    (k, v)
                    for k, v in held_start["headers"]
                    if k.lower() not in {b"content-type", b"content-length"}
                ]
                headers.append((b"content-type", MARKDOWN_CONTENT_TYPE.encode()))
                headers.append((b"content-length", str(len(payload)).encode()))
                await send({**held_start, "headers": headers})
                await send({"type": "http.response.body", "body": payload})
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)


def _accept_header(scope: Any) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"accept":
            return value.decode("latin-1")
    return ""
