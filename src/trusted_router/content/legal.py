"""Public legal/procurement packet helpers.

This module deliberately separates legal packet facts from marketing copy.
The output is consumed by lawyers in HTML and by agents in JSON, so statuses
must be conservative and explicit.
"""

from __future__ import annotations

from typing import Any

from trusted_router.catalog import Provider, provider_privacy_tier, providers_for_display
from trusted_router.config import Settings

SYSTEM_SUBPROCESSORS: tuple[dict[str, str], ...] = (
    {
        "name": "Google Cloud Platform",
        "purpose": "Cloud hosting, Confidential Space, Cloud Run, Spanner, Bigtable, KMS, Secret Manager, and operational infrastructure.",
        "data_access": "Prompt traffic on the production API terminates inside the attested gateway. GCP services store metadata, billing records, secrets, and operational logs as configured; prompt/output content is not stored by default.",
        "policy_url": "https://cloud.google.com/security/compliance",
    },
    {
        "name": "Cloudflare",
        "purpose": "DNS, public-site caching, status/trust hosting support, and edge protection for non-prompt surfaces.",
        "data_access": "Public website traffic and DNS metadata. Production prompt TLS is designed to terminate inside the attested gateway, not inside the control-plane site.",
        "policy_url": "https://www.cloudflare.com/trust-hub/",
    },
    {
        "name": "Stripe",
        "purpose": "Card payments, stablecoin checkout, customer records, saved payment methods, invoices, and billing webhooks.",
        "data_access": "Billing identity and payment metadata. No prompt/output content.",
        "policy_url": "https://stripe.com/privacy",
    },
    {
        "name": "PayPal",
        "purpose": "Optional PayPal payment processing for prepaid credits.",
        "data_access": "Billing identity and payment metadata. No prompt/output content.",
        "policy_url": "https://www.paypal.com/us/legalhub/privacy-full",
    },
    {
        "name": "Amazon Web Services SES/SNS",
        "purpose": "Transactional email delivery, bounce handling, complaint handling, and email-domain verification.",
        "data_access": "Email address and transactional email metadata. No prompt/output content.",
        "policy_url": "https://aws.amazon.com/privacy/",
    },
    {
        "name": "Sentry",
        "purpose": "Control-plane exception monitoring.",
        "data_access": "Scrubbed control-plane errors and metadata. Sentry is not configured in the attested prompt gateway and must not receive prompts, outputs, API keys, or BYOK secrets.",
        "policy_url": "https://sentry.io/privacy/",
    },
    {
        "name": "Axiom",
        "purpose": "Operational log search and alerting.",
        "data_access": "Structured operational metadata. Prompt/output content must not be logged.",
        "policy_url": "https://axiom.co/privacy",
    },
    {
        "name": "GitHub",
        "purpose": "Source control, CI, release workflows, and public open-source repositories.",
        "data_access": "Source code, CI metadata, and release artifacts. No production prompt/output content.",
        "policy_url": "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement",
    },
)

SOC2_READINESS_DOCUMENTS: tuple[dict[str, str], ...] = (
    {
        "name": "SOC 2 readiness overview",
        "path": "docs/compliance/soc2/README.md",
        "status": "prepared_for_type_1_readiness",
    },
    {
        "name": "System description",
        "path": "docs/compliance/soc2/system-description.md",
        "status": "prepared_for_management_review",
    },
    {
        "name": "Control matrix",
        "path": "docs/compliance/soc2/control-matrix.md",
        "status": "prepared_for_auditor_mapping",
    },
    {
        "name": "Evidence checklist",
        "path": "docs/compliance/soc2/evidence-checklist.md",
        "status": "prepared_for_evidence_collection",
    },
    {
        "name": "Information security policy",
        "path": "docs/compliance/soc2/policies/information-security.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Access control policy",
        "path": "docs/compliance/soc2/policies/access-control.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Change management and SDLC policy",
        "path": "docs/compliance/soc2/policies/change-management-sdlc.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Incident response policy",
        "path": "docs/compliance/soc2/policies/incident-response.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Risk management policy",
        "path": "docs/compliance/soc2/policies/risk-management.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Vendor management policy",
        "path": "docs/compliance/soc2/policies/vendor-management.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Asset management policy",
        "path": "docs/compliance/soc2/policies/asset-management.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Data classification and retention policy",
        "path": "docs/compliance/soc2/policies/data-classification-retention.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Backup, disaster recovery, and business continuity policy",
        "path": "docs/compliance/soc2/policies/backup-dr-bcp.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Vulnerability management policy",
        "path": "docs/compliance/soc2/policies/vulnerability-management.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Logging and monitoring policy",
        "path": "docs/compliance/soc2/policies/logging-monitoring.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Encryption and key management policy",
        "path": "docs/compliance/soc2/policies/encryption-key-management.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Personnel security and training policy",
        "path": "docs/compliance/soc2/policies/personnel-security-training.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "AI data handling policy",
        "path": "docs/compliance/soc2/policies/ai-data-handling.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Audit operations policy",
        "path": "docs/compliance/soc2/policies/audit-operations.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "Risk register template",
        "path": "docs/compliance/soc2/templates/risk-register.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Vendor review template",
        "path": "docs/compliance/soc2/templates/vendor-review.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Access review template",
        "path": "docs/compliance/soc2/templates/access-review.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Incident record template",
        "path": "docs/compliance/soc2/templates/incident-record.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Change record template",
        "path": "docs/compliance/soc2/templates/change-record.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Asset inventory template",
        "path": "docs/compliance/soc2/templates/asset-inventory.md",
        "status": "prepared_for_recurring_evidence",
    },
    {
        "name": "Evidence index template",
        "path": "docs/compliance/soc2/templates/evidence-index.md",
        "status": "prepared_for_recurring_evidence",
    },
)

