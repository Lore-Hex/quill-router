#!/usr/bin/env python3
"""Capture and re-verify the public GCP rollout frontend contract.

The artifact intentionally contains only resource identities and normalized,
non-secret state.  Both commands are read-only with respect to GCP and DNS;
``capture`` is the only command that writes, and it writes only its artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
SMOKE_RELATIVE_PATH = "scripts/deploy/rollout_smoke.sh"
MAX_PROVIDER_OUTPUT = 4 * 1024 * 1024
MAX_ARTIFACT_SIZE = 2 * 1024 * 1024
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
RESOURCE_RE = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REQUIRED_HOSTS = (
    "trustedrouter.com",
    "www.trustedrouter.com",
    "status.trustedrouter.com",
    "trust.trustedrouter.com",
    "eu.trustedrouter.com",
    "status-us.trustedrouter.com",
    "status-eu.trustedrouter.com",
    "allyrouter.com",
    "www.allyrouter.com",
    "status.allyrouter.com",
    "trust.allyrouter.com",
    "uptimerouter.com",
    "www.uptimerouter.com",
    "status.uptimerouter.com",
    "trust.uptimerouter.com",
)


class AttestationError(ValueError):
    """A fail-closed frontend attestation error."""


def _exact_dict(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AttestationError(f"{label} fields differ from schema v1")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{label} is invalid")
    return value


def _project(value: Any) -> str:
    value = _string(value, "project ID")
    if not PROJECT_RE.fullmatch(value):
        raise AttestationError("project ID is invalid")
    return value


def _resource_name(value: Any, label: str) -> str:
    value = _string(value, label)
    if not RESOURCE_RE.fullmatch(value):
        raise AttestationError(f"{label} is invalid")
    return value


def _host(value: Any, *, pattern: bool = False) -> str:
    value = _string(value, "hostname")
    wildcard = pattern and value.startswith("*.")
    candidate = value[2:] if wildcard else value
    if value != value.lower() or len(candidate) > 253 or candidate.endswith("."):
        raise AttestationError("hostname is invalid")
    labels = candidate.split(".")
    if len(labels) < 2:
        raise AttestationError("hostname is invalid")
    for label in labels:
        if not 1 <= len(label) <= 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label):
            raise AttestationError("hostname is invalid")
    return value


def _hosts(value: str) -> list[str]:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AttestationError("host inventory is invalid")
    hosts = value.split(",")
    if not 1 <= len(hosts) <= 32 or any(not item for item in hosts):
        raise AttestationError("host inventory is invalid")
    normalized = [_host(item) for item in hosts]
    if len(set(normalized)) != len(normalized):
        raise AttestationError("host inventory contains duplicates")
    if normalized != list(REQUIRED_HOSTS):
        raise AttestationError("host inventory differs from the managed-domain contract")
    return normalized


def _compute_resource(project: str, collection: str, name: str) -> str:
    return f"projects/{project}/global/{collection}/{name}"


def _certificate_manager_resource(project: str, collection: str, name: str) -> str:
    return f"projects/{project}/locations/global/{collection}/{name}"


def _reference_path(value: Any, *, api: str) -> str:
    value = _string(value, "provider resource reference")
    if value.startswith("projects/"):
        return value
    if value.startswith("//"):
        parsed = value[2:].split("/", 1)
        expected_service = (
            "certificatemanager.googleapis.com" if api == "certificate-manager" else None
        )
        if len(parsed) != 2 or parsed[0] != expected_service:
            raise AttestationError("provider resource reference is invalid")
        return parsed[1]
    parsed_url = urlsplit(value)
    if parsed_url.scheme != "https" or parsed_url.query or parsed_url.fragment:
        raise AttestationError("provider resource reference is invalid")
    if api == "compute":
        if parsed_url.netloc not in {
            "www.googleapis.com",
            "compute.googleapis.com",
        } or not parsed_url.path.startswith("/compute/v1/"):
            raise AttestationError("provider resource reference is invalid")
        return parsed_url.path.removeprefix("/compute/v1/")
    if parsed_url.netloc != "certificatemanager.googleapis.com" or not parsed_url.path.startswith(
        "/v1/"
    ):
        raise AttestationError("provider resource reference is invalid")
    return parsed_url.path.removeprefix("/v1/")


def _compute_ref(
    value: Any,
    project: str,
    collection: str,
    *,
    expected_name: str | None = None,
) -> tuple[str, str]:
    path = _reference_path(value, api="compute")
    prefix = f"projects/{project}/global/{collection}/"
    if not path.startswith(prefix) or "/" in path.removeprefix(prefix):
        raise AttestationError("compute resource identity differs")
    name = _resource_name(path.removeprefix(prefix), "compute resource name")
    if expected_name is not None and name != expected_name:
        raise AttestationError("compute resource identity differs")
    return _compute_resource(project, collection, name), name


def _certificate_manager_ref(
    value: Any,
    project: str,
    collection: str,
    *,
    expected_name: str | None = None,
) -> tuple[str, str]:
    path = _reference_path(value, api="certificate-manager")
    prefix = f"projects/{project}/locations/global/{collection}/"
    if not path.startswith(prefix) or "/" in path.removeprefix(prefix):
        raise AttestationError("Certificate Manager resource identity differs")
    name = _resource_name(path.removeprefix(prefix), "Certificate Manager resource name")
    if expected_name is not None and name != expected_name:
        raise AttestationError("Certificate Manager resource identity differs")
    return _certificate_manager_resource(project, collection, name), name


def _map_entry_ref(value: Any, project: str, map_name: str) -> tuple[str, str]:
    path = _reference_path(value, api="certificate-manager")
    prefix = (
        f"projects/{project}/locations/global/certificateMaps/{map_name}/certificateMapEntries/"
    )
    if not path.startswith(prefix) or "/" in path.removeprefix(prefix):
        raise AttestationError("certificate-map entry identity differs")
    name = _resource_name(path.removeprefix(prefix), "certificate-map entry name")
    return f"{prefix}{name}", name


def _run_read_only(argv: list[str], *, label: str) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            "LC_ALL": "C",
        }
    )
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, never a shell
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=environment,
        )
    except FileNotFoundError as error:
        raise AttestationError(f"required read-only tool is unavailable: {label}") from error
    except subprocess.TimeoutExpired as error:
        raise AttestationError(f"read-only query timed out: {label}") from error
    if result.returncode != 0:
        raise AttestationError(f"read-only query failed: {label}")
    if len(result.stdout) > MAX_PROVIDER_OUTPUT:
        raise AttestationError(f"read-only query output is too large: {label}")
    return result.stdout


def _gcloud(arguments: list[str], *, expected: type[dict] | type[list], label: str) -> Any:
    raw = _run_read_only(["gcloud", *arguments, "--format=json"], label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationError(f"provider returned malformed JSON: {label}") from error
    if not isinstance(value, expected):
        raise AttestationError(f"provider returned the wrong JSON shape: {label}")
    return value


def _dig(host: str, record_type: str) -> list[str]:
    raw = _run_read_only(
        ["dig", "+time=5", "+tries=1", "+short", host, record_type],
        label=f"DNS {record_type}",
    )
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise AttestationError("DNS response is malformed") from error
    addresses: set[str] = set()
    version = 4 if record_type == "A" else 6
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as error:
            # CNAMEs and any other non-address answer are fail-closed.  The
            # rollout domains must resolve directly to the attested VIP.
            raise AttestationError("DNS response contains a non-address answer") from error
        if address.version != version:
            raise AttestationError("DNS response contains the wrong address family")
        addresses.add(address.compressed)
    return sorted(addresses, key=lambda item: ipaddress.ip_address(item).packed)


def _provider_name(resource: dict[str, Any], expected: str, label: str) -> None:
    if resource.get("name") != expected:
        raise AttestationError(f"{label} name differs")


def _provider_compute_self_link(
    resource: dict[str, Any], project: str, collection: str, name: str
) -> str:
    canonical, _ = _compute_ref(resource.get("selfLink"), project, collection, expected_name=name)
    return canonical


def _normalized_ports(forwarding_rule: dict[str, Any]) -> list[str]:
    if forwarding_rule.get("allPorts") not in (None, False):
        raise AttestationError("forwarding rule exposes ports other than TCP/443")
    has_range = "portRange" in forwarding_rule and forwarding_rule.get("portRange") is not None
    has_ports = "ports" in forwarding_rule and forwarding_rule.get("ports") is not None
    if has_range == has_ports:
        raise AttestationError("forwarding rule port contract is ambiguous")
    if has_range:
        if forwarding_rule["portRange"] not in {"443", "443-443"}:
            raise AttestationError("forwarding rule does not expose exactly TCP/443")
    else:
        if forwarding_rule["ports"] != ["443"]:
            raise AttestationError("forwarding rule does not expose exactly TCP/443")
    return ["443"]


def _certificate_domains(
    resource: dict[str, Any], managed: Any, san_field: str, label: str
) -> list[str]:
    if not isinstance(managed, dict) or managed.get("status", managed.get("state")) != "ACTIVE":
        raise AttestationError(f"{label} is not ACTIVE")
    configured_domains = managed.get("domains")
    san_domains = resource.get(san_field)
    if (
        not isinstance(configured_domains, list)
        or not configured_domains
        or not isinstance(san_domains, list)
        or not san_domains
    ):
        raise AttestationError(f"{label} hostname inventory is invalid")
    normalized_configured = sorted(_host(item, pattern=True) for item in configured_domains)
    normalized_sans = sorted(_host(item, pattern=True) for item in san_domains)
    if (
        len(set(normalized_configured)) != len(normalized_configured)
        or len(set(normalized_sans)) != len(normalized_sans)
        or normalized_configured != normalized_sans
    ):
        raise AttestationError(f"{label} hostname inventory is inconsistent")
    return normalized_sans


def _pattern_covers(pattern: str, host: str) -> bool:
    if pattern == host:
        return True
    if not pattern.startswith("*."):
        return False
    suffix = pattern[2:]
    return host.endswith(f".{suffix}") and len(host.split(".")) == len(suffix.split(".")) + 1


def _select_certificate_map_entry(
    entries: list[dict[str, Any]], host: str
) -> dict[str, Any]:
    """Select the one Certificate Manager entry that GCP will use for *host*.

    Certificate maps do not union certificates across all matching entries.  An
    exact hostname shadows wildcard entries, the most-specific wildcard shadows
    PRIMARY, and PRIMARY is used only when no hostname selector matches.  Treat
    ties at the effective precedence as ambiguous instead of blessing one based
    on provider response order.
    """

    exact = [entry for entry in entries if entry["hostname"] == host]
    if exact:
        selected = exact
    else:
        wildcards = [
            entry
            for entry in entries
            if isinstance(entry["hostname"], str)
            and entry["hostname"].startswith("*.")
            and _pattern_covers(entry["hostname"], host)
        ]
        if wildcards:
            most_specific = max(len(entry["hostname"][2:].split(".")) for entry in wildcards)
            selected = [
                entry
                for entry in wildcards
                if len(entry["hostname"][2:].split(".")) == most_specific
            ]
        else:
            selected = [entry for entry in entries if entry["matcher"] == "PRIMARY"]
    if len(selected) != 1:
        raise AttestationError("certificate-map selector is missing or ambiguous")
    return selected[0]


def _capture_compute_certificate(project: str, reference: str) -> dict[str, Any]:
    canonical, name = _compute_ref(reference, project, "sslCertificates")
    response = _gcloud(
        [
            "compute",
            "ssl-certificates",
            "describe",
            name,
            "--global",
            f"--project={project}",
        ],
        expected=dict,
        label="managed SSL certificate",
    )
    _provider_name(response, name, "managed SSL certificate")
    _provider_compute_self_link(response, project, "sslCertificates", name)
    if response.get("type") != "MANAGED":
        raise AttestationError("direct SSL certificate is not Google-managed")
    return {
        "resource": canonical,
        "provider": "compute-managed",
        "state": "ACTIVE",
        "domains": _certificate_domains(
            response,
            response.get("managed"),
            "subjectAlternativeNames",
            "managed SSL certificate",
        ),
    }


def _capture_certificate_manager_certificate(project: str, reference: str) -> dict[str, Any]:
    canonical, name = _certificate_manager_ref(reference, project, "certificates")
    response = _gcloud(
        [
            "certificate-manager",
            "certificates",
            "describe",
            name,
            "--location=global",
            f"--project={project}",
        ],
        expected=dict,
        label="Certificate Manager certificate",
    )
    response_canonical, _ = _certificate_manager_ref(
        response.get("name"), project, "certificates", expected_name=name
    )
    if response_canonical != canonical:
        raise AttestationError("Certificate Manager certificate identity differs")
    return {
        "resource": canonical,
        "provider": "certificate-manager-managed",
        "state": "ACTIVE",
        "domains": _certificate_domains(
            response,
            response.get("managed"),
            "sanDnsnames",
            "Certificate Manager certificate",
        ),
    }


def _capture_direct_binding(
    project: str, references: list[Any], hosts: list[str]
) -> dict[str, Any]:
    if not references or not all(isinstance(item, str) for item in references):
        raise AttestationError("direct certificate binding is invalid")
    certificates = [_capture_compute_certificate(project, item) for item in references]
    certificates.sort(key=lambda item: item["resource"])
    if len({item["resource"] for item in certificates}) != len(certificates):
        raise AttestationError("direct certificate binding contains duplicates")
    for host in hosts:
        if not any(
            _pattern_covers(domain, host)
            for certificate in certificates
            for domain in certificate["domains"]
        ):
            raise AttestationError("an attested host lacks ACTIVE certificate coverage")
    return {
        "mode": "direct",
        "certificate_map": None,
        "entries": [],
        "certificates": certificates,
    }


def _capture_map_binding(project: str, reference: str, hosts: list[str]) -> dict[str, Any]:
    canonical_map, map_name = _certificate_manager_ref(reference, project, "certificateMaps")
    map_response = _gcloud(
        [
            "certificate-manager",
            "maps",
            "describe",
            map_name,
            "--location=global",
            f"--project={project}",
        ],
        expected=dict,
        label="certificate map",
    )
    observed_map, _ = _certificate_manager_ref(
        map_response.get("name"), project, "certificateMaps", expected_name=map_name
    )
    if observed_map != canonical_map:
        raise AttestationError("certificate map identity differs")

    raw_entries = _gcloud(
        [
            "certificate-manager",
            "maps",
            "entries",
            "list",
            f"--map={map_name}",
            "--location=global",
            f"--project={project}",
        ],
        expected=list,
        label="certificate-map entries",
    )
    if not raw_entries:
        raise AttestationError("certificate map has no entries")
    entries: list[dict[str, Any]] = []
    certificate_references: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise AttestationError("certificate-map entry is malformed")
        entry_resource, _ = _map_entry_ref(raw_entry.get("name"), project, map_name)
        if raw_entry.get("state") != "ACTIVE":
            raise AttestationError("certificate-map entry is not ACTIVE")
        hostname = raw_entry.get("hostname")
        matcher = raw_entry.get("matcher")
        if hostname is not None:
            hostname = _host(hostname, pattern=True)
            if matcher is not None:
                raise AttestationError("certificate-map entry selector is ambiguous")
        elif matcher != "PRIMARY":
            raise AttestationError("certificate-map entry selector is invalid")
        raw_certificates = raw_entry.get("certificates")
        if not isinstance(raw_certificates, list) or not raw_certificates:
            raise AttestationError("certificate-map entry has no certificates")
        entry_certificates: list[str] = []
        for raw_certificate in raw_certificates:
            certificate_resource, _ = _certificate_manager_ref(
                raw_certificate, project, "certificates"
            )
            entry_certificates.append(certificate_resource)
            certificate_references.add(certificate_resource)
        entry_certificates.sort()
        if len(set(entry_certificates)) != len(entry_certificates):
            raise AttestationError("certificate-map entry contains duplicate certificates")
        entries.append(
            {
                "resource": entry_resource,
                "state": "ACTIVE",
                "hostname": hostname,
                "matcher": matcher,
                "certificates": entry_certificates,
            }
        )
    entries.sort(key=lambda item: item["resource"])
    if len({item["resource"] for item in entries}) != len(entries):
        raise AttestationError("certificate map contains duplicate entry identities")

    certificates = [
        _capture_certificate_manager_certificate(project, reference)
        for reference in sorted(certificate_references)
    ]
    by_resource = {item["resource"]: item for item in certificates}
    for host in hosts:
        selected_entry = _select_certificate_map_entry(entries, host)
        if not any(
            _pattern_covers(domain, host)
            for certificate_resource in selected_entry["certificates"]
            for domain in by_resource[certificate_resource]["domains"]
        ):
            raise AttestationError("an attested host lacks ACTIVE certificate-map coverage")
    return {
        "mode": "certificate-map",
        "certificate_map": canonical_map,
        "entries": entries,
        "certificates": certificates,
    }


def _capture_frontend(request: dict[str, Any]) -> dict[str, Any]:
    project = request["project_id"]
    forwarding_name = request["forwarding_rule"]
    proxy_name = request["https_proxy"]
    map_name = request["url_map"]
    hosts = request["hosts"]

    forwarding = _gcloud(
        [
            "compute",
            "forwarding-rules",
            "describe",
            forwarding_name,
            "--global",
            f"--project={project}",
        ],
        expected=dict,
        label="global forwarding rule",
    )
    _provider_name(forwarding, forwarding_name, "global forwarding rule")
    forwarding_resource = _provider_compute_self_link(
        forwarding, project, "forwardingRules", forwarding_name
    )
    proxy_resource, _ = _compute_ref(
        forwarding.get("target"), project, "targetHttpsProxies", expected_name=proxy_name
    )
    ip_value = _string(forwarding.get("IPAddress"), "forwarding-rule VIP")
    try:
        vip = ipaddress.ip_address(ip_value).compressed
    except ValueError as error:
        raise AttestationError("forwarding-rule VIP is not an IP address") from error
    if forwarding.get("IPProtocol") != "TCP":
        raise AttestationError("forwarding rule protocol is not TCP")
    if forwarding.get("networkTier") != "PREMIUM":
        raise AttestationError("forwarding rule network tier is not PREMIUM")
    if forwarding.get("loadBalancingScheme") != "EXTERNAL_MANAGED":
        raise AttestationError("forwarding rule is not EXTERNAL_MANAGED")
    ports = _normalized_ports(forwarding)

    proxy = _gcloud(
        [
            "compute",
            "target-https-proxies",
            "describe",
            proxy_name,
            "--global",
            f"--project={project}",
        ],
        expected=dict,
        label="global target HTTPS proxy",
    )
    _provider_name(proxy, proxy_name, "global target HTTPS proxy")
    observed_proxy_resource = _provider_compute_self_link(
        proxy, project, "targetHttpsProxies", proxy_name
    )
    if observed_proxy_resource != proxy_resource:
        raise AttestationError("forwarding rule target HTTPS proxy differs")
    url_map_resource, _ = _compute_ref(
        proxy.get("urlMap"), project, "urlMaps", expected_name=map_name
    )

    url_map = _gcloud(
        [
            "compute",
            "url-maps",
            "describe",
            map_name,
            "--global",
            f"--project={project}",
        ],
        expected=dict,
        label="global URL map",
    )
    _provider_name(url_map, map_name, "global URL map")
    if _provider_compute_self_link(url_map, project, "urlMaps", map_name) != url_map_resource:
        raise AttestationError("target HTTPS proxy URL map differs")

    certificate_map = proxy.get("certificateMap")
    ssl_certificates = proxy.get("sslCertificates")
    has_map = isinstance(certificate_map, str) and bool(certificate_map)
    has_direct = isinstance(ssl_certificates, list) and bool(ssl_certificates)
    if has_map == has_direct:
        raise AttestationError("HTTPS proxy certificate binding mode is ambiguous")
    if has_map:
        if ssl_certificates not in (None, []):
            raise AttestationError("HTTPS proxy certificate binding mode is ambiguous")
        binding = _capture_map_binding(project, certificate_map, hosts)
    else:
        if certificate_map is not None:
            raise AttestationError("HTTPS proxy certificate binding mode is ambiguous")
        binding = _capture_direct_binding(project, ssl_certificates, hosts)

    dns: list[dict[str, Any]] = []
    expected_a = [vip] if ipaddress.ip_address(vip).version == 4 else []
    expected_aaaa = [vip] if ipaddress.ip_address(vip).version == 6 else []
    for host in hosts:
        records_a = _dig(host, "A")
        records_aaaa = _dig(host, "AAAA")
        if records_a != expected_a or records_aaaa != expected_aaaa:
            raise AttestationError("public DNS does not resolve exactly to the attested VIP")
        dns.append({"host": host, "a": records_a, "aaaa": records_aaaa})

    return {
        "forwarding_rule": {
            "resource": forwarding_resource,
            "ip_address": vip,
            "ip_protocol": "TCP",
            "ports": ports,
            "network_tier": "PREMIUM",
            "load_balancing_scheme": "EXTERNAL_MANAGED",
            "target_https_proxy": proxy_resource,
        },
        "https_proxy": {
            "resource": proxy_resource,
            "url_map": url_map_resource,
        },
        "url_map": {"resource": url_map_resource},
        "certificate_binding": binding,
        "dns": dns,
    }


def _smoke_path() -> Path:
    return Path(__file__).resolve().parents[2] / SMOKE_RELATIVE_PATH


def _smoke_identity() -> dict[str, str]:
    path = _smoke_path()
    try:
        info = path.lstat()
    except OSError as error:
        raise AttestationError("repository-owned rollout smoke script is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise AttestationError("repository-owned rollout smoke script is not a regular file")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise AttestationError("repository-owned rollout smoke script cannot be read") from error
    return {"path": SMOKE_RELATIVE_PATH, "sha256": digest}


def _request(
    *, project: str, forwarding_rule: str, https_proxy: str, url_map: str, hosts: str
) -> dict[str, Any]:
    return {
        "project_id": _project(project),
        "forwarding_rule": _resource_name(forwarding_rule, "forwarding-rule name"),
        "https_proxy": _resource_name(https_proxy, "HTTPS-proxy name"),
        "url_map": _resource_name(url_map, "URL-map name"),
        "hosts": _hosts(hosts),
    }


def _validate_request_artifact(value: Any) -> dict[str, Any]:
    request = _exact_dict(
        value,
        {"project_id", "forwarding_rule", "https_proxy", "url_map", "hosts"},
        "attestation request",
    )
    hosts = request["hosts"]
    if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
        raise AttestationError("artifact host inventory is invalid")
    rebuilt = _request(
        project=request["project_id"],
        forwarding_rule=request["forwarding_rule"],
        https_proxy=request["https_proxy"],
        url_map=request["url_map"],
        hosts=",".join(hosts),
    )
    if rebuilt != request:
        raise AttestationError("attestation request is not canonical")
    return request


def _validate_certificate_artifact(value: Any, project: str, hosts: list[str]) -> None:
    binding = _exact_dict(
        value,
        {"mode", "certificate_map", "entries", "certificates"},
        "certificate binding",
    )
    if binding["mode"] not in {"direct", "certificate-map"}:
        raise AttestationError("certificate binding mode is invalid")
    if not isinstance(binding["entries"], list) or not isinstance(binding["certificates"], list):
        raise AttestationError("certificate binding inventory is invalid")
    certificate_resources: list[str] = []
    certificate_domains: dict[str, list[str]] = {}
    expected_provider = (
        "compute-managed" if binding["mode"] == "direct" else "certificate-manager-managed"
    )
    for certificate in binding["certificates"]:
        certificate = _exact_dict(
            certificate,
            {"resource", "provider", "state", "domains"},
            "certificate",
        )
        if certificate["provider"] != expected_provider or certificate["state"] != "ACTIVE":
            raise AttestationError("certificate identity or state is invalid")
        if binding["mode"] == "direct":
            canonical, _ = _compute_ref(certificate["resource"], project, "sslCertificates")
        else:
            canonical, _ = _certificate_manager_ref(
                certificate["resource"], project, "certificates"
            )
        if canonical != certificate["resource"]:
            raise AttestationError("certificate resource is not canonical")
        domains = certificate["domains"]
        if not isinstance(domains, list) or not domains:
            raise AttestationError("certificate hostname inventory is invalid")
        normalized_domains = sorted(_host(item, pattern=True) for item in domains)
        if normalized_domains != domains or len(set(domains)) != len(domains):
            raise AttestationError("certificate hostname inventory is not canonical")
        certificate_resources.append(canonical)
        certificate_domains[canonical] = domains
    if certificate_resources != sorted(certificate_resources) or len(
        set(certificate_resources)
    ) != len(certificate_resources):
        raise AttestationError("certificate inventory is not canonical")

    if binding["mode"] == "direct":
        if binding["certificate_map"] is not None or binding["entries"]:
            raise AttestationError("direct certificate binding is invalid")
        for host in hosts:
            if not any(
                _pattern_covers(domain, host)
                for domains in certificate_domains.values()
                for domain in domains
            ):
                raise AttestationError("artifact certificate coverage is incomplete")
        return

    canonical_map, map_name = _certificate_manager_ref(
        binding["certificate_map"], project, "certificateMaps"
    )
    if canonical_map != binding["certificate_map"]:
        raise AttestationError("certificate map resource is not canonical")
    entry_resources: list[str] = []
    validated_entries: list[dict[str, Any]] = []
    for entry in binding["entries"]:
        entry = _exact_dict(
            entry,
            {"resource", "state", "hostname", "matcher", "certificates"},
            "certificate-map entry",
        )
        canonical_entry, _ = _map_entry_ref(entry["resource"], project, map_name)
        if canonical_entry != entry["resource"] or entry["state"] != "ACTIVE":
            raise AttestationError("certificate-map entry identity or state is invalid")
        if entry["hostname"] is not None:
            if _host(entry["hostname"], pattern=True) != entry["hostname"]:
                raise AttestationError("certificate-map hostname is not canonical")
            if entry["matcher"] is not None:
                raise AttestationError("certificate-map selector is ambiguous")
        elif entry["matcher"] != "PRIMARY":
            raise AttestationError("certificate-map selector is invalid")
        references = entry["certificates"]
        if (
            not isinstance(references, list)
            or not references
            or references != sorted(references)
            or len(set(references)) != len(references)
            or any(reference not in certificate_domains for reference in references)
        ):
            raise AttestationError("certificate-map entry certificate inventory is invalid")
        entry_resources.append(canonical_entry)
        validated_entries.append(entry)
    if entry_resources != sorted(entry_resources) or len(set(entry_resources)) != len(
        entry_resources
    ):
        raise AttestationError("certificate-map entry inventory is not canonical")
    for host in hosts:
        selected = _select_certificate_map_entry(validated_entries, host)
        if not any(
            _pattern_covers(domain, host)
            for certificate in selected["certificates"]
            for domain in certificate_domains[certificate]
        ):
            raise AttestationError("artifact certificate-map coverage is incomplete")


def _validate_frontend_artifact(value: Any, request: dict[str, Any]) -> None:
    frontend = _exact_dict(
        value,
        {"forwarding_rule", "https_proxy", "url_map", "certificate_binding", "dns"},
        "frontend attestation",
    )
    project = request["project_id"]
    expected_forwarding = _compute_resource(project, "forwardingRules", request["forwarding_rule"])
    expected_proxy = _compute_resource(project, "targetHttpsProxies", request["https_proxy"])
    expected_map = _compute_resource(project, "urlMaps", request["url_map"])
    forwarding = _exact_dict(
        frontend["forwarding_rule"],
        {
            "resource",
            "ip_address",
            "ip_protocol",
            "ports",
            "network_tier",
            "load_balancing_scheme",
            "target_https_proxy",
        },
        "forwarding-rule attestation",
    )
    if (
        forwarding["resource"] != expected_forwarding
        or forwarding["target_https_proxy"] != expected_proxy
        or forwarding["ip_protocol"] != "TCP"
        or forwarding["ports"] != ["443"]
        or forwarding["network_tier"] != "PREMIUM"
        or forwarding["load_balancing_scheme"] != "EXTERNAL_MANAGED"
    ):
        raise AttestationError("forwarding-rule artifact contract differs")
    try:
        vip = ipaddress.ip_address(forwarding["ip_address"]).compressed
    except (TypeError, ValueError) as error:
        raise AttestationError("artifact VIP is invalid") from error
    if vip != forwarding["ip_address"]:
        raise AttestationError("artifact VIP is not canonical")
    proxy = _exact_dict(frontend["https_proxy"], {"resource", "url_map"}, "HTTPS-proxy attestation")
    url_map = _exact_dict(frontend["url_map"], {"resource"}, "URL-map attestation")
    if proxy != {"resource": expected_proxy, "url_map": expected_map} or url_map != {
        "resource": expected_map
    }:
        raise AttestationError("HTTPS-proxy or URL-map artifact contract differs")
    _validate_certificate_artifact(frontend["certificate_binding"], project, request["hosts"])
    dns = frontend["dns"]
    if not isinstance(dns, list) or len(dns) != len(request["hosts"]):
        raise AttestationError("artifact DNS inventory is invalid")
    expected_a = [vip] if ipaddress.ip_address(vip).version == 4 else []
    expected_aaaa = [vip] if ipaddress.ip_address(vip).version == 6 else []
    for item, host in zip(dns, request["hosts"], strict=True):
        item = _exact_dict(item, {"host", "a", "aaaa"}, "DNS attestation")
        if item != {"host": host, "a": expected_a, "aaaa": expected_aaaa}:
            raise AttestationError("artifact DNS contract differs")


def _validate_artifact(value: Any) -> dict[str, Any]:
    artifact = _exact_dict(
        value, {"schema_version", "request", "frontend", "smoke"}, "frontend artifact"
    )
    if artifact["schema_version"] != SCHEMA_VERSION:
        raise AttestationError("frontend artifact schema version differs")
    request = _validate_request_artifact(artifact["request"])
    _validate_frontend_artifact(artifact["frontend"], request)
    smoke = _exact_dict(artifact["smoke"], {"path", "sha256"}, "smoke identity")
    if (
        smoke["path"] != SMOKE_RELATIVE_PATH
        or not isinstance(smoke["sha256"], str)
        or not SHA256_RE.fullmatch(smoke["sha256"])
    ):
        raise AttestationError("smoke identity is invalid")
    return artifact


def _read_artifact(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as error:
        raise AttestationError("frontend artifact is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise AttestationError("frontend artifact must be a regular mode-0600 file")
    if info.st_size > MAX_ARTIFACT_SIZE:
        raise AttestationError("frontend artifact is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationError("frontend artifact JSON is malformed") from error
    return _validate_artifact(value)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    smoke_path = _smoke_path().resolve()
    own_path = Path(__file__).resolve()
    destination = path.resolve(strict=False)
    if destination in {smoke_path, own_path}:
        raise AttestationError("artifact destination aliases a repository-owned executable")
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise AttestationError("artifact destination is not a regular file")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except AttestationError:
        raise
    except OSError as error:
        raise AttestationError("artifact destination cannot be prepared") from error
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if stat.S_IMODE(path.lstat().st_mode) != 0o600:
            raise AttestationError("frontend artifact mode differs after atomic write")
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _capture(args: argparse.Namespace) -> None:
    request = _request(
        project=args.project,
        forwarding_rule=args.forwarding_rule,
        https_proxy=args.https_proxy,
        url_map=args.url_map,
        hosts=args.hosts,
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "request": request,
        "frontend": _capture_frontend(request),
        "smoke": _smoke_identity(),
    }
    _validate_artifact(artifact)
    _atomic_write(args.artifact, artifact)
    print("frontend attestation captured", file=sys.stderr)


def _verify(args: argparse.Namespace) -> None:
    artifact = _read_artifact(args.artifact)
    if artifact["smoke"] != _smoke_identity():
        raise AttestationError("repository-owned rollout smoke script hash differs")
    current_frontend = _capture_frontend(artifact["request"])
    if current_frontend != artifact["frontend"]:
        raise AttestationError("live frontend contract differs from the captured artifact")
    print("frontend attestation verified", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and verify the public GCP rollout frontend attestation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--project", required=True)
    capture.add_argument("--forwarding-rule", required=True)
    capture.add_argument("--https-proxy", required=True)
    capture.add_argument("--url-map", required=True)
    capture.add_argument("--hosts", required=True)
    capture.add_argument("--artifact", type=Path, required=True)
    capture.set_defaults(handler=_capture)
    verify = commands.add_parser("verify-artifact")
    verify.add_argument("artifact", type=Path)
    verify.set_defaults(handler=_verify)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        args.handler(args)
    except AttestationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except OSError:
        # Provider output and artifact content are deliberately never reflected
        # in logs, including unusual filesystem failures.
        print("ERROR: frontend attestation filesystem operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
