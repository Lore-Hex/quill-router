from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any

from trusted_router.config import Settings
from trusted_router.domains import (
    api_base_url_for_domain,
    canonical_public_url,
    configured_control_domains,
)

ATTESTED_GATEWAY_REPO = "https://github.com/Lore-Hex/quill-cloud-proxy"
CLOUD_INFRA_REPO = "https://github.com/Lore-Hex/quill-cloud-infra"
CONTROL_PLANE_REPO = "https://github.com/Lore-Hex/quill-router"
QUILL_REPO = "https://github.com/Lore-Hex/quill"
PYTHON_SDK_REPO = "https://github.com/Lore-Hex/trusted-router-py"
JAVASCRIPT_SDK_REPO = "https://github.com/Lore-Hex/trusted-router-js"
PROVIDER_CHECK_REPO = "https://github.com/Lore-Hex/trustedrouter-provider-check"


NOT_CONFIGURED = "not-configured"

AWS_API_HOSTNAME = "api-aws.trustedrouter.com"
AZURE_API_HOSTNAME = "api-azure.trustedrouter.com"

# Nitro attestation documents are COSE_Sign1 signed by AWS's own PKI. There is
# no issuer URL to compare the way GCP's JWT has one; the check is a chain to
# this published root.
AWS_ATTESTATION_ROOT = "https://aws-nitro-enclaves.amazonaws.com/AWS_NitroEnclaves_Root-G1.zip"


def gcp_api_hostname(settings: Settings) -> str:
    """The hostname that terminates the GCP prompt path.

    A property of the plane, not of whoever is serving the record. Any control
    plane — GCP, AWS or Azure hosted — describing the GCP plane must name this.
    """
    return f"api.{configured_control_domains(settings)[0]}"


def gcp_api_base_url(settings: Settings) -> str:
    return f"https://{gcp_api_hostname(settings)}/v1"


def _source_repositories() -> dict[str, str]:
    return {
        "control_plane": CONTROL_PLANE_REPO,
        "attested_gateway": ATTESTED_GATEWAY_REPO,
        "cloud_infra": CLOUD_INFRA_REPO,
        "quill": QUILL_REPO,
        "python_sdk": PYTHON_SDK_REPO,
        "javascript_sdk": JAVASCRIPT_SDK_REPO,
        "provider_check": PROVIDER_CHECK_REPO,
    }


def gcp_release(
    settings: Settings,
    *,
    release_metadata: Mapping[str, Any] | None = None,
    release_metadata_status: str = "embedded",
) -> dict[str, Any]:
    digest = settings.trust_gcp_image_digest or NOT_CONFIGURED
    reference = settings.trust_gcp_image_reference or NOT_CONFIGURED
    metadata = release_metadata or {
        "source_commit": settings.trust_gcp_source_commit or NOT_CONFIGURED,
        "image_reference": reference,
        "image_digest": digest,
        "accepted_image_digests": [digest],
        "accepted_image_references": [reference],
        "release_state": "current",
    }
    api_hostnames = [f"api.{domain}" for domain in configured_control_domains(settings)]
    return {
        "platform": "gcp-confidential-space",
        "source_repo": ATTESTED_GATEWAY_REPO,
        "source_repositories": _source_repositories(),
        "source_commit": metadata["source_commit"],
        "image_reference": metadata["image_reference"],
        "image_digest": metadata["image_digest"],
        # Published as SETS. During a roll the fleet still serves the outgoing
        # digest while this record already names the incoming one, so a verifier
        # given only the scalar concludes the enclave answering them does not
        # match its measurement. Carried through from upstream rather than
        # recomputed here — a mirror serves a record, it does not narrow it.
        "accepted_image_digests": list(
            metadata.get("accepted_image_digests") or [metadata["image_digest"]]
        ),
        "accepted_image_references": list(
            metadata.get("accepted_image_references") or [metadata["image_reference"]]
        ),
        "release_state": metadata.get("release_state", "current"),
        "release_metadata_status": release_metadata_status,
        "attestation_issuer": "https://confidentialcomputing.googleapis.com",
        "attestation_audience": "quill-cloud",
        # Describes the GCP PLANE, never the deployment that happens to serve
        # this record. settings.api_base_url is per-deployment, so the AWS- and
        # Azure-hosted control planes were serving a gcp-confidential-space
        # record — Google issuer and audience intact — whose api_base_url
        # pointed at their own gateway. Verified live on 2026-08-15:
        # aws.trustedrouter.com advertised api-aws.trustedrouter.com. A verifier
        # following that would fetch COSE_Sign1 CBOR over a self-signed cert
        # while expecting a Confidential Space JWT, and correctly conclude the
        # measurement did not match — an accusation of tampering manufactured
        # entirely by us.
        #
        # A mirror serves a record; it does not rewrite it. Every field here is
        # a property of the plane.
        "api_base_url": gcp_api_base_url(settings),
        # Derived from api_hostnames, not from api_base_url_for_domain(), for
        # the same reason the scalar above is: that helper returns
        # settings.api_base_url for the canonical domain, which is
        # per-DEPLOYMENT. The scalar was fixed and the plural was not, so on the
        # AWS- and Azure-hosted control planes entry 0 of this list named THEIR
        # gateway inside a gcp-confidential-space record — the exact leak the
        # comment above describes, still open one field over. Every entry here
        # is now api.<control domain>, which is a property of the GCP plane.
        "api_base_urls": [f"https://{hostname}/v1" for hostname in api_hostnames],
        "tls": {
            "mode": "acme-inside-confidential-space",
            # Derived from the same source as api_base_url so the two cannot
            # drift into disagreeing about which host terminates the prompt path.
            "hostname": gcp_api_hostname(settings),
            "hostnames": api_hostnames,
        },
        "data_policy": {
            "prompt_output_storage": False,
            "control_plane_prompt_access": False,
        },
    }


