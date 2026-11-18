"""Native BYOK credentials for Amazon Bedrock, Google Vertex and Azure.

These providers do not take one API-key string. A workspace uploads its
native credential as a JSON object; it is validated here, sealed in the same
BYOK envelope as any key, and opened only inside the gateway enclave, which
validates it again before use (enclave-go internal/llm/cloudcreds.go). The
rules here mirror the enclave's so a bad credential fails at upload, not on
the first request.

Accepted shapes:

* ``amazon-bedrock``: a Bedrock API key string, or
  ``{"bedrock_api_key", "region"}``, or
  ``{"access_key_id", "secret_access_key", "session_token"?, "region"}``.
* ``google-vertex``: a service-account key file, optionally wrapped as
  ``{"service_account": {...}, "location": "...", "project_id": "..."}``.
* ``azure``: ``{"endpoint", "api_key", "deployments"?}`` where endpoint is an
  Azure OpenAI / AI Foundry resource and deployments maps an upstream model id
  to the workspace's deployment name.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CLOUD_CREDENTIAL_PROVIDERS = frozenset({"amazon-bedrock", "google-vertex", "azure"})


_AWS_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-[0-9]$")
_AWS_ACCESS_KEY = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{12,124}$")
_GCP_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_GCP_LOCATION = re.compile(r"^([a-z]+-[a-z]+[0-9]+|global|us|eu)$")
_GCP_CLIENT_EMAIL = re.compile(r"^[a-zA-Z0-9._%+-]{1,128}@[a-z0-9.-]{1,253}$")
_AZURE_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,62}\.(openai\.azure\.com|services\.ai\.azure\.com|cognitiveservices\.azure\.com)$"
)
_AZURE_DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_CREDENTIAL_BYTES = 16 * 1024
_MAX_PRIVATE_KEY_PEM_CHARS = 4000


class CloudCredentialError(ValueError):
    """The upload is not a valid native credential; the message says why."""


def is_cloud_credential_provider(provider: str) -> bool:
    return provider in CLOUD_CREDENTIAL_PROVIDERS


def normalize_cloud_credential(provider: str, credential: str | dict[str, Any]) -> tuple[str, str]:
    """Return (the secret to seal, a display hint) for a cloud credential."""
    if provider == "amazon-bedrock":
        return _aws(credential)
    if provider == "google-vertex":
        return _gcp(credential)
    if provider == "azure":
        return _azure(credential)
    raise CloudCredentialError(f"{provider} does not take a native cloud credential")


def _no_whitespace(value: str) -> bool:
    return not any(ch.isspace() for ch in value)


def _as_object(credential: str | dict[str, Any], label: str) -> dict[str, Any]:
    if isinstance(credential, str):
        try:
            credential = json.loads(credential)
        except json.JSONDecodeError as exc:
            raise CloudCredentialError(f"{label} credential must be a JSON object") from exc
    if not isinstance(credential, dict):
        raise CloudCredentialError(f"{label} credential must be a JSON object")
    return credential


def _seal(document: dict[str, Any]) -> str:
    secret = json.dumps(document, separators=(",", ":"), sort_keys=True)
    if len(secret.encode()) > _MAX_CREDENTIAL_BYTES:
        raise CloudCredentialError("credential is too large")
    return secret


def _aws(credential: str | dict[str, Any]) -> tuple[str, str]:
    if isinstance(credential, str) and not credential.strip().startswith("{"):
        key = credential.strip()
        if not key or not _no_whitespace(key):
            raise CloudCredentialError("Bedrock API key is empty or malformed")
        return key, f"Bedrock API key {key[:4]}...{key[-4:]} (us-east-1)"
    raw = _as_object(credential, "Bedrock")
    unknown = set(raw) - {"bedrock_api_key", "access_key_id", "secret_access_key", "session_token", "region"}
    if unknown:
        raise CloudCredentialError(f"Bedrock credential has unknown fields: {', '.join(sorted(unknown))}")
    values = {name: str(raw.get(name) or "").strip() for name in raw}
    region = values.get("region") or "us-east-1"
    if not _AWS_REGION.match(region):
        raise CloudCredentialError("Bedrock region is invalid")
    api_key = values.get("bedrock_api_key", "")
    access_key = values.get("access_key_id", "")
    secret_key = values.get("secret_access_key", "")
    if bool(api_key) == bool(access_key or secret_key):
        raise CloudCredentialError("Bedrock credential needs exactly one of bedrock_api_key or access keys")
    if api_key:
        if not _no_whitespace(api_key):
            raise CloudCredentialError("Bedrock API key is malformed")
        return _seal({"bedrock_api_key": api_key, "region": region}), (
            f"Bedrock API key {api_key[:4]}...{api_key[-4:]} ({region})"
        )
    if not _AWS_ACCESS_KEY.match(access_key) or len(secret_key) < 16 or not _no_whitespace(secret_key):
        raise CloudCredentialError("Bedrock access keys are malformed")
    document = {"access_key_id": access_key, "secret_access_key": secret_key, "region": region}
    if values.get("session_token"):
        document["session_token"] = values["session_token"]
    return _seal(document), f"{access_key[:4]}...{access_key[-4:]} ({region})"


def _gcp(credential: str | dict[str, Any]) -> tuple[str, str]:
    outer = _as_object(credential, "Vertex")
    account = outer.get("service_account", outer)
    if not isinstance(account, dict):
        raise CloudCredentialError("Vertex service_account must be a JSON object")
    location = str(outer.get("location") or "global").strip()
    project_id = str(
        (outer.get("project_id") if "service_account" in outer else None) or account.get("project_id") or ""
    ).strip()
    client_email = str(account.get("client_email") or "").strip()
    if account.get("type") != "service_account":
        raise CloudCredentialError("Vertex credential must be a service account key")
    if not _GCP_PROJECT.match(project_id):
        raise CloudCredentialError("Vertex project_id is invalid")
    if not _GCP_LOCATION.match(location):
        raise CloudCredentialError("Vertex location is invalid")
    if not _GCP_CLIENT_EMAIL.match(client_email):
        raise CloudCredentialError("Vertex client_email is invalid")
    private_key = str(account.get("private_key") or "")
    # Bound the key before parsing: RSA validation costs grow fast with size
    # and run on an API worker. A 4096-bit PKCS#8 PEM is about 3.3 KB; Google
    # issues 2048-bit keys.
    if len(private_key) > _MAX_PRIVATE_KEY_PEM_CHARS:
        raise CloudCredentialError("Vertex private_key is too large; use a 2048-bit service account key")
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise CloudCredentialError("Vertex private_key is not a PEM private key") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or not 2048 <= key.key_size <= 4096:
        raise CloudCredentialError("Vertex private_key must be RSA of 2048 to 4096 bits")
    # The enclave never follows the key file's own token_uri, so it is not kept.
    sealed_account = {
        "type": "service_account",
        "project_id": project_id,
        "private_key_id": str(account.get("private_key_id") or ""),
        "private_key": private_key,
        "client_email": client_email,
    }
    return _seal({"service_account": sealed_account, "location": location, "project_id": project_id}), (
        f"{client_email} ({location})"
    )


def _azure(credential: str | dict[str, Any]) -> tuple[str, str]:
    raw = _as_object(credential, "Azure")
    unknown = set(raw) - {"endpoint", "api_key", "deployments"}
    if unknown:
        raise CloudCredentialError(f"Azure credential has unknown fields: {', '.join(sorted(unknown))}")
    host = _azure_host(str(raw.get("endpoint") or ""))
    api_key = str(raw.get("api_key") or "").strip()
    if len(api_key) < 16 or not _no_whitespace(api_key):
        raise CloudCredentialError("Azure api_key is malformed")
    deployments = raw.get("deployments") or {}
    if not isinstance(deployments, dict) or len(deployments) > 200:
        raise CloudCredentialError("Azure deployments must be an object of at most 200 entries")
    clean: dict[str, str] = {}
    for model, deployment in deployments.items():
        deployment = str(deployment).strip()
        if not _AZURE_DEPLOYMENT.match(deployment):
            raise CloudCredentialError("Azure deployment name is invalid")
        clean[str(model).strip()] = deployment
    document: dict[str, Any] = {"endpoint": f"https://{host}", "api_key": api_key}
    if clean:
        document["deployments"] = clean
    return _seal(document), f"{host} ...{api_key[-4:]}"


def _azure_host(endpoint: str) -> str:
    try:
        parsed = urlparse(endpoint.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        # A malformed port or bracketed host is a bad credential, not a 500.
        raise CloudCredentialError("Azure endpoint must be the https root of an Azure resource") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in ("", "/openai", "/openai/v1")
    ):
        raise CloudCredentialError("Azure endpoint must be the https root of an Azure resource")
    if not _AZURE_HOST.match(host):
        raise CloudCredentialError("Azure endpoint must be an Azure OpenAI or AI Foundry resource")
    return host