HIPAA_READINESS_DOCUMENTS: tuple[dict[str, str], ...] = (
    {
        "name": "HIPAA readiness overview",
        "path": "docs/compliance/hipaa/README.md",
        "status": "prepared_for_customer_review",
    },
    {
        "name": "HIPAA readiness matrix",
        "path": "docs/compliance/hipaa/hipaa-readiness-matrix.md",
        "status": "prepared_for_safeguard_mapping",
    },
    {
        "name": "PHI handling policy",
        "path": "docs/compliance/hipaa/policies/phi-handling.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "BAA operations policy",
        "path": "docs/compliance/hipaa/policies/baa-operations.md",
        "status": "prepared_for_contract_operations",
    },
    {
        "name": "HIPAA incident and breach response policy",
        "path": "docs/compliance/hipaa/policies/hipaa-incident-breach-response.md",
        "status": "prepared_for_approval_and_operation",
    },
    {
        "name": "HIPAA risk analysis template",
        "path": "docs/compliance/hipaa/templates/hipaa-risk-analysis.md",
        "status": "prepared_for_customer_specific_review",
    },
    {
        "name": "PHI route approval template",
        "path": "docs/compliance/hipaa/templates/phi-route-approval.md",
        "status": "prepared_for_customer_specific_review",
    },
    {
        "name": "BAA execution checklist",
        "path": "docs/compliance/hipaa/templates/baa-execution-checklist.md",
        "status": "prepared_for_customer_specific_review",
    },
)


def legal_entity(settings: Settings) -> dict[str, str]:
    entity = {
        "name": settings.legal_entity_name,
        "type": settings.legal_entity_type,
        "address": settings.legal_entity_address,
        "phone": settings.legal_entity_phone,
        "ein": settings.legal_entity_ein,
        "duns": settings.legal_entity_duns,
        "signatory_name": settings.legal_signatory_name,
        "signatory_title": settings.legal_signatory_title,
        "security_contact_email": settings.security_contact_email,
    }
    if settings.legal_entity_date_established:
        entity["date_established"] = settings.legal_entity_date_established
    return entity


def provider_subprocessor_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider in providers_for_display():
        if provider.slug == "trustedrouter":
            continue
        rows.append(_provider_subprocessor_row(provider))
    return rows


def _provider_subprocessor_row(provider: Provider) -> dict[str, Any]:
    tier = provider_privacy_tier(provider)
    return {
        "id": provider.slug,
        "name": provider.name,
        "purpose": "Downstream model inference provider when a workspace selects this provider, this model, or an alias that routes to this provider.",
        "data_access": "Prompt/output content in transit only for requests routed to this provider; request metadata needed for billing, routing, abuse controls, and support.",
        "zdr": provider.provider_zero_data_retention,
        "confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "privacy_tier": tier,
        "policy": provider.provider_policy,
        "policy_url": provider.provider_policy_url,
    }


def subprocessor_packet() -> dict[str, Any]:
    return {
        "system_subprocessors": list(SYSTEM_SUBPROCESSORS),
        "model_provider_subprocessors": provider_subprocessor_rows(),
        "routing_note": (
            "Model providers are subprocessors only for traffic routed to them. "
            "Use trustedrouter/zdr, trustedrouter/e2e, or explicit provider allowlists "
            "for sensitive legal workloads."
        ),
    }


