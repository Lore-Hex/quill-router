"""One answer to "do these two URLs name the same place", for both sides of it.

WHY THIS IS A MODULE AND NOT TWO PRIVATE FUNCTIONS
    Two things decide what an Azure `regions[]` entry is worth, and they used to
    decide it differently. services.trust_release.validated_azure_metadata
    refuses a record whose entries name one endpoint twice, and
    scripts/verify_trust_measurements.py counts how many distinct endpoints a
    run actually contacted. Each had its own normalizer: the validator's dropped
    an explicit `:443` and the checker's kept it, and neither touched a trailing
    slash, a trailing dot on the host, or a doubled path slash. Two normalizers
    that disagree mean a record can be accepted at the mirror on one reading and
    counted as N covered regions on the other, which is the false-coverage
    defect this whole change exists to remove, rebuilt out of punctuation.

STDLIB ONLY, ON PURPOSE
    The validator's version was built on httpx.URL, which RAISES httpx.InvalidURL
    on a malformed authority instead of returning a value. That exception is not
    in httpx.HTTPError's hierarchy, so it escaped TrustReleaseResolver.resolve()
    and turned the public /trust/azure-release.json route into a 500 for any
    upstream record carrying a region URL like `https://[::1`. urllib.parse
    raises ValueError for the same inputs and is caught here, so the answer to
    "is this a place" is always a value and never an exception.

WHAT "THE SAME PLACE" MEANS HERE, EXACTLY
    Scheme, host, port and path, after the normalizations a client would perform
    or that cannot change where a request lands:
      * scheme and host are lowercased, and one trailing dot on the host is
        removed (`h.com.` and `h.com` are the same name in DNS);
      * a port equal to the scheme's default is dropped, so `https://h/x` and
        `https://h:443/x` are one place;
      * the path has empty segments and `.`/`..` segments removed per RFC 3986
        §5.2.4, so `/a//b`, `/a/./b` and `/a/c/../b` are one path, and a
        trailing slash is dropped so `/attestation/` and `/attestation` are one;
      * query and fragment are NOT part of the identity. A fragment is never
        transmitted, and an attestation route is not made into a second region
        by a parameter.

    Dropping the trailing slash is the one rule a server may disagree with: an
    origin is free to serve `/a` and `/a/` differently, or to redirect between
    them. It is deliberate and it is the safe direction. Every caller here uses
    this to answer "how many distinct places did we cover", so folding two
    strings together can only make a record claim FEWER regions than it wrote
    down — never more — and the callers report the fold as a defect rather than
    swallowing it.

WHAT THIS DOES NOT DO
    * No DNS. Two hostnames that resolve to one address, or a hostname and its
      literal IP, are two endpoints here. Coverage is counted per published
      name, which is what a verifier reading the record would contact.
    * No percent-decoding and no case folding of the path. `/A` and `/a` are
      different paths (they are, to an origin), and `/%61` is left as written.
    * Nothing about whether the place EXISTS or answers. That is the fetch's
      job, not this function's.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

#: Ports that carry no information: naming them cannot change where a request
#: lands, so an identity that kept them would count one place as two.
_DEFAULT_PORTS = {"http": 80, "https": 443}

Identity = tuple[str, str, int | None, str]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A parsed URL, reduced to what a request actually lands on."""

    scheme: str
    host: str
    port: int | None
    path: str
    #: True when the string names the place and nothing else — no query, no
    #: fragment. Both are ignored for identity, so a caller that needs the URL
    #: to be exactly a place (a published coverage claim, say) has to ask.
    bare: bool

    @property
    def identity(self) -> Identity:
        return (self.scheme, self.host, self.port, self.path)


def _normalized_path(path: str) -> str:
    """RFC 3986 §5.2.4 remove_dot_segments, plus empty-segment removal.

    Trailing slashes go with the empty segments, so `/a/` and `/a` normalize
    together — see the module docstring for why that fold is deliberate.
    """
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def parse_endpoint(url: object) -> Endpoint | None:
    """The place `url` names, or None when it does not name one at all.

    None — never an exception — for: a non-string, a string urllib cannot parse
    (a malformed authority such as `https://[::1`, a non-numeric port), a scheme
    other than http or https, an absent host, or a URL carrying userinfo.
    Userinfo is refused rather than ignored because `https://a@b/` reads as host
    a to a human and contacts host b, which is a coverage claim that lies about
    where it points.
    """
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # Invalid IPv6 literal, or a port that is not a number. urlsplit defers
        # both to attribute access, so the parse and the port read are in one
        # try together on purpose.
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    if parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    return Endpoint(
        scheme=scheme,
        host=host,
        port=port,
        path=_normalized_path(parts.path),
        bare=not parts.query and not parts.fragment,
    )
