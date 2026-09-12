"""Sourced company-domain affiliations. The directory is data, never bundled code.

An affiliation proves only an exact verified-email domain match to a reviewed
directory listing, not employment, funding eligibility, or investor endorsement.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from trusted_router.store_protocol import Store

logger = logging.getLogger(__name__)
ENTITY_KIND = "company_affiliation"
MAX_AGE = dt.timedelta(days=30)
MAX_ROWS = 50_000
MAX_BUCKET_BYTES = 512_000
CACHE_SECONDS = 60
SCHEMA_VERSION = 1

# These are verification-policy origins, not the company directory itself.
SOURCE_HOSTS = {
    "Y Combinator": ("ycombinator.com",),
    "StartX": ("startx.com", "startx.stanford.edu"),
    "Sequoia Capital": ("sequoiacap.com",),
    "Andreessen Horowitz (a16z)": ("a16z.com", "a16zcrypto.com"),
    "Lightspeed Venture Partners": ("lsvp.com",),
    "General Catalyst": ("generalcatalyst.com",),
    "Accel": ("accel.com",),
    "Insight Partners": ("insightpartners.com",),
    "Bain Capital Ventures": ("baincapitalventures.com",),
    "Khosla Ventures": ("khoslaventures.com",),
    "Bessemer Venture Partners": ("bvp.com",),
    "Lux Capital": ("luxcapital.com",),
}
_SHARED_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "pm.me", "mail.com",
    "fastmail.com", "hey.com", "qq.com", "163.com", "126.com", "gmx.com",
    "gmx.de", "yandex.com", "yandex.ru", "github.io", "gitlab.io",
    "netlify.app", "vercel.app", "webflow.io", "wixsite.com", "notion.site",
    "framer.website", "framer.app", "substack.com", "medium.com",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "co.uk", "com.au", "co.in", "co.jp", "com.br", "com.cn", "com.sg",
})
_ACTIVE_STATUSES = frozenset({"active", "operating", "public", "private", "listed"})
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def normalize_company_domain(value: str) -> str:
    value = value.strip().lower()
    if any(character in value for character in "/:@*\\") or value.endswith("."):
        raise ValueError("Invalid company domain")
    try:
        domain = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid company domain") from exc
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(p) for p in labels):
        raise ValueError("Invalid company domain")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not company domains")
    if any(domain == shared or domain.endswith("." + shared) for shared in _SHARED_DOMAINS):
        raise ValueError("Shared domains cannot establish company affiliation")
    return domain


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        len(value) > 2048 or parsed.scheme not in {"http", "https"}
        or not parsed.hostname or parsed.username or parsed.password
        or parsed.port not in {None, 80, 443}
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("A public source URL is required")
    return value


def _timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _bucket(domain: str) -> str:
    return hashlib.sha256(domain.encode()).hexdigest()[:2]


def build_snapshot(
    rows: Iterable[Mapping[str, Any]], *, source_sheet_url: str,
    now: dt.datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate a Sheet export and build bounded immutable domain buckets."""
    now = now or dt.datetime.now(dt.UTC)
    sheet = urlsplit(_url(source_sheet_url))
    if sheet.scheme != "https" or sheet.hostname != "docs.google.com" or not sheet.path.startswith("/spreadsheets/d/"):
        raise ValueError("The source must be a Google Sheet")
    by_domain: dict[str, list[dict[str, Any]]] = {}
    owners: dict[str, str] = {}
    for number, row in enumerate(rows, 1):
        if number > MAX_ROWS:
            raise ValueError("Company directory exceeds row limit")
        if str(row.get("enabled", "")).strip().lower() != "true":
            continue
        if str(row.get("listing_status", "")).strip().lower() not in _ACTIVE_STATUSES:
            continue
        domain = normalize_company_domain(str(row.get("domain", "")))
        company_name = str(row.get("company_name", "")).strip()
        organization = str(row.get("funding_organization", "")).strip()
        if not company_name or len(company_name) > 200 or organization not in SOURCE_HOSTS:
            raise ValueError(f"Invalid company or organization at row {number}")
        source_url = _url(str(row.get("directory_url", "")).strip())
        source_host = urlsplit(source_url).hostname or ""
        if not any(source_host == host or source_host.endswith("." + host) for host in SOURCE_HOSTS[organization]):
            raise ValueError(f"Directory source is not owned by the organization at row {number}")
        website = _url(str(row.get("company_url", "")).strip())
        website_domain = normalize_company_domain((urlsplit(website).hostname or "").removeprefix("www."))
        if website_domain != domain:
            raise ValueError(f"Company website and email domain disagree at row {number}")
        checked_at = _timestamp(str(row.get("verified_at", "")))
        if checked_at > now + dt.timedelta(minutes=5):
            raise ValueError(f"Future observation at row {number}")
        if now - checked_at > MAX_AGE:
            continue
        raw_year = str(row.get("founding_year", "") or "").strip()
        year: int | None = None
        if raw_year:
            if not re.fullmatch(r"\d{4}", raw_year) or not 1000 <= int(raw_year) <= now.year:
                raise ValueError(f"Invalid founding year at row {number}")
            _url(str(row.get("founding_year_source", "")).strip())
            year = int(raw_year)
        owner = company_name.casefold()
        if domain in owners and owners[domain] != owner:
            raise ValueError(f"ambiguous company ownership for {domain}")
        owners[domain] = owner
        record = {
            "company_name": company_name,
            "funding_organization": organization,
            "relationship": "accelerator" if organization in {"Y Combinator", "StartX"} else "portfolio",
            "domain": domain,
            "founding_year": year,
            "source_url": source_url,
            "checked_at": checked_at.isoformat(),
            "match_method": "verified_email_domain",
        }
        existing = by_domain.setdefault(domain, [])
        if record not in existing:
            if any(item["funding_organization"] == organization for item in existing):
                raise ValueError(f"Conflicting duplicate affiliation for {domain}")
            existing.append(record)
    for records in by_domain.values():
        records.sort(key=lambda item: (item["funding_organization"], item["source_url"]))
        if len({record["founding_year"] for record in records if record["founding_year"] is not None}) > 1:
            raise ValueError("Conflicting founding years require review")
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "source_sheet_url": source_sheet_url,
        "generated_at": now.isoformat(), "expires_at": (now + MAX_AGE).isoformat(),
        "domain_count": len(by_domain), "record_count": sum(map(len, by_domain.values())),
    }
    revision = hashlib.sha256(_canonical({"metadata": metadata, "domains": by_domain})).hexdigest()
    metadata["revision"] = revision
    documents = {"current": metadata}
    for domain, records in sorted(by_domain.items()):
        document = documents.setdefault(f"{revision}:{_bucket(domain)}", {"revision": revision, "domains": {}})
        document["domains"][domain] = records
    if any(len(_canonical(doc)) > MAX_BUCKET_BYTES for doc in documents.values()):
        raise ValueError("Company directory bucket exceeds size limit")
    return documents