def procurement_packet(settings: Settings) -> dict[str, Any]:
    checkpoint = {
        "DPA": {
            "status": "draft_available_requires_signature",
            "obtained_for_production": False,
            "url": f"https://{settings.trusted_domain}/legal/dpa",
            "note": "Use only after the DPA is reviewed and signed or after legal grants a written exception.",
        },
        "named_entity": {
            "status": "available",
            "obtained_for_production": True,
            "url": f"https://{settings.trusted_domain}/legal",
            "note": settings.legal_entity_name,
        },
        "subprocessor_list": {
            "status": "available",
            "obtained_for_production": True,
            "url": f"https://{settings.trusted_domain}/legal/subprocessors",
            "note": "Includes platform vendors and downstream model providers.",
        },
        "SOC_2": {
            "status": "not_obtained",
            "obtained_for_production": False,
            "url": f"https://{settings.trusted_domain}/legal/soc2-readiness",
            "note": "SOC 2 readiness documentation is prepared. No independent SOC 2 Type I or Type II report has been obtained yet.",
        },
        "HIPAA": {
            "status": "readiness_package_available_requires_signed_baa",
            "obtained_for_production": False,
            "url": f"https://{settings.trusted_domain}/legal/hipaa-readiness",
            "note": "HIPAA readiness documentation and draft BAA are available. PHI production requires an executed BAA and approved route policy.",
        },
    }
    return {
        "service": "TrustedRouter",
        "generated_for": "read-only procurement review and agent checkpointing",
        "legal_entity": legal_entity(settings),
        "checkpoint": checkpoint,
        "production_recommendation": (
            "Do not send privileged attorney work product to production until the DPA "
            "is signed and the customer accepts the current no-SOC-2 status in writing "
            "or waits for SOC 2."
        ),
        "legal_defaults": {
            "sensitive_workload_default_model": "trustedrouter/zdr",
            "content_export_requires_written_approval": True,
            "security_contact_email": settings.security_contact_email,
            "contract_signatory": {
                "name": settings.legal_signatory_name,
                "title": settings.legal_signatory_title,
            },
            "route_restriction_explanation": (
                "Restricted routing means sensitive or regulated prompts may only route to aliases "
                "or providers that the customer approved in writing. Broad aliases such as "
                "trustedrouter/auto are not acceptable for PHI or privileged legal work unless every "
                "possible downstream route is approved."
            ),
        },
        "available_documents": {
            "legal_packet": f"https://{settings.trusted_domain}/legal",
            "dpa_draft": f"https://{settings.trusted_domain}/legal/dpa",
            "baa_draft": f"https://{settings.trusted_domain}/legal/baa",
            "soc2_readiness": f"https://{settings.trusted_domain}/legal/soc2-readiness",
            "hipaa_readiness": f"https://{settings.trusted_domain}/legal/hipaa-readiness",
            "subprocessors": f"https://{settings.trusted_domain}/legal/subprocessors",
            "procurement_json": f"https://{settings.trusted_domain}/legal/procurement.json",
            "soc2_readiness_json": f"https://{settings.trusted_domain}/legal/soc2-readiness.json",
            "hipaa_readiness_json": f"https://{settings.trusted_domain}/legal/hipaa-readiness.json",
            "subprocessors_json": f"https://{settings.trusted_domain}/legal/subprocessors.json",
            "trust_attestation": "https://trust.trustedrouter.com/",
            "security": f"https://{settings.trusted_domain}/security",
            "providers": f"https://{settings.trusted_domain}/providers",
        },
        "subprocessors": subprocessor_packet(),
        "agent_instruction": (
            "Report which of {DPA, named entity, subprocessor list, SOC 2, HIPAA/BAA} are obtained. "
            "If DPA, SOC 2, HIPAA, or BAA are required by the matter policy and still missing, "
            "stop before production traffic. For legal or healthcare workloads, default to "
            "trustedrouter/zdr unless the customer approved a narrower provider allowlist. "
            "Verify the running gateway at https://trust.trustedrouter.com/."
        ),
    }


