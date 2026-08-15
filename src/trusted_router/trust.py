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


def gcp_release(
    settings: Settings,
    *,
    release_metadata: Mapping[str, str] | None = None,
    release_metadata_status: str = "embedded",
) -> dict[str, Any]:
    metadata = release_metadata or {
        "source_commit": settings.trust_gcp_source_commit or "not-configured",
        "image_reference": settings.trust_gcp_image_reference or "not-configured",
        "image_digest": settings.trust_gcp_image_digest or "not-configured",
    }
    api_hostnames = [f"api.{domain}" for domain in configured_control_domains(settings)]
    return {
        "platform": "gcp-confidential-space",
        "source_repo": ATTESTED_GATEWAY_REPO,
        "source_repositories": {
            "control_plane": CONTROL_PLANE_REPO,
            "attested_gateway": ATTESTED_GATEWAY_REPO,
            "cloud_infra": CLOUD_INFRA_REPO,
            "quill": QUILL_REPO,
            "python_sdk": PYTHON_SDK_REPO,
            "javascript_sdk": JAVASCRIPT_SDK_REPO,
        },
        "source_commit": metadata["source_commit"],
        "image_reference": metadata["image_reference"],
        "image_digest": metadata["image_digest"],
        "release_metadata_status": release_metadata_status,
        "attestation_issuer": "https://confidentialcomputing.googleapis.com",
        "attestation_audience": "quill-cloud",
        "api_base_url": settings.api_base_url,
        "api_base_urls": [
            api_base_url_for_domain(settings, domain)
            for domain in configured_control_domains(settings)
        ],
        "tls": {
            "mode": "acme-inside-confidential-space",
            "hostname": "api.trustedrouter.com",
            "hostnames": api_hostnames,
        },
        "data_policy": {
            "prompt_output_storage": False,
            "control_plane_prompt_access": False,
        },
    }


def gcp_release_json(settings: Settings) -> str:
    return json.dumps(gcp_release(settings), indent=2, sort_keys=True) + "\n"


def trust_html(
    settings: Settings,
    *,
    public_domain: str | None = None,
    api_base_url: str | None = None,
    release_metadata: Mapping[str, str] | None = None,
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
    if release_metadata_status == "stale":
        release_warning = (
            '<section class="panel warn"><h2>Release record temporarily stale</h2>'
            '<p>This page is showing the last validated gateway release record and returns '
            'HTTP 503. Verify the canonical trust record before sending sensitive data.</p></section>'
        )
    elif release_metadata_status == "unavailable":
        release_warning = (
            '<section class="panel warn"><h2>Live release record unavailable</h2>'
            '<p>This page cannot currently verify the running gateway digest and returns '
            'HTTP 503. Do not rely on an older embedded digest.</p></section>'
        )
    else:
        release_warning = ""
    release_json = html.escape(json.dumps(release, indent=2, sort_keys=True) + "\n")
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
      </div>
      <div class="panel warn">
        <h2>DNS Requirement</h2>
        <p><code>{api_hostname}</code> must remain DNS-only or TCP-passthrough. TLS termination by a CDN would break the hosted-code trust claim because the prompt path certificate key must remain inside the measured workload.</p>
      </div>
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
