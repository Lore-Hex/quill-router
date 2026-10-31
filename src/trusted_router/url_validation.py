"""Parse untrusted URLs without leaking urllib's authority errors."""

from urllib.parse import ParseResult, urlparse


def parse_url(value: str, *, check_port: bool = True) -> ParseResult | None:
    """Return None for malformed authorities, preserving urlparse semantics.

    Port validation is eager by default because urllib defers it to attribute
    access. Callers with an existing port-specific error can check it themselves.
    """
    try:
        parsed = urlparse(value)
        if check_port:
            _ = parsed.port
        return parsed
    except ValueError:
        return None