def soc2_readiness_packet(settings: Settings) -> dict[str, Any]:
    return {
        "service": "TrustedRouter",
        "legal_entity": legal_entity(settings),
        "status": "readiness_package_available_no_report_obtained",
        "soc2_type_1_report": "not_obtained",
        "soc2_type_2_report": "not_obtained",
        "audit_claim": "No independent SOC 2 report is currently available.",
        "target_report": {
            "initial_target": "SOC 2 Type I",
            "future_target": "SOC 2 Type II after an observation period",
            "trust_services_categories": [
                "Security",
                "Availability",
                "Confidentiality",
                "Privacy",
                "Processing Integrity for billing, authorization, settlement, credits, and refunds",
            ],
            "scope": [
                "Hosted control plane",
                "Billing and payment-method management",
                "API key and workspace management",
                "Public trust/status/legal surfaces",
                "Attested API gateway and settlement callbacks",
            ],
            "out_of_scope": [
                "Customer self-hosted deployments",
                "Downstream model-provider internal systems except as subprocessors",
                "Customer BYOK provider accounts beyond encrypted storage and release into the attested gateway",
                "Non-production experiments",
            ],
        },
        "documents": list(SOC2_READINESS_DOCUMENTS),
        "type_1_blockers": [
            "Management approval of policies and system description",
            "Evidence collection indexed to the control matrix",
            "Auditor engagement and readiness review",
            "Management assertion signed for the audit date",
        ],
        "type_2_blockers": [
            "Operating period evidence after Type I scope is approved",
            "Recurring access reviews, vendor reviews, incident reviews, vulnerability evidence, and change evidence",
        ],
        "operating_model_note": (
            "A solo-founder Type I readiness pass is feasible with strict automation and evidence templates. "
            "Type II should use an evidence/compliance platform or equivalent workflow before the observation period."
        ),
        "references": [
            {
                "name": "AICPA Trust Services Criteria",
                "url": "https://www.aicpa.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022",
            }
        ],
        "agent_instruction": (
            "Treat this as readiness documentation only. Do not report SOC 2 obtained, certified, "
            "or audited until a CPA firm issues a SOC 2 report."
        ),
    }


def hipaa_readiness_packet(settings: Settings) -> dict[str, Any]:
    return {
        "service": "TrustedRouter",
        "legal_entity": legal_entity(settings),
        "status": "readiness_package_available_requires_executed_baa",
        "hipaa_certification": "not_obtained",
        "baa": "draft_available_requires_signature",
        "phi_production_approved": False,
        "contract_signatory": {
            "name": settings.legal_signatory_name,
            "title": settings.legal_signatory_title,
        },
        "security_contact_email": settings.security_contact_email,
        "production_gate": (
            "Do not send PHI/ePHI until a BAA is executed, route restrictions are approved, "
            "content export is disabled or separately approved, and PHI subprocessors are accepted in writing."
        ),
        "default_route_policy": {
            "default_sensitive_alias": "trustedrouter/zdr",
            "content_export_requires_written_approval": True,
            "meaning": (
                "PHI can use only customer-approved routes. The default candidate is trustedrouter/zdr; "
                "trustedrouter/e2e or named provider allowlists may be approved per customer. "
                "Unrestricted trustedrouter/auto is not approved for PHI."
            ),
        },
        "safeguard_scope": {
            "administrative": [
                "Risk analysis and risk management process",
                "Assigned security and privacy responsibility",
                "Workforce access and training controls",
                "Incident and breach response process",
                "BAA and subprocessor approval process",
            ],
            "physical": [
                "Cloud data center physical controls inherited from cloud providers",
                "Device and media controls for operator workstations",
                "No production prompt content stored on operator devices",
            ],
            "technical": [
                "Attested gateway boundary",
                "Encrypted transport",
                "Encrypted metadata and BYOK storage",
                "API key hashing and scoped access",
                "Metadata-only logging by default",
                "Route allowlists for PHI workloads",
            ],
        },
        "documents": list(HIPAA_READINESS_DOCUMENTS),
        "required_before_phi": [
            "Executed BAA",
            "BAA signed by Joseph Perla, CEO, as Lore Hex Corp officer",
            "Customer role and covered-service scope confirmed",
            "PHI route policy approved",
            "Approved downstream model-provider subprocessor list",
            "Content export disabled unless separately approved",
            "Customer counsel signoff",
        ],
        "references": [
            {
                "name": "HHS HIPAA Security Rule",
                "url": "https://www.hhs.gov/hipaa/for-professionals/security/index.html",
            },
            {
                "name": "HHS sample business associate agreement provisions",
                "url": "https://www.hhs.gov/hipaa/for-professionals/covered-entities/sample-business-associate-agreement-provisions/index.html",
            },
        ],
        "agent_instruction": (
            "Treat this as HIPAA readiness documentation only. Do not send PHI until the BAA "
            "is signed and the PHI route approval is complete."
        ),
    }