def validate_documents(documents: dict[str, dict[str, Any]]) -> str:
    """Reject malformed or oversized imports before opening a transaction."""
    current = documents.get("current", {})
    revision = current.get("revision", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("Invalid affiliation revision")
    if len(documents) > 257 or current.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid affiliation snapshot")
    by_domain: dict[str, Any] = {}
    for key, value in documents.items():
        if len(_canonical(value)) > MAX_BUCKET_BYTES:
            raise ValueError("Affiliation snapshot document too large")
        if key != "current" and (
            not re.fullmatch(re.escape(revision) + r":[a-f0-9]{2}", key)
            or value.get("revision") != revision or not isinstance(value.get("domains"), dict)
        ):
            raise ValueError("Invalid affiliation bucket")
        if key != "current":
            for domain, records in value["domains"].items():
                if key != f"{revision}:{_bucket(domain)}" or domain in by_domain:
                    raise ValueError("Incorrect affiliation bucket placement")
                by_domain[domain] = records
    metadata = {key: value for key, value in current.items() if key != "revision"}
    digest = hashlib.sha256(_canonical({"metadata": metadata, "domains": by_domain})).hexdigest()
    if digest != revision or current.get("domain_count") != len(by_domain) or current.get("record_count") != sum(map(len, by_domain.values())):
        raise ValueError("Incomplete or modified affiliation snapshot")
    return revision


class AffiliationDirectory:
    """A bounded per-process cache. Cache keys are domains, never email addresses."""

    def __init__(self, read: Callable[[str], dict[str, Any] | None]) -> None:
        self._read = read
        self._cache: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
        self._lock = threading.RLock()
        self._retry_after = 0.0

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._retry_after = 0.0

    def defer_retries(self) -> None:
        # No lock: a timed-out database read may still hold the cache lock.
        self._retry_after = time.monotonic() + CACHE_SECONDS

    def _document(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            monotonic = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > monotonic:
                self._cache.move_to_end(key)
                return cached[1]
            value = self._read(key)
            self._cache[key] = (monotonic + CACHE_SECONDS, value)
            self._cache.move_to_end(key)
            while len(self._cache) > 257:
                self._cache.popitem(last=False)
            return value

    def lookup(
        self, email: str | None, *, email_verified: bool,
        now: dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        if email_verified is not True or not email or email.count("@") != 1:
            return []
        local, raw_domain = email.split("@")
        if not local or any(char.isspace() for char in email):
            return []
        try:
            domain = normalize_company_domain(raw_domain)
        except ValueError:
            return []
        now = now or dt.datetime.now(dt.UTC)
        if time.monotonic() < self._retry_after:
            return []
        try:
            metadata = self._document("current")
            if not metadata or metadata.get("schema_version") != SCHEMA_VERSION:
                return []
            if not _timestamp(metadata["generated_at"]) <= now < _timestamp(metadata["expires_at"]):
                return []
            revision = metadata["revision"]
            if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
                return []
            document = self._document(f"{revision}:{_bucket(domain)}")
            if not document or document.get("revision") != revision:
                return []
            records = document.get("domains", {}).get(domain, [])
            return copy.deepcopy([
                record for record in records
                if record.get("domain") == domain and
                dt.timedelta(0) <= now - _timestamp(record["checked_at"]) <= MAX_AGE
            ])
        except Exception:  # noqa: BLE001 - optional enrichment must not break authentication.
            self.defer_retries()
            # Do not attach exceptions or identifiers: a storage error may contain data.
            logger.warning("company_affiliation_lookup_unavailable")
            return []


def directory_for_store(store: Store) -> AffiliationDirectory:
    directory = getattr(store, "_company_affiliation_directory", None)
    if not isinstance(directory, AffiliationDirectory):
        directory = AffiliationDirectory(store.get_company_affiliation_document)
        setattr(store, "_company_affiliation_directory", directory)  # noqa: B010 - private backend cache, outside the public Store contract.
    return directory
