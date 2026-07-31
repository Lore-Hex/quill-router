"""Architectural guard: cloud SDKs stay out of the infrastructure path.

The control plane must be able to run somewhere other than GCP. That is not
achieved by a one-time cleanup — it is achieved by keeping cloud SDKs out of
application code permanently. Route handlers, services and the app factory
were making control-flow decisions on Google exception types; each such import
quietly re-couples the process to one cloud and makes the Google client
libraries an install-time requirement everywhere.

The distinction this test draws is INFRASTRUCTURE vs VENDOR PRODUCT:

* **Infrastructure** — storage, secrets, key wrapping, retry/conflict
  classification. These must sit behind a port, because they are exactly what
  changes when the deployment moves cloud. Violations here block portability.
* **Vendor product APIs** — calling Vertex as an upstream LLM provider, or SES
  to send mail. These use a vendor SDK to talk to that vendor's service, which
  works identically from any cloud. They are ordinary third-party
  integrations, no different from the OpenAI SDK, and are NOT portability
  blockers.

So the allowlist below is not "known exceptions we tolerate" — it is the set
of modules whose job legitimately involves a cloud SDK. Anything else fails
here with a pointer to the port it should use. Adding an entry is deliberate,
and visible in a diff.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "trusted_router"

#: Modules permitted to import a cloud SDK, and why.
ALLOWED = {
    # The GCP storage adapter: implementing SpannerBigtableStore is exactly
    # what these exist to do.
    "storage_gcp.py",
    "storage_gcp_authorize.py",
    "storage_gcp_google_ads.py",
    "storage_gcp_io.py",
    "storage_gcp_settle_outbox.py",
    "storage_gcp_synthetic_rollups.py",
    "storage_gcp_synthetic_index.py",
    # The two explicit cloud ports. Both import lazily so a non-GCP deployment
    # need not install the Google libraries at all.
    "storage_errors.py",
    "key_management.py",
    # VENDOR PRODUCT APIs, not infrastructure — see the module docstring.
    # google.auth here mints an access token for Vertex AI as an upstream LLM
    # PROVIDER. Routing to Vertex requires Google credentials no matter which
    # cloud we run on, exactly as routing to OpenAI requires an OpenAI key.
    "providers.py",
    # boto3 talks to Amazon SES to send transactional mail. SES is reachable
    # from any cloud; this is a vendor choice, not a deployment coupling.
    "services/email.py",
}

CLOUD_SDK_ROOTS = {"google", "boto3", "botocore", "azure"}


def _cloud_sdk_imports(path: Path) -> set[str]:
    """Top-level package names of any cloud SDK imported by `path`.

    Walks the AST rather than grepping so that a name inside a string or
    comment (e.g. the provider slug "google-ai-studio") is not mistaken for an
    import.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in CLOUD_SDK_ROOTS:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in CLOUD_SDK_ROOTS:
                found.add(root)
    return found


def test_cloud_sdks_are_confined_to_the_adapter_layer() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED or path.name in ALLOWED:
            continue
        imports = _cloud_sdk_imports(path)
        if imports:
            violations.append(f"{rel} imports {sorted(imports)}")
    assert not violations, (
        "Cloud SDK imported outside the storage adapter layer:\n  "
        + "\n  ".join(violations)
        + "\n\nUse a port instead: storage_errors (retry/conflict classification), "
        "key_management (envelope key wrapping), or the Store protocol. "
        "If this really is a new adapter, add it to ALLOWED with a reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlist that outlives its reason silently widens the boundary."""
    stale = [name for name in ALLOWED if not (SRC / name).exists()]
    assert not stale, f"ALLOWED lists modules that no longer exist: {stale}"
