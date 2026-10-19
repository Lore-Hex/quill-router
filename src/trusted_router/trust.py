from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any

from trusted_router.config import Settings
from trusted_router.dashboard import _organization_node, _static_version
from trusted_router.domains import (
    api_base_url_for_domain,
    canonical_public_url,
    configured_control_domains,
)
from trusted_router.views import render_template

ATTESTED_GATEWAY_REPO = "https://github.com/Lore-Hex/quill-cloud-proxy"
CLOUD_INFRA_REPO = "https://github.com/Lore-Hex/quill-cloud-infra"
CONTROL_PLANE_REPO = "https://github.com/Lore-Hex/quill-router"
QUILL_REPO = "https://github.com/Lore-Hex/quill"
PYTHON_SDK_REPO = "https://github.com/Lore-Hex/trusted-router-py"
JAVASCRIPT_SDK_REPO = "https://github.com/Lore-Hex/trusted-router-js"
PROVIDER_CHECK_REPO = "https://github.com/Lore-Hex/trustedrouter-provider-check"

TRUST_AGENT_PROMPT = (
    "Use https://trust.trustedrouter.com to verify https://trustedrouter.com "
    "with fresh, TLS-bound attestation. Trace the running build to published "
    "source and build provenance. Separate router protections from model-provider "
    "privacy claims. Report verified evidence, failures, and anything you cannot verify."
)


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

    Mirrored from the plane-owned release record rather than resolved from a
    live attestation here. Its staleness is checked out of band by
    scripts/verify_trust_measurements.py, which .github/workflows/trust-drift.yml
    runs hourly in --strict mode — until that workflow existed, nothing executed
    the comparison this docstring relies on.
    """
    # Mirrored from the plane's own published record when available. The
    # settings are only the offline fallback — the control plane is not the
    # author of what the AWS enclave is running.
    if metadata is not None:
        pcr0 = str(metadata.get("pcr0") or NOT_CONFIGURED)
        accepted = tuple(str(v) for v in metadata.get("accepted_pcr0s", []) if v != NOT_CONFIGURED)
        mirrored_source_commit = metadata.get("source_commit")
        source_commit_provenance = (
            metadata.get("source_commit_provenance")
            if mirrored_source_commit is not None
            else None
        )
        release_state = metadata.get("release_state")
    else:
        pcr0 = settings.trust_aws_pcr0 or NOT_CONFIGURED
        accepted = settings.trust_aws_accepted_pcr0_list
        mirrored_source_commit = None
        source_commit_provenance = None
        release_state = None
    source_commit = mirrored_source_commit or settings.trust_aws_source_commit or NOT_CONFIGURED
    return {
        "platform": "aws-nitro-enclaves",
        "source_repo": ATTESTED_GATEWAY_REPO,
        "source_repositories": _source_repositories(),
        "source_commit": source_commit,
        **(
            {"source_commit_provenance": source_commit_provenance}
            if source_commit_provenance is not None
            else {}
        ),
        "image_reference": settings.trust_aws_image_reference or NOT_CONFIGURED,
        # PCR0 measures the enclave image file: kernel, ramdisk, and
        # application, as built by nitro-cli build-enclave.
        "measurement_type": "nitro-pcr0-sha384",
        "pcr0": pcr0,
        "accepted_pcr0s": list(accepted),
        **({"release_state": release_state} if release_state is not None else {}),
        "release_metadata_status": ("configured" if accepted else NOT_CONFIGURED),
        "attestation_format": "cose-sign1-nitro-attestation-document",
        "attestation_root": AWS_ATTESTATION_ROOT,
        "api_base_url": f"https://{AWS_API_HOSTNAME}/v1",
        "tls": {
            # The enclave obtains a public-CA (Let's Encrypt) certificate via
            # ACME dns01 running inside the enclave, so normal WebPKI chain
            # validation works. The attestation still binds the served
            # certificate's SHA-256 into user_data[0:32]; that stronger anchor
            # is unchanged from the self-signed era.
            "mode": "acme-inside-nitro-enclave",
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
    aws_record: Mapping[str, Any] | None = None,
    aws_status: str = "embedded",
    azure_record: Mapping[str, Any] | None = None,
    azure_status: str = "embedded",
) -> str:
    release = gcp_release(
        settings,
        release_metadata=release_metadata,
        release_metadata_status=release_metadata_status,
    )
    domain = public_domain or settings.trusted_domain
    resolved_api_base_url = api_base_url or api_base_url_for_domain(settings, domain)
    api_hostname = resolved_api_base_url.removeprefix("https://").split("/", 1)[0]
    control_origin = f"https://{domain}"
    canonical_url = canonical_public_url(settings, "/trust")
    docs_url = canonical_public_url(settings, "/api/reference")
    trust_title = "Verify TrustedRouter Attestation and Running Code"
    trust_description = (
        "Verify the live TrustedRouter gateway attestation, image digest, source commit, "
        "TLS boundary, and open-source deployment before sending prompts."
    )
    og_image = canonical_public_url(settings, "/og.png")
    # The @graph form so the operating company travels with the page. /trust is
    # where somebody lands to decide whether this vendor is real, which makes it
    # the worst page to omit the contact and address from.
    trust_json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@graph": [
                _organization_node(settings),
                {
                    "@type": "WebPage",
                    "name": trust_title,
                    "description": trust_description,
                    "url": canonical_url,
                    "about": {
                        "@type": "SoftwareApplication",
                        "name": "TrustedRouter",
                        "applicationCategory": "DeveloperApplication",
                        "codeRepository": ATTESTED_GATEWAY_REPO,
                    },
                },
            ],
        },
        separators=(",", ":"),
    ).replace("<", "\\u003c")

    # The records /trust/aws-release.json and /trust/azure-release.json serve,
    # with the status each of those routes reports.
    aws = aws_record if aws_record is not None else aws_release(settings)
    azure = azure_record if azure_record is not None else azure_release(settings)

    def _plane_note(payload: Mapping[str, Any], status: str) -> str:
        if payload["release_metadata_status"] == NOT_CONFIGURED:
            return (
                "<p class=\"trust-plane-note\"><strong>No measurement is available for this plane right now.</strong> "
                "Do not treat its absence as a measurement of zero — verify against a "
                "live attestation before sending sensitive data.</p>"
            )
        if status == "stale":
            return (
                "<p class=\"trust-plane-note\"><strong>This plane's release record could not be refreshed.</strong> "
                "This is the last validated copy — verify against a live attestation "
                "before sending sensitive data.</p>"
            )
        return ""

    aws_note = _plane_note(aws, aws_status)
    azure_note = _plane_note(azure, azure_status)
    return render_template(
        "public/trust.html",
        title=trust_title,
        description=trust_description,
        site_url=canonical_url,
        canonical_url=canonical_url,
        og_image=og_image,
        og_image_alt="Verify TrustedRouter attestation and running code",
        json_ld_blob=trust_json_ld,
        api_base_url=resolved_api_base_url,
        static_version=_static_version(settings),
        agent_prompt=TRUST_AGENT_PROMPT,
        release_metadata_status=release_metadata_status,
        control_repo=CONTROL_PLANE_REPO,
        gateway_repo=ATTESTED_GATEWAY_REPO,
        infra_repo=CLOUD_INFRA_REPO,
        python_sdk_repo=PYTHON_SDK_REPO,
        javascript_sdk_repo=JAVASCRIPT_SDK_REPO,
        provider_check_repo=PROVIDER_CHECK_REPO,
        docs_url=docs_url,
        control_origin=control_origin,
        api_hostname=api_hostname,
        api=resolved_api_base_url,
        source=str(release["source_commit"]),
        image=str(release["image_reference"]),
        digest=str(release["image_digest"]),
        # Escaped here and printed with |safe, as the page always printed it:
        # tests pin the &quot; form of the record's quotes
        # (tests/private_repository.py).
        release_json=html.escape(json.dumps(release, indent=2, sort_keys=True) + "\n"),
        aws_note=aws_note,
        aws_pcr0=str(aws["pcr0"]),
        aws_api=str(aws["api_base_url"]),
        azure_note=azure_note,
        azure_hostdata=str(azure["hostdata"]),
        azure_api=str(azure["api_base_url"]),
        azure_issuers=", ".join(azure["attestation_issuers"]) or NOT_CONFIGURED,
    )
