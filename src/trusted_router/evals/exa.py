from __future__ import annotations

import importlib
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import ip_address
from re import sub
from shutil import which
from subprocess import run
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlparse

import httpx

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
DEFAULT_EXA_SEARCH_TYPE = "auto"
DEFAULT_EXA_NUM_RESULTS = 5
DEFAULT_EXA_FETCH_RESULTS = 5
DEFAULT_EXA_HIGHLIGHT_CHARS = 4_000


@dataclass(frozen=True)
class ExaResult:
    title: str
    url: str
    published_date: str | None
    author: str | None
    highlights: tuple[str, ...]
    text: str | None
    fetched_text: str | None = None

    def compact_text(self, *, max_chars: int = DEFAULT_EXA_HIGHLIGHT_CHARS) -> str:
        search_parts = [part.strip() for part in self.highlights if part.strip()]
        if self.text and self.text.strip():
            search_parts.append(self.text.strip())
        fetched_text = self.fetched_text.strip() if self.fetched_text else ""
        search_text = "\n".join(search_parts)
        if search_text and fetched_text:
            search_budget = max(300, max_chars // 2)
            fetched_budget = max(300, max_chars - search_budget - 36)
            return "\n".join(
                (
                    "Search extract:",
                    _truncate_text(search_text, search_budget),
                    "Fetched page extract:",
                    _truncate_text(fetched_text, fetched_budget),
                )
            )
        joined = fetched_text or search_text
        return _truncate_text(joined, max_chars)

    def with_fetched_text(self, fetched_text: str | None) -> ExaResult:
        return ExaResult(
            title=self.title,
            url=self.url,
            published_date=self.published_date,
            author=self.author,
            highlights=self.highlights,
            text=self.text,
            fetched_text=fetched_text,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "published_date": self.published_date,
            "author": self.author,
            "highlights": list(self.highlights),
        }


@dataclass(frozen=True)
class ExaSearchBundle:
    query: str
    request_id: str | None
    resolved_search_type: str | None
    cost_dollars: float | None
    results: tuple[ExaResult, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "request_id": self.request_id,
            "resolved_search_type": self.resolved_search_type,
            "cost_dollars": self.cost_dollars,
            "results": [result.public_dict() for result in self.results],
        }


class ExaSearchClient:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = EXA_SEARCH_URL,
        contents_endpoint: str = EXA_CONTENTS_URL,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Exa API key is required")
        self._api_key = api_key
        self._endpoint = endpoint
        self._contents_endpoint = contents_endpoint
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search_with_contents(
        self,
        query: str,
        *,
        exclude_domains: tuple[str, ...] = (),
        include_domains: tuple[str, ...] = (),
        num_results: int = DEFAULT_EXA_NUM_RESULTS,
        search_type: str = DEFAULT_EXA_SEARCH_TYPE,
    ) -> ExaSearchBundle:
        if not query.strip():
            raise ValueError("query is required")
        if num_results < 1 or num_results > 100:
            raise ValueError("num_results must be between 1 and 100")
        payload: dict[str, Any] = {
            "query": query,
            "type": search_type,
            "numResults": num_results,
            "contents": {
                "highlights": True,
            },
        }
        if exclude_domains:
            payload["excludeDomains"] = list(exclude_domains)
        if include_domains:
            payload["includeDomains"] = list(include_domains)
        response = self._client.post(
            self._endpoint,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return parse_exa_search_response(query=query, payload=response.json())

    def fetch_contents(
        self,
        bundle: ExaSearchBundle,
        *,
        max_results: int = DEFAULT_EXA_FETCH_RESULTS,
        max_chars_per_result: int = 4_000,
    ) -> ExaSearchBundle:
        if max_results < 0:
            raise ValueError("max_results cannot be negative")
        if max_chars_per_result < 1:
            raise ValueError("max_chars_per_result must be positive")
        urls = [result.url for result in bundle.results[:max_results]]
        if not urls:
            return bundle
        response = self._client.post(
            self._contents_endpoint,
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            json={
                "urls": urls,
                "text": {"maxCharacters": max_chars_per_result},
                "highlights": {
                    "query": bundle.query,
                    "maxCharacters": max(1_000, max_chars_per_result // 2),
                },
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return merge_exa_contents_response(
            bundle=bundle, payload=response.json(), max_results=max_results
        )


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif not self._skip_depth and normalized in {"tr", "p", "li", "br", "div"}:
            self._parts.append("\n")
        elif not self._skip_depth and normalized in {"td", "th"}:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and normalized in {"tr", "p", "li"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return normalize_visible_html_text("".join(self._parts))


def fetch_search_result_texts(
    bundle: ExaSearchBundle,
    *,
    max_results: int = DEFAULT_EXA_FETCH_RESULTS,
    max_bytes: int = 250_000,
    max_pdf_bytes: int = 8_000_000,
    max_chars_per_result: int = 4_000,
    timeout_seconds: float = 15.0,
    client: httpx.Client | None = None,
) -> ExaSearchBundle:
    """Fetch top search result pages to approximate a web_fetch tool.

    Returned public artifacts still redact fetched page text; it is only used as
    transient model context for live eval replication.
    """

    if max_results < 0:
        raise ValueError("max_results cannot be negative")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if max_chars_per_result < 1:
        raise ValueError("max_chars_per_result must be positive")
    owned_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    fetched: list[ExaResult] = []
    try:
        for index, result in enumerate(bundle.results):
            if index >= max_results:
                fetched.append(result)
                continue
            text = fetch_result_text(
                result.url,
                max_bytes=max_bytes,
                max_pdf_bytes=max_pdf_bytes,
                max_chars=max_chars_per_result,
                timeout_seconds=timeout_seconds,
                client=http_client,
            )
            fetched.append(result.with_fetched_text(text))
    finally:
        if owned_client:
            http_client.close()
    return ExaSearchBundle(
        query=bundle.query,
        request_id=bundle.request_id,
        resolved_search_type=bundle.resolved_search_type,
        cost_dollars=bundle.cost_dollars,
        results=tuple(fetched),
    )


def fetch_result_text(
    url: str,
    *,
    max_bytes: int = 250_000,
    max_pdf_bytes: int = 8_000_000,
    max_chars: int = 4_000,
    timeout_seconds: float = 15.0,
    client: httpx.Client | None = None,
) -> str | None:
    if not _is_fetchable_public_url(url):
        return None
    owned_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    try:
        with http_client.stream("GET", url, timeout=timeout_seconds) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            pdf_response = _looks_like_pdf(url, content_type)
            if not (
                pdf_response
                or content_type.startswith("text/")
                or "html" in content_type
                or "json" in content_type
                or "xml" in content_type
            ):
                return None
            byte_limit = max_pdf_bytes if pdf_response else max_bytes
            chunks: list[bytes] = []
            read = 0
            for chunk in response.iter_bytes():
                read += len(chunk)
                if read > byte_limit:
                    break
                chunks.append(chunk)
    except httpx.HTTPError:
        return None
    finally:
        if owned_client:
            http_client.close()
    raw = b"".join(chunks)
    if not raw:
        return None
    if _looks_like_pdf(url, content_type):
        text = _docling_text_from_bytes(raw, suffix=".pdf")
        if not text:
            text = _pdf_text_from_bytes(raw, timeout_seconds=timeout_seconds)
        if text is None:
            return None
    else:
        text = raw.decode("utf-8", errors="replace")
    if "html" in content_type and not _looks_like_pdf(url, content_type):
        parser = _VisibleTextParser()
        parser.feed(text)
        visible_text = parser.text()
        sec_text = _sec_parser_text_from_html(text) if _is_sec_url(url) else None
        text = _join_structured_and_visible_text(sec_text, visible_text)
    else:
        text = normalize_visible_text(text)
    if not text:
        return None
    return text[: max_chars - 1].rstrip() + "…" if len(text) > max_chars else text


def normalize_visible_text(text: str) -> str:
    return sub(r"\s+", " ", text).strip()


def normalize_visible_html_text(text: str) -> str:
    lines = [normalize_visible_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def _looks_like_pdf(url: str, content_type: str) -> bool:
    return "pdf" in content_type or urlparse(url).path.lower().endswith(".pdf")


def _looks_like_parse_heavy_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (
        path.endswith((".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt"))
        or "sec.gov" in host
        or "annualreports.com" in host
    )


def _is_sec_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


def _pdf_text_from_bytes(raw: bytes, *, timeout_seconds: float) -> str | None:
    executable = which("pdftotext")
    if executable is None:
        return None
    with NamedTemporaryFile(suffix=".pdf") as pdf_file:
        pdf_file.write(raw)
        pdf_file.flush()
        result = run(  # noqa: S603 - executable is resolved with shutil.which, arguments are static.
            [executable, "-layout", "-q", pdf_file.name, "-"],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    if result.returncode != 0 or not result.stdout:
        return None
    return normalize_visible_text(result.stdout.decode("utf-8", errors="replace"))


def _docling_text_from_bytes(raw: bytes, *, suffix: str) -> str | None:
    try:
        document_converter = importlib.import_module("docling.document_converter")
        converter_cls = document_converter.DocumentConverter
    except (ImportError, AttributeError):
        return None
    try:
        with NamedTemporaryFile(suffix=suffix) as input_file:
            input_file.write(raw)
            input_file.flush()
            converter = converter_cls()
            result = converter.convert(input_file.name, raises_on_error=False)
        document = getattr(result, "document", None)
        if document is None or not hasattr(document, "export_to_markdown"):
            return None
        markdown = document.export_to_markdown()
    except Exception:  # noqa: BLE001 - optional parser failures must fall back locally.
        return None
    return normalize_visible_html_text(markdown) if isinstance(markdown, str) else None


def _sec_parser_text_from_html(html: str) -> str | None:
    try:
        sec_parser = importlib.import_module("sec_parser")
    except ImportError:
        return None
    parser_cls = getattr(sec_parser, "Edgar10QParser", None) or getattr(
        sec_parser, "Edgar10KParser", None
    )
    render = getattr(sec_parser, "render", None)
    if parser_cls is None or render is None:
        return None
    try:
        elements = parser_cls().parse(html)
        rendered = render(elements)
    except Exception:  # noqa: BLE001 - optional parser failures must fall back locally.
        return None
    return normalize_visible_html_text(rendered) if isinstance(rendered, str) else None


def _join_structured_and_visible_text(structured: str | None, visible: str) -> str:
    if not structured:
        return visible
    if not visible:
        return structured
    return "\n\n".join(
        (
            "SEC semantic structure:",
            structured,
            "Visible filing text:",
            visible,
        )
    )


def _is_fetchable_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        return False
    try:
        parsed_ip = ip_address(host)
    except ValueError:
        return True
    return not (
        parsed_ip.is_private
        or parsed_ip.is_loopback
        or parsed_ip.is_link_local
        or parsed_ip.is_multicast
        or parsed_ip.is_reserved
        or parsed_ip.is_unspecified
    )


def parse_exa_search_response(*, query: str, payload: dict[str, Any]) -> ExaSearchBundle:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Exa response did not contain results")
    results = tuple(_parse_result(item) for item in raw_results if isinstance(item, dict))
    cost_dollars = None
    cost_payload = payload.get("costDollars")
    if isinstance(cost_payload, dict):
        total = cost_payload.get("total")
        if isinstance(total, int | float):
            cost_dollars = float(total)
    request_id = payload.get("requestId")
    resolved_search_type = payload.get("resolvedSearchType")
    return ExaSearchBundle(
        query=query,
        request_id=request_id if isinstance(request_id, str) else None,
        resolved_search_type=resolved_search_type
        if isinstance(resolved_search_type, str)
        else None,
        cost_dollars=cost_dollars,
        results=results,
    )


def merge_exa_contents_response(
    *,
    bundle: ExaSearchBundle,
    payload: dict[str, Any],
    max_results: int,
) -> ExaSearchBundle:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Exa contents response did not contain results")
    content_by_url: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("id")
        if isinstance(url, str):
            content_by_url[url] = item
    statuses = payload.get("statuses")
    failed_urls: set[str] = set()
    if isinstance(statuses, list):
        for item in statuses:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            url = item.get("url") or item.get("id")
            if status == "error" and isinstance(url, str):
                failed_urls.add(url)

    merged: list[ExaResult] = []
    for index, result in enumerate(bundle.results):
        if index >= max_results or result.url in failed_urls:
            merged.append(result)
            continue
        content = content_by_url.get(result.url)
        if not content:
            merged.append(result)
            continue
        text = content.get("text")
        highlights_value = content.get("highlights")
        content_highlights: tuple[str, ...] = ()
        if isinstance(highlights_value, list):
            content_highlights = tuple(
                value for value in highlights_value if isinstance(value, str)
            )
        merged.append(
            ExaResult(
                title=result.title,
                url=result.url,
                published_date=result.published_date,
                author=result.author,
                highlights=result.highlights + content_highlights,
                text=result.text,
                fetched_text=text if isinstance(text, str) else result.fetched_text,
            )
        )
    return ExaSearchBundle(
        query=bundle.query,
        request_id=bundle.request_id,
        resolved_search_type=bundle.resolved_search_type,
        cost_dollars=bundle.cost_dollars,
        results=tuple(merged),
    )


def format_search_context(bundle: ExaSearchBundle, *, max_chars_per_result: int = 1_200) -> str:
    if not bundle.results:
        return "No search results were returned."
    parts: list[str] = []
    for index, result in enumerate(bundle.results, start=1):
        snippet = result.compact_text(max_chars=max_chars_per_result)
        parts.append(
            "\n".join(
                (
                    f"[{index}] {result.title}",
                    f"URL: {result.url}",
                    f"Published: {result.published_date or 'unknown'}",
                    f"Extract: {snippet or 'No extract returned.'}",
                )
            )
        )
    return "\n\n".join(parts)


def _parse_result(item: dict[str, Any]) -> ExaResult:
    highlights_value = item.get("highlights")
    highlights: tuple[str, ...] = ()
    if isinstance(highlights_value, list):
        highlights = tuple(value for value in highlights_value if isinstance(value, str))
    title = item.get("title")
    url = item.get("url")
    if not isinstance(title, str) or not title.strip():
        title = "Untitled"
    if not isinstance(url, str) or not url.strip():
        url = "about:blank"
    published_date = item.get("publishedDate")
    author = item.get("author")
    text = item.get("text")
    return ExaResult(
        title=title.strip(),
        url=url.strip(),
        published_date=published_date if isinstance(published_date, str) else None,
        author=author if isinstance(author, str) else None,
        highlights=highlights,
        text=text if isinstance(text, str) else None,
    )