def gcp_release_json(settings: Settings) -> str:
    return json.dumps(gcp_release(settings), indent=2, sort_keys=True) + "\n"


def aws_release(settings: Settings, *, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish the AWS Nitro serving plane's measurement.

    Deploy-time configured rather than resolved live: the enclave answers
    attestation over a self-signed certificate, so fetching it from here would
    mean the control plane making an unauthenticated TLS connection and parsing
    CBOR on a public route. The staleness that trade buys is checked out of band
    by scripts/verify_trust_measurements.py, which .github/workflows/trust-drift.yml
    runs hourly in --strict mode — until that workflow existed, nothing executed
    the comparison this docstring relies on.
    """
    # Mirrored from the plane's own published record when available. The
    # settings are only the offline fallback — the control plane is not the
    # author of what the AWS enclave is running.
    if metadata is not None:
        pcr0 = str(metadata.get("pcr0") or NOT_CONFIGURED)
        accepted = tuple(str(v) for v in metadata.get("accepted_pcr0s", []) if v != NOT_CONFIGURED)
    else:
        pcr0 = settings.trust_aws_pcr0 or NOT_CONFIGURED
        accepted = settings.trust_aws_accepted_pcr0_list
    return {
        "platform": "aws-nitro-enclaves",
        "source_repo": ATTESTED_GATEWAY_REPO,
        "source_repositories": _source_repositories(),
        "source_commit": settings.trust_aws_source_commit or NOT_CONFIGURED,
        "image_reference": settings.trust_aws_image_reference or NOT_CONFIGURED,
        # PCR0 measures the enclave image file: kernel, ramdisk, and
        # application, as built by nitro-cli build-enclave.
        "measurement_type": "nitro-pcr0-sha384",
        "pcr0": pcr0,
        "accepted_pcr0s": list(accepted),
        "release_metadata_status": ("configured" if accepted else NOT_CONFIGURED),
        "attestation_format": "cose-sign1-nitro-attestation-document",
        "attestation_root": AWS_ATTESTATION_ROOT,
        "api_base_url": f"https://{AWS_API_HOSTNAME}/v1",
        "tls": {
            # Deliberately not a public-CA certificate. The enclave generates
            # its own key, and binds the certificate fingerprint and the TLS
            # exporter value into the attestation's user_data, so the connection
            # you are on is the connection that was attested. Chain validation
            # is replaced by that binding, not dropped.
            "mode": "attested-self-signed-inside-enclave",
            "hostname": AWS_API_HOSTNAME,
            # Measured against the live enclave 2026-08-15, 8/8 samples:
            # user_data is 96 bytes and [0:32] is SHA-256 of the served
            # certificate DER, which is also the window probes.py checks. This
            # field previously said [0:64], sending a verifier to compare a
            # 64-byte window against a 32-byte hash and conclude the binding
            # failed. [32:64] is a build-invariant constant, unchanged across
            # nonces and connections, and is deliberately left undescribed
            # rather than guessed at.
            "certificate_binding": "user_data[0:32]=SHA-256 of the served certificate (DER), "
            "user_data[64:96]=TLS exporter channel binding",
        },
        "data_policy": {
            "prompt_output_storage": False,
            "control_plane_prompt_access": False,
        },
    }


#: The only keys copied out of a region entry. Mirroring whatever arrives would
#: let the plane's record inject arbitrary fields into ours. The first three are
#: required of every entry; the last two are optional descriptions the plane may
#: carry and this control plane does not interpret.
_AZURE_REGION_REQUIRED = ("attestation_url", "hostdata", "attestation_issuer")
_AZURE_REGION_OPTIONAL = ("launch_measurement", "compliance_status")


def _azure_regions(metadata: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Per-region endpoints from the plane's record, whitelisted to known keys.

    This is a SHAPE filter and nothing more. Reconciling a region entry against
    the record's own accepted sets happens one layer up, in
    services.trust_release.validated_azure_metadata, which REFUSES a record
    whose region entries contradict it rather than editing them — see that
    function for why a silent drop was the wrong instrument. Every entry that
    reaches production has already been through it.

    An entry that does not name all three required fields as non-empty strings
    is not a region this record can vouch for and is dropped here, because
    azure_release has no error channel to refuse one through. That drop is not
    load-bearing for coverage and this docstring does not claim it is:
    scripts/verify_trust_measurements.py grounds Azure coverage in the MAA
    issuer each contacted endpoint presents LIVE, so a dropped region whose
    issuer the record still publishes is reported as an unreached issuer, while
    a region the record never listed an issuer for is invisible to it either
    way. Refusing the record upstream is what closes that second case.
    """
    regions: list[dict[str, str]] = []
    for entry in metadata.get("regions") or []:
        if not isinstance(entry, Mapping):
            continue
        values = {
            field: str(entry[field])
            for field in (*_AZURE_REGION_REQUIRED, *_AZURE_REGION_OPTIONAL)
            if isinstance(entry.get(field), str) and entry[field]
        }
        if any(field not in values for field in _AZURE_REGION_REQUIRED):
            continue
        regions.append(values)
    return tuple(regions)


def azure_release(
    settings: Settings, *, metadata: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Publish the Azure SEV-SNP serving plane's measurement.

    hostdata is sha256 over the decoded CCE policy, which is what the released
    key is bound to; it is the value a verifier compares against
    x-ms-sevsnpvm-hostdata in an MAA token.
    """
    if metadata is not None:
        hostdata = str(metadata.get("hostdata") or NOT_CONFIGURED)
        accepted = tuple(
            str(v) for v in metadata.get("accepted_hostdata", []) if v != NOT_CONFIGURED
        )
        issuers = tuple(str(v) for v in metadata.get("attestation_issuers", []))
        regions = _azure_regions(metadata)
    else:
        hostdata = settings.trust_azure_hostdata or NOT_CONFIGURED
        accepted = settings.trust_azure_accepted_hostdata_list
        issuers = settings.trust_azure_attestation_issuer_list
        regions = ()
    return {
        "platform": "azure-confidential-containers-sev-snp",
        "source_repo": ATTESTED_GATEWAY_REPO,
        "source_repositories": _source_repositories(),
        "source_commit": settings.trust_azure_source_commit or NOT_CONFIGURED,
        "image_reference": settings.trust_azure_image_reference or NOT_CONFIGURED,
        "measurement_type": "sev-snp-hostdata-sha256",
        "hostdata": hostdata,
        "accepted_hostdata": list(accepted),
        "release_metadata_status": ("configured" if accepted else NOT_CONFIGURED),
        "attestation_format": "microsoft-azure-attestation-jwt",
        "attestation_type": "sevsnpvm",
        # One MAA instance per serving region, so which issuer signs depends on
        # which region answered. A verifier should accept any of them.
        "attestation_issuers": list(issuers),
        # WHERE each of those regions answers. accepted_hostdata is a union and
        # cannot say which endpoint serves which value, so a reader holding only
        # the union can verify whichever region anycast happens to give them and
        # has no way to reach the others. Dropping this array on the way through
        # the mirror is what let scripts/verify_trust_measurements.py check one
        # of two Azure regions and report success for the plane.
        #
        # This function is the LAST layer, not the only one. On the serving
        # route the array has to survive
        # services.trust_release.validated_azure_metadata first, which is where
        # it was actually being dropped — that validator whitelisted three
        # scalar keys, so mirroring it here alone changed nothing anyone could
        # fetch. Both layers, or neither.
        "regions": [dict(region) for region in regions],
        "api_base_url": f"https://{AZURE_API_HOSTNAME}/v1",
        "tls": {
            "mode": "acme-inside-confidential-container",
            "hostname": AZURE_API_HOSTNAME,
        },
        "data_policy": {
            "prompt_output_storage": False,
            "control_plane_prompt_access": False,
        },
    }


def aws_release_json(settings: Settings) -> str:
    return json.dumps(aws_release(settings), indent=2, sort_keys=True) + "\n"


def azure_release_json(settings: Settings) -> str:
    return json.dumps(azure_release(settings), indent=2, sort_keys=True) + "\n"


def trust_html(
    settings: Settings,
    *,
    public_domain: str | None = None,
    api_base_url: str | None = None,
    release_metadata: Mapping[str, Any] | None = None,
    release_metadata_status: str = "embedded",
) -> str:
    release = gcp_release(
        settings,
        release_metadata=release_metadata,
        release_metadata_status=release_metadata_status,
    )
    digest = html.escape(str(release["image_digest"]))
    image = html.escape(str(release["image_reference"]))
    source = html.escape(str(release["source_commit"]))
    domain = public_domain or settings.trusted_domain
    resolved_api_base_url = api_base_url or api_base_url_for_domain(settings, domain)
    api = html.escape(resolved_api_base_url)
    api_hostname = html.escape(resolved_api_base_url.removeprefix("https://").split("/", 1)[0])
    control_origin = html.escape(f"https://{domain}")
    canonical_url = html.escape(canonical_public_url(settings, "/trust"), quote=True)
    docs_url = html.escape(canonical_public_url(settings, "/api/reference"), quote=True)
    trust_title = "Verify TrustedRouter Attestation and Running Code"
    trust_description = (
        "Verify the live TrustedRouter gateway attestation, image digest, source commit, "
        "TLS boundary, and open-source deployment before sending prompts."
    )
    og_image = html.escape(canonical_public_url(settings, "/og.png"), quote=True)
    trust_json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": trust_title,
            "description": trust_description,
            "url": canonical_public_url(settings, "/trust"),
            "about": {
                "@type": "SoftwareApplication",
                "name": "TrustedRouter",
                "applicationCategory": "DeveloperApplication",
                "codeRepository": ATTESTED_GATEWAY_REPO,
            },
        },
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    control_repo = html.escape(CONTROL_PLANE_REPO)
    gateway_repo = html.escape(ATTESTED_GATEWAY_REPO)
    infra_repo = html.escape(CLOUD_INFRA_REPO)
    quill_repo = html.escape(QUILL_REPO)
    python_sdk_repo = html.escape(PYTHON_SDK_REPO)
    javascript_sdk_repo = html.escape(JAVASCRIPT_SDK_REPO)
    provider_check_repo = html.escape(PROVIDER_CHECK_REPO)
    if release_metadata_status == "stale":
        release_warning = (
            '<section class="panel warn"><h2>Release record temporarily stale</h2>'
            "<p>This page is showing the last validated gateway release record and returns "
            "HTTP 503. Verify the canonical trust record before sending sensitive data.</p></section>"
        )
    elif release_metadata_status == "unavailable":
        release_warning = (
            '<section class="panel warn"><h2>Live release record unavailable</h2>'
            "<p>This page cannot currently verify the running gateway digest and returns "
            "HTTP 503. Do not rely on an older embedded digest.</p></section>"
        )
    else:
        release_warning = ""
    release_json = html.escape(json.dumps(release, indent=2, sort_keys=True) + "\n")

    aws = aws_release(settings)
    azure = azure_release(settings)
    aws_pcr0 = html.escape(str(aws["pcr0"]))
    aws_api = html.escape(str(aws["api_base_url"]))
    azure_hostdata = html.escape(str(azure["hostdata"]))
    azure_api = html.escape(str(azure["api_base_url"]))
    azure_issuers = html.escape(", ".join(azure["attestation_issuers"]) or NOT_CONFIGURED)

    def _plane_note(payload: Mapping[str, Any]) -> str:
        if payload["release_metadata_status"] == NOT_CONFIGURED:
            return (
                "<p><strong>No measurement published for this plane yet.</strong> "
                "Do not treat its absence as a measurement of zero — verify against a "
                "live attestation before sending sensitive data.</p>"
            )
        return ""

    aws_note = _plane_note(aws)
    azure_note = _plane_note(azure)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{trust_title}</title>
  <meta name="description" content="{trust_description}">
  <link rel="canonical" href="{canonical_url}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="TrustedRouter">
  <meta property="og:title" content="{trust_title}">
  <meta property="og:description" content="{trust_description}">
  <meta property="og:url" content="{canonical_url}">
  <meta property="og:image" content="{og_image}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{trust_title}">
  <meta name="twitter:description" content="{trust_description}">
  <meta name="twitter:image" content="{og_image}">
  <script type="application/ld+json">{trust_json_ld}</script>
  <style>
    :root {{
      color-scheme: light;
      --ink:#172027; --muted:#5c6974; --line:#d8e1e8; --bg:#f6f8fa;
      --panel:#ffffff; --green:#11724c; --blue:#2355a6; --red:#b42318; --nav:#101820;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:var(--bg); }}
    header {{ border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:3; }}
    nav {{ max-width:1120px; margin:0 auto; padding:14px 22px; display:flex; align-items:center; justify-content:space-between; gap:16px; }}
    a {{ color:var(--blue); text-decoration:none; }}
    .brand {{ font-weight:800; color:var(--ink); display:flex; align-items:center; gap:10px; }}
    .mark {{ width:30px; height:30px; border-radius:7px; background:linear-gradient(135deg,#2c6ecb,#19a06d); display:grid; place-items:center; font-size:13px; color:#fff; }}
    .links {{ display:flex; gap:14px; flex-wrap:wrap; font-size:14px; }}
    .wrap {{ max-width:1120px; margin:0 auto; padding:34px 22px 56px; display:grid; gap:18px; }}
    .hero {{ display:grid; grid-template-columns:minmax(0,1.15fr) minmax(300px,.85fr); gap:20px; align-items:start; }}
    h1 {{ font-size:42px; line-height:1.08; margin:0 0 12px; letter-spacing:0; }}
    h2 {{ font-size:17px; margin:0 0 12px; letter-spacing:0; }}
    p {{ color:var(--muted); line-height:1.55; margin:0 0 14px; }}
    code {{ background:#edf2f6; border:1px solid #d7e0e7; border-radius:6px; padding:2px 6px; font-size:.92em; overflow-wrap:anywhere; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; min-width:0; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }}
    .status {{ display:inline-flex; align-items:center; gap:8px; font-weight:700; color:var(--green); }}
    .dot {{ width:9px; height:9px; border-radius:50%; background:var(--green); }}
    .kv {{ display:grid; gap:12px; margin-top:8px; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0; margin-bottom:3px; }}
    .value {{ font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px; overflow-wrap:anywhere; }}
    .repo-list {{ display:grid; gap:12px; margin:0; }}
    .repo-list p {{ margin:3px 0 0; }}
    .checks {{ list-style:none; padding:0; margin:0; display:grid; gap:10px; }}
    .checks li {{ display:flex; gap:10px; color:#2d3742; line-height:1.4; }}
    .check {{ color:var(--green); font-weight:800; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#101820; color:#eef6ff; border-radius:8px; padding:16px; margin:0; font-size:13px; line-height:1.45; }}
    .warn {{ border-color:#ead49b; background:#fff8e4; color:#5a3b00; }}
    .warn p {{ color:#5a3b00; }}
    @media (max-width:850px) {{
      .hero, .grid {{ grid-template-columns:1fr; }}
      nav {{ align-items:flex-start; flex-direction:column; }}
      h1 {{ font-size:31px; }}
    }}
  </style>
</head>
<body>
  <header>
    <nav>
      <a class="brand" href="{control_origin}"><span class="mark">TR</span><span>TrustedRouter</span></a>
      <div class="links"><a href="{control_repo}">Control repo</a><a href="{gateway_repo}">Gateway repo</a><a href="{infra_repo}">Infra repo</a><a href="{quill_repo}">Quill repo</a><a href="/trust/gcp-release.json">gcp-release.json</a><a href="{docs_url}">API docs</a><a href="{control_origin}">Console</a></div>
    </nav>
  </header>
  <main class="wrap">
    {release_warning}
    <section class="hero">
      <div class="panel">
        <p class="status"><span class="dot"></span>Trust boundary</p>
        <h1>Verify that the hosted API runs the published open-source workload.</h1>
        <p><code>{api_hostname}</code> is the prompt path. Public TLS terminates inside the measured GCP Confidential Space workload. The TrustedRouter control plane does not serve production inference routes and does not receive prompt or output bodies.</p>
        <p>Clients can fetch the live attestation, verify issuer/audience/digest, and compare the measured image digest with the release data published here.</p>
      </div>
      <aside class="panel">
        <h2>Current GCP Workload</h2>
        <div class="kv">
          <div><div class="label">Source commit</div><div class="value">{source}</div></div>
          <div><div class="label">Image</div><div class="value">{image}</div></div>
          <div><div class="label">Digest</div><div class="value">{digest}</div></div>
          <div><div class="label">Attested gateway repo</div><div class="value"><a href="{gateway_repo}">Lore-Hex/quill-cloud-proxy</a></div></div>
          <div><div class="label">API base</div><div class="value">{api}</div></div>
        </div>
      </aside>
    </section>
    <section class="grid" aria-label="Verification checklist">
      <div class="panel">
        <h2>Client Verification</h2>
        <ul class="checks">
          <li><span class="check">OK</span><span>Fetch <code>https://{api_hostname}/attestation</code> over normal public TLS.</span></li>
          <li><span class="check">OK</span><span>Verify the JWT issuer is <code>https://confidentialcomputing.googleapis.com</code>.</span></li>
          <li><span class="check">OK</span><span>Verify the audience is <code>quill-cloud</code>.</span></li>
          <li><span class="check">OK</span><span>Compare the attested image digest with this page.</span></li>
          <li><span class="check">OK</span><span>Check the TLS certificate fingerprint is bound into the attestation nonce.</span></li>
        </ul>
      </div>
      <div class="panel">
        <h2>Published Files</h2>
        <p><a href="/trust/image-digest-gcp.txt">image-digest-gcp.txt</a></p>
        <p><a href="/trust/image-reference-gcp.txt">image-reference-gcp.txt</a></p>
        <p><a href="/trust/gcp-release.json">gcp-release.json</a></p>
        <p><a href="/trust/aws-release.json">aws-release.json</a></p>
        <p><a href="/trust/azure-release.json">azure-release.json</a></p>
      </div>
      <div class="panel warn">
        <h2>DNS Requirement</h2>
        <p><code>{api_hostname}</code> must remain DNS-only or TCP-passthrough. TLS termination by a CDN would break the hosted-code trust claim because the prompt path certificate key must remain inside the measured workload.</p>
      </div>
    </section>
    <section class="grid" aria-label="Attested serving planes">
      <div class="panel">
        <h2>GCP · Confidential Space</h2>
        <div class="kv">
          <div><div class="label">Measures</div><div class="value">Container image digest</div></div>
          <div><div class="label">Image digest</div><div class="value">{digest}</div></div>
          <div><div class="label">Attestation issuer</div><div class="value">confidentialcomputing.googleapis.com</div></div>
          <div><div class="label">API base</div><div class="value">{api}</div></div>
          <div><div class="label">Release record</div><div class="value"><a href="/trust/gcp-release.json">gcp-release.json</a></div></div>
        </div>
        <p>Compare <code>image_digest</code> against the digest in a live attestation JWT from the issuer above, checking its audience is <code>quill-cloud</code>. The certificate fingerprint is bound into the attestation nonce, so the connection you are on is the connection that was attested.</p>
      </div>
      <div class="panel">
        <h2>AWS · Nitro Enclaves</h2>
        {aws_note}
        <div class="kv">
          <div><div class="label">Measures</div><div class="value">PCR0 over the enclave image file (SHA-384)</div></div>
          <div><div class="label">PCR0</div><div class="value">{aws_pcr0}</div></div>
          <div><div class="label">Attestation</div><div class="value">COSE_Sign1, AWS Nitro PKI</div></div>
          <div><div class="label">API base</div><div class="value">{aws_api}</div></div>
          <div><div class="label">Release record</div><div class="value"><a href="/trust/aws-release.json">aws-release.json</a></div></div>
        </div>
        <p>This plane serves a certificate generated inside the enclave rather than one from a public CA. Its fingerprint and the TLS exporter value are bound into the attestation, so the connection you are on is the connection that was attested. Verify with <code>--attested-cert-only</code>; chain validation is replaced by that binding, not dropped.</p>
      </div>
      <div class="panel">
        <h2>Azure · Confidential Containers</h2>
        {azure_note}
        <div class="kv">
          <div><div class="label">Measures</div><div class="value">SEV-SNP hostdata, sha256 over the CCE policy</div></div>
          <div><div class="label">hostdata</div><div class="value">{azure_hostdata}</div></div>
          <div><div class="label">MAA issuers</div><div class="value">{azure_issuers}</div></div>
          <div><div class="label">API base</div><div class="value">{azure_api}</div></div>
          <div><div class="label">Release record</div><div class="value"><a href="/trust/azure-release.json">azure-release.json</a></div></div>
        </div>
        <p>Compare <code>hostdata</code> against <code>x-ms-sevsnpvm-hostdata</code> in a live MAA token. Each serving region runs its own MAA instance, so accept any issuer listed above.</p>
      </div>
    </section>
    <section class="panel">
      <h2>Why the three measurements look different</h2>
      <p>Each platform measures the artifact its own hardware can attest to, so there is no single number to compare across all three. GCP measures the container image; AWS measures the enclave image file into PCR0; Azure measures the policy that constrains what the container is allowed to be. A verifier checks one plane at a time, against that plane's own record.</p>
      <p>Measurements published here are a <strong>set</strong>, not a single value. During a rollout the released key is deliberately bound to both the outgoing and incoming measurement so the old enclave keeps serving while the new one starts. A verifier pinned to exactly one value would fail during precisely that window, which is why each record carries the full accepted set alongside the value expected to be serving.</p>
    </section>
    <section class="grid">
      <div class="panel"><h2>No Prompt Logs</h2><p>Ordinary synchronous and streaming prompt/output storage is disabled. The opt-in Batch API uses separately documented encrypted retention. Generation content endpoint returns a compatible <code>content_not_stored</code> response.</p></div>
      <div class="panel">
        <h2>Hosted Open Source</h2>
        <div class="repo-list">
          <div><a href="{control_repo}">Lore-Hex/quill-router</a><p>Control plane, billing, keys, compatibility routes, dashboard, and trust page.</p></div>
          <div><a href="{gateway_repo}">Lore-Hex/quill-cloud-proxy</a><p>Attested prompt gateway, release digest, and Confidential Space verification path.</p></div>
          <div><a href="{infra_repo}">Lore-Hex/quill-cloud-infra</a><p>Cloud deployment scripts, measured workload bringup, and trust publication flow.</p></div>
          <div><a href="{quill_repo}">Lore-Hex/quill</a><p>Open-source Quill client, device, bootstrap, and attestation-facing code.</p></div>
          <div><a href="{python_sdk_repo}">Lore-Hex/trusted-router-py</a><p>Python SDK repository for attestation-aware client helpers.</p></div>
          <div><a href="{javascript_sdk_repo}">Lore-Hex/trusted-router-js</a><p>JavaScript SDK repository for browser and Node integrations.</p></div>
          <div><a href="{provider_check_repo}">Lore-Hex/trustedrouter-provider-check</a><p>Public provider conformance suite for validating the attested gateway translator contract.</p></div>
        </div>
      </div>
      <div class="panel"><h2>Fail Closed</h2><p>If attestation, billing authorization, or the gateway contract is unavailable, the prompt path should fail rather than silently downgrade to a non-attested route.</p></div>
    </section>
    <section class="panel">
      <h2>Machine-readable release</h2>
      <pre>{release_json}</pre>
    </section>
  </main>
</body>
</html>"""
