"""Scheduled, fail-closed collection of per-re-mint enclave receipt evidence."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from trusted_router.config import Settings, parse_gateway_region_targets
from trusted_router.receipt_keys import (
    GCP_ATTESTATION_KIND,
    attestation_commits_to_jwk,
    normalize_receipt_jwk,
    receipt_attestation_sha256,
    receipt_kid,
    verify_gcp_attestation_chain,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import ReceiptKey, iso_now
from trusted_router.store_protocol import Store

logger = logging.getLogger(__name__)

RECEIPT_KEY_FETCH_TIMEOUT_SECONDS = 10.0
RECEIPT_KEY_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RECEIPT_KEY_MAX_HISTORY_ENTRIES = 64
RECEIPT_KEY_MAX_TARGETS = 128


@dataclass(frozen=True, order=True)
class ReceiptKeyTarget:
    host: str
    connect_ip: str


def _ipv4_addresses(host: str) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [str(literal)] if literal.version == 4 else []
    addresses = {
        str(sockaddr[0])
        for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
            host,
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    }
    return sorted(addresses)


def discover_receipt_key_targets(settings: Settings) -> list[ReceiptKeyTarget]:
    """Resolve every configured gateway endpoint into one target per A record."""

    canonical_host = urlsplit(settings.api_base_url).hostname
    if not canonical_host:
        raise ValueError("TR_API_BASE_URL has no hostname")
    endpoint_names: list[tuple[str, str]] = [(canonical_host, canonical_host)]
    for entry in parse_gateway_region_targets(settings.synthetic_gateway_region_targets):
        endpoint_names.append((entry.public_host or canonical_host, entry.connect_host))

    targets: set[ReceiptKeyTarget] = set()
    for public_host, resolve_host in endpoint_names:
        try:
            for address in _ipv4_addresses(resolve_host):
                targets.add(ReceiptKeyTarget(host=public_host, connect_ip=address))
        except OSError as exc:
            logger.warning(
                "receipt_key_dns_failed host=%s endpoint=%s error=%s",
                public_host,
                resolve_host,
                exc,
            )
    ordered = sorted(targets)
    if len(ordered) > RECEIPT_KEY_MAX_TARGETS:
        logger.error(
            "receipt_key_target_limit_exceeded discovered=%d limit=%d",
            len(ordered),
            RECEIPT_KEY_MAX_TARGETS,
        )
    return ordered[:RECEIPT_KEY_MAX_TARGETS]


def _fetch_receipt_key(target: ReceiptKeyTarget, *, verify_tls: bool) -> dict[str, Any]:
    request_url = f"https://{target.connect_ip}/receipt-key"
    verify: bool | ssl.SSLContext = verify_tls
    if not verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        verify = context
    with httpx.Client(
        timeout=RECEIPT_KEY_FETCH_TIMEOUT_SECONDS,
        verify=verify,
        follow_redirects=False,
    ) as client:
        request = client.build_request(
            "GET",
            request_url,
            headers={"Host": target.host, "Accept": "application/json"},
            extensions={"sni_hostname": target.host},
        )
        response = client.send(request, stream=True)
        try:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > RECEIPT_KEY_MAX_RESPONSE_BYTES:
                    raise ValueError("receipt-key response exceeds size limit")
            payload = json.loads(body)
        finally:
            response.close()
    if not isinstance(payload, dict):
        raise ValueError("receipt-key response is not an object")
    return payload


def _record_from_payload(
    payload: dict[str, Any],
    *,
    plane: str,
    seen_at: str,
    history: bool = False,
) -> ReceiptKey:
    kid = payload.get("kid")
    jwk = payload.get("jwk")
    att = payload.get("att")
    att_kind = payload.get("att_kind")
    if not isinstance(kid, str) or not kid:
        raise ValueError("receipt-key response has no kid")
    if not isinstance(jwk, dict):
        raise ValueError("receipt-key response has no JWK")
    if not isinstance(att, str) or not att:
        raise ValueError("receipt-key response has no attestation")
    if not isinstance(att_kind, str) or not att_kind:
        raise ValueError("receipt-key response has no attestation kind")
    narrowed_jwk = normalize_receipt_jwk(jwk)
    if kid != receipt_kid(narrowed_jwk):
        raise ValueError("receipt-key kid does not match JWK x")
    if not attestation_commits_to_jwk(att, att_kind, narrowed_jwk):
        raise ValueError("receipt-key attestation does not contain the key commitment")
    att_sha256 = receipt_attestation_sha256(att, att_kind)
    claimed_att_sha256 = payload.get("att_sha256")
    if claimed_att_sha256 is not None and claimed_att_sha256 != att_sha256:
        raise ValueError("receipt-key attestation hash does not match document bytes")

    verified = False
    if att_kind == GCP_ATTESTATION_KIND:
        if history:
            # History exists specifically to preserve past documents through a
            # collector outage.  Verify its issuer signature, chain, claims,
            # debug state, and key commitment exactly as current evidence, but
            # do not reject it merely because its validity window has ended.
            verify_gcp_attestation_chain(att, allow_expired=True)
        else:
            verify_gcp_attestation_chain(att)
        verified = True
    return ReceiptKey(
        kid=kid,
        jwk=narrowed_jwk,
        att=att,
        att_kind=att_kind,
        plane=plane,
        first_seen=seen_at,
        last_seen=seen_at,
        att_sha256=att_sha256,
        verified=verified,
    )


def _observe_record(
    store: Store,
    record: ReceiptKey,
    result: dict[str, int],
    *,
    refresh_last_seen: bool = True,
) -> None:
    outcome = store.observe_receipt_key(record, refresh_last_seen=refresh_last_seen)
    if outcome in {"appended", "refreshed"}:
        result[outcome] += 1
    else:
        result["skipped"] += 1


def _collect_history(
    payload: dict[str, Any],
    *,
    plane: str,
    seen_at: str,
    store: Store,
    result: dict[str, int],
    target: ReceiptKeyTarget,
) -> None:
    if "att_history" not in payload:
        return
    history = payload.get("att_history")
    if not isinstance(history, list):
        result["skipped"] += 1
        logger.warning(
            "receipt_key_history_skipped host=%s ip=%s index=all error=not-a-list",
            target.host,
            target.connect_ip,
        )
        return
    if len(history) > RECEIPT_KEY_MAX_HISTORY_ENTRIES:
        result["skipped"] += 1
        logger.warning(
            "receipt_key_history_truncated host=%s ip=%s count=%d limit=%d",
            target.host,
            target.connect_ip,
            len(history),
            RECEIPT_KEY_MAX_HISTORY_ENTRIES,
        )
        history = history[:RECEIPT_KEY_MAX_HISTORY_ENTRIES]
    # The enclave history contains documents that stopped being current before
    # this response.  It does not carry an observation timestamp, so reserve
    # the immediately preceding second for newly discovered history.  More
    # importantly, do not refresh historical last_seen on every poll: otherwise
    # thousands of old versions can tie with and evict a live current document
    # from a bounded newest-first read.
    history_seen_at = (
        datetime.fromisoformat(seen_at.replace("Z", "+00:00")) - timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    for index, item in enumerate(history):
        try:
            if not isinstance(item, dict):
                raise ValueError("history entry is not an object")
            history_payload = {
                "kid": payload.get("kid"),
                "jwk": payload.get("jwk"),
                "att": item.get("att"),
                "att_kind": item.get("att_kind"),
                "att_sha256": item.get("att_sha256"),
            }
            record = _record_from_payload(
                history_payload,
                plane=plane,
                seen_at=history_seen_at,
                history=True,
            )
        except ValueError as exc:
            result["skipped"] += 1
            logger.warning(
                "receipt_key_history_skipped host=%s ip=%s index=%d error=%s",
                target.host,
                target.connect_ip,
                index,
                exc,
            )
            continue
        # Storage faults are target failures, just as they are for the current
        # document.  Do not mislabel them as malformed enclave history.
        _observe_record(store, record, result, refresh_last_seen=False)


def collect_receipt_keys(
    settings: Settings,
    *,
    store: Store = STORE,
) -> dict[str, int]:
    """Collect one key from each live DNS endpoint without trusting failures."""

    targets = discover_receipt_key_targets(settings)
    result = {
        "discovered": len(targets),
        "fetched": 0,
        "appended": 0,
        "refreshed": 0,
        "skipped": 0,
        "errors": 0,
    }
    plane = urlsplit(settings.api_base_url).hostname or settings.trusted_domain
    for target in targets:
        try:
            payload = _fetch_receipt_key(
                target,
                verify_tls=not settings.synthetic_canonical_attested,
            )
            result["fetched"] += 1
            seen_at = iso_now()
            record = _record_from_payload(payload, plane=plane, seen_at=seen_at)
            _observe_record(store, record, result)
            _collect_history(
                payload,
                plane=plane,
                seen_at=seen_at,
                store=store,
                result=result,
                target=target,
            )
        except Exception as exc:
            # One bad or unreachable instance cannot suppress observations
            # from its siblings, but its key must fail closed.
            result["errors"] += 1
            logger.warning(
                "receipt_key_collection_failed host=%s ip=%s error=%s",
                target.host,
                target.connect_ip,
                exc,
            )
    return result
