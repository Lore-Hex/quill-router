"""Evidence-backed AI gateway and model-router comparison content."""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-08-16"


@dataclass(frozen=True)
class ComparisonSource:
    label: str
    url: str


@dataclass(frozen=True)
class CompetitorComparison:
    slug: str
    name: str
    category: str
    summary: str
    competitor_fit: str
    trustedrouter_fit: str
    migration: str
    rows: tuple[tuple[str, str, str], ...]
    sources: tuple[ComparisonSource, ...]
    faq_items: tuple[tuple[str, str], ...]
    custom_page: bool = False

    @property
    def href(self) -> str:
        return f"/compare/{self.slug}"

    @property
    def title(self) -> str:
        return f"TrustedRouter vs {self.name}"

    @property
    def description(self) -> str:
        return (
            f"Compare TrustedRouter and {self.name} across APIs, deployment, routing, "
            "billing, observability, content handling, and verifiable privacy."
        )


def _rows(
    *,
    deployment: str,
    api: str,
    catalog: str,
    routing: str,
    observability: str,
    content: str,
    verification: str,
    billing: str,
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("Deployment", deployment, "Hosted open-source control plane with an attested API path"),
        ("API surface", api, "OpenAI Chat Completions and Responses plus Anthropic Messages"),
        ("Model access", catalog, "Hundreds of models across direct, BYOK, and prepaid routes"),
        ("Routing", routing, "Provider fallback plus auto, cheap, ZDR, E2E, EU, and combo routes"),
        ("Observability", observability, "Metadata analytics and opt-in external broadcast"),
        ("Prompt content", content, "No durable prompt or output logs on realtime inference"),
        (
            "Verification",
            verification,
            "Live gateway attestation bound to published source and release evidence",
        ),
        ("Billing", billing, "Prepaid credits at provider price plus 5.5%, or BYOK"),
    )


def _faq(name: str, competitor_fit: str, migration: str) -> tuple[tuple[str, str], ...]:
    return (
        (
            f"When should I choose {name}?",
            competitor_fit,
        ),
        (
            "When should I choose TrustedRouter?",
            "Choose TrustedRouter when one API must combine many providers with realtime "
            "inference that does not durably log prompt or output content, plus a live "
            "hardware-attested gateway build your team can verify.",
        ),
        (
            f"How hard is it to move from {name}?",
            migration,
        ),
    )


def _comparison(
    *,
    slug: str,
    name: str,
    category: str,
    summary: str,
    competitor_fit: str,
    trustedrouter_fit: str,
    migration: str,
    deployment: str,
    api: str,
    catalog: str,
    routing: str,
    observability: str,
    content: str,
    verification: str,
    billing: str,
    sources: tuple[ComparisonSource, ...],
    custom_page: bool = False,
) -> CompetitorComparison:
    return CompetitorComparison(
        slug=slug,
        name=name,
        category=category,
        summary=summary,
        competitor_fit=competitor_fit,
        trustedrouter_fit=trustedrouter_fit,
        migration=migration,
        rows=_rows(
            deployment=deployment,
            api=api,
            catalog=catalog,
            routing=routing,
            observability=observability,
            content=content,
            verification=verification,
            billing=billing,
        ),
        sources=sources,
        faq_items=_faq(name, competitor_fit, migration),
        custom_page=custom_page,
    )


COMPETITOR_COMPARISONS: tuple[CompetitorComparison, ...] = (
    _comparison(
        slug="openrouter",
        name="OpenRouter",
        category="Hosted model marketplace",
        summary=(
            "OpenRouter popularized a broad hosted model marketplace. TrustedRouter keeps the "
            "one-key migration shape and adds an open-source, hardware-attested gateway path."
        ),
        competitor_fit=(
            "Choose OpenRouter when its marketplace breadth, community integrations, or a route "
            "that is not yet available on TrustedRouter is the deciding requirement."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when gateway content handling must be technically inspectable "
            "and verified against the running release."
        ),
        migration=(
            "OpenAI-compatible clients usually move by changing the base URL and API key. Keep "
            "the same messages, streaming loop, and most canonical model identifiers."
        ),
        deployment="Hosted model marketplace",
        api="OpenAI-compatible API with additional platform endpoints",
        catalog="Very broad model and provider marketplace",
        routing="Provider selection, ordering, and fallback controls",
        observability="Usage and generation metadata with configurable content handling",
        content="Controlled by account settings and selected provider policy",
        verification="Published policies and provider metadata",
        billing="Prepaid credits and BYOK with a published purchase fee",
        sources=(
            ComparisonSource("OpenRouter documentation", "https://openrouter.ai/docs"),
            ComparisonSource("OpenRouter pricing", "https://openrouter.ai/pricing"),
        ),
        custom_page=True,
    ),
    _comparison(
        slug="vercel-ai-gateway",
        name="Vercel AI Gateway",
        category="Hosted AI gateway",
        summary=(
            "Vercel AI Gateway is a natural fit for Vercel and AI SDK applications. "
            "TrustedRouter is designed for teams that also need an independently verifiable "
            "prompt gateway and provider privacy policies."
        ),
        competitor_fit=(
            "Choose Vercel AI Gateway when your application already centers on Vercel and the AI "
            "SDK, and one integrated deployment and billing environment matters most."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when attestation, open-source prompt-path code, ZDR routing, "
            "and independently operated regional gateways are requirements."
        ),
        migration=(
            "OpenAI-compatible calls move with a base URL and key change. AI SDK applications can "
            "also call TrustedRouter through its OpenAI-compatible provider configuration."
        ),
        deployment="Hosted service integrated with Vercel and AI SDK",
        api="AI SDK, OpenAI Chat and Responses, and Anthropic Messages",
        catalog="Hundreds of text, image, video, and audio models",
        routing="Provider fallback and cost or latency routing",
        observability="Unified spend and usage inside Vercel",
        content="ZDR selection is available for eligible routes",
        verification="Hosted service controls and provider policy metadata",
        billing="Unified billing with published no-token-markup positioning",
        sources=(
            ComparisonSource("Vercel AI Gateway documentation", "https://vercel.com/docs/ai-gateway"),
            ComparisonSource("Vercel AI Gateway overview", "https://vercel.com/ai-gateway"),
        ),
        custom_page=True,
    ),
    _comparison(
        slug="litellm",
        name="LiteLLM",
        category="Self-hosted AI gateway",
        summary=(
            "LiteLLM gives teams a broad, configurable gateway they can operate themselves. "
            "TrustedRouter offers a managed path with billing, public status, and hardware "
            "attestation while remaining open source."
        ),
        competitor_fit=(
            "Choose LiteLLM when your team wants to own the gateway deployment, configure every "
            "provider adapter, and operate its scaling and availability directly."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want a managed marketplace and gateway whose running "
            "release can be verified without operating the router yourself."
        ),
        migration=(
            "Both expose OpenAI-compatible interfaces. Change the base URL, API key, and model "
            "identifier mapping; remove deployment-specific virtual-key headers if present."
        ),
        deployment="Open-source proxy operated in your infrastructure or via partners",
        api="OpenAI-compatible proxy plus broad provider adapters",
        catalog="Large adapter catalog across more than 100 providers",
        routing="Configurable retries, fallbacks, budgets, and load balancing",
        observability="Callbacks and integrations with external observability systems",
        content="Determined by your deployment, callbacks, and configured providers",
        verification="Your infrastructure controls and deployment audit process",
        billing="Your provider accounts and optional commercial platform services",
        sources=(
            ComparisonSource("LiteLLM documentation", "https://docs.litellm.ai/"),
            ComparisonSource("LiteLLM source", "https://github.com/BerriAI/litellm"),
        ),
        custom_page=True,
    ),
    _comparison(
        slug="cloudflare-ai-gateway",
        name="Cloudflare AI Gateway",
        category="Hosted edge AI gateway",
        summary=(
            "Cloudflare combines its global network, gateway controls, Workers AI, and unified "
            "billing. TrustedRouter focuses on open-source routing with privacy proof and explicit "
            "provider-level privacy selection."
        ),
        competitor_fit=(
            "Choose Cloudflare AI Gateway when Cloudflare is already your edge platform and you "
            "want its caching, rate limiting, logging, Workers AI, and unified billing together."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the gateway build itself must be attested and realtime "
            "prompt content must stay out of durable gateway logs."
        ),
        migration=(
            "Both support OpenAI-compatible chat and Responses calls. Replace the Cloudflare "
            "account URL and gateway header with the TrustedRouter base URL and API key."
        ),
        deployment="Hosted on Cloudflare's global network",
        api="REST envelope, OpenAI Chat and Responses, and Anthropic Messages",
        catalog="Third-party models plus Cloudflare Workers AI",
        routing="Gateway policies, caching, rate limiting, and provider access",
        observability="Gateway logging and analytics integrated with Cloudflare",
        content="Logging behavior follows the selected gateway configuration",
        verification="Cloudflare account controls and service documentation",
        billing="Cloudflare unified billing or provider credentials",
        sources=(
            ComparisonSource(
                "Cloudflare AI Gateway REST API",
                "https://developers.cloudflare.com/ai-gateway/usage/rest-api/",
            ),
            ComparisonSource(
                "Cloudflare AI Gateway documentation",
                "https://developers.cloudflare.com/ai-gateway/",
            ),
        ),
    ),
    _comparison(
        slug="portkey",
        name="Portkey",
        category="AI gateway and observability",
        summary=(
            "Portkey combines a mature gateway, guardrails, prompt management, and deep "
            "observability. TrustedRouter prioritizes content-stateless realtime routing and "
            "hardware-verifiable gateway execution."
        ),
        competitor_fit=(
            "Choose Portkey when integrated logs, traces, guardrails, prompt management, and "
            "enterprise gateway configuration are central to your operating model."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when privacy with proof is the primary control and observability "
            "should remain metadata-only unless you explicitly export content."
        ),
        migration=(
            "Move an OpenAI-compatible client to the TrustedRouter base URL, replace Portkey "
            "headers with a TrustedRouter key, and translate gateway configs into routing policy."
        ),
        deployment="Hosted, open-source gateway, or enterprise private deployment",
        api="Universal OpenAI-compatible gateway and provider adapters",
        catalog="Hundreds of models with gateway configuration targets",
        routing="Fallbacks, conditional routes, cache, retries, and circuit breakers",
        observability="Detailed logs, analytics, traces, and OpenTelemetry",
        content="Full logging or metrics-only privacy mode",
        verification="Deployment controls, policies, and private infrastructure options",
        billing="Platform plans plus provider billing or managed integrations",
        sources=(
            ComparisonSource("Portkey AI Gateway", "https://portkey.ai/docs/product/ai-gateway"),
            ComparisonSource(
                "Portkey request logging controls",
                "https://portkey.ai/docs/product/administration/configuring-request-logging",
            ),
        ),
    ),
    _comparison(
        slug="helicone",
        name="Helicone",
        category="AI gateway and observability",
        summary=(
            "Helicone is built around LLM observability and a proxy gateway. TrustedRouter is a "
            "model marketplace and router whose realtime content path is designed to be "
            "hardware-attested and content-stateless."
        ),
        competitor_fit=(
            "Choose Helicone when request-level debugging, evaluation, experimentation, and rich "
            "observability across existing provider accounts are the main job."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when unified prepaid access, provider rollover, and gateway "
            "privacy proof matter more than retaining prompt-level observability."
        ),
        migration=(
            "Replace the Helicone gateway URL and target headers with the TrustedRouter base URL. "
            "Move needed metadata analytics to tags or Broadcast destinations."
        ),
        deployment="Hosted or self-hosted open-source observability platform",
        api="Proxy gateway preserving provider API shapes",
        catalog="Routes to supported provider domains using your credentials",
        routing="Provider proxying with gateway controls",
        observability="Request logs, evaluations, experiments, and cost analytics",
        content="Content handling follows logging and deployment configuration",
        verification="Self-hosted auditability or hosted service controls",
        billing="Platform usage plus direct provider billing",
        sources=(
            ComparisonSource(
                "Helicone gateway documentation",
                "https://docs.helicone.ai/getting-started/integration-method/gateway",
            ),
            ComparisonSource("Helicone source", "https://github.com/Helicone/helicone"),
        ),
    ),
    _comparison(
        slug="requesty",
        name="Requesty",
        category="Hosted AI gateway",
        summary=(
            "Requesty offers a hosted gateway with cost-aware routing, caching, EU residency, "
            "observability, and ZDR controls. TrustedRouter adds open-source attested execution "
            "for the gateway hop and specialized privacy aliases."
        ),
        competitor_fit=(
            "Choose Requesty when its routing policies, EU-hosted product surface, enterprise "
            "controls, or supported catalog best match your application."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want comparable hosted convenience with a live "
            "attestation record and zero durable content logging on realtime inference."
        ),
        migration=(
            "Both are OpenAI-compatible hosted gateways. Change the base URL and key, then map "
            "Requesty routing policies to a TrustedRouter alias or provider controls."
        ),
        deployment="Hosted gateway with EU data-residency options",
        api="OpenAI-compatible router endpoint",
        catalog="More than 600 models across more than 20 providers",
        routing="Policies, fallbacks, load balancing, caching, and latency routing",
        observability="Spend tracking, analytics, logs, and enterprise audit controls",
        content="Configurable ZDR mode; encrypted retention when logging is enabled",
        verification="Service controls, policy documents, and compliance program",
        billing="Published 5% pay-as-you-go model markup plus BYOK",
        sources=(
            ComparisonSource("Requesty pricing and features", "https://www.requesty.ai/pricing"),
            ComparisonSource("Requesty privacy policy", "https://www.requesty.ai/privacy"),
        ),
    ),
    _comparison(
        slug="aws-bedrock",
        name="Amazon Bedrock",
        category="Cloud model platform",
        summary=(
            "Amazon Bedrock provides AWS-native model access, IAM, geographic inference profiles, "
            "and several API shapes. TrustedRouter provides a cloud-neutral model catalog with one "
            "key and an attested gateway front door."
        ),
        competitor_fit=(
            "Choose Bedrock when AWS IAM, private networking, regional controls, committed cloud "
            "spend, or Bedrock-specific agents and guardrails are core requirements."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need routes spanning clouds and specialist inference "
            "providers without maintaining regional model deployments and credentials."
        ),
        migration=(
            "Bedrock's OpenAI-compatible endpoints can move directly. Converse or InvokeModel "
            "applications need a request-shape adapter before changing the base URL and key."
        ),
        deployment="AWS regional managed service",
        api="Converse, InvokeModel, Chat Completions, Responses, and Messages by model",
        catalog="AWS-selected foundation models with regional availability",
        routing="Geographic and global cross-region inference profiles",
        observability="CloudWatch, CloudTrail, tags, and AWS cost tooling",
        content="Controlled by AWS service terms, model, region, and abuse-monitoring posture",
        verification="AWS IAM, service controls, audit logs, and cloud compliance evidence",
        billing="Direct AWS metering and optional provisioned throughput",
        sources=(
            ComparisonSource(
                "Amazon Bedrock API compatibility",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html",
            ),
            ComparisonSource(
                "Bedrock cross-region inference",
                "https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html",
            ),
        ),
    ),
    _comparison(
        slug="azure-ai-foundry",
        name="Microsoft Foundry",
        category="Cloud model platform",
        summary=(
            "Microsoft Foundry combines Azure deployment controls with a trained model router and "
            "model catalog. TrustedRouter offers a cloud-neutral API and auditable gateway for "
            "teams that do not want routing tied to one cloud account."
        ),
        competitor_fit=(
            "Choose Microsoft Foundry when Azure networking, identity, content filters, data zones, "
            "and enterprise procurement are already the center of your AI platform."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when provider diversity, rapid catalog changes, and independent "
            "gateway attestation matter more than Azure-native deployment management."
        ),
        migration=(
            "OpenAI-compatible deployments can move with endpoint, key, and model-name changes. "
            "Azure deployment names and model-router settings need explicit mapping."
        ),
        deployment="Azure resource deployments with global or data-zone options",
        api="Azure OpenAI-compatible endpoints and Foundry service APIs",
        catalog="Microsoft-selected proprietary and open models",
        routing="Trained model router with quality, balanced, or cost modes",
        observability="Azure Monitor, resource controls, and enterprise governance",
        content="Honors selected Azure deployment and data-zone boundaries",
        verification="Azure identity, deployment configuration, and compliance evidence",
        billing="Azure metering, quotas, and enterprise cloud agreements",
        sources=(
            ComparisonSource(
                "Microsoft Foundry model router",
                "https://learn.microsoft.com/azure/foundry/openai/concepts/model-router",
            ),
            ComparisonSource(
                "Using the Foundry model router",
                "https://learn.microsoft.com/azure/foundry/openai/how-to/model-router",
            ),
        ),
    ),
    _comparison(
        slug="google-vertex-ai",
        name="Google Vertex AI",
        category="Cloud model platform",
        summary=(
            "Vertex AI provides Google Cloud-native access to Gemini and Model Garden with IAM and "
            "regional controls. TrustedRouter adds a single catalog across Google and competing "
            "providers with attested routing."
        ),
        competitor_fit=(
            "Choose Vertex AI when Google Cloud identity, private networking, Gemini integration, "
            "or committed GCP spend determines your deployment."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the same application must compare and fail over across "
            "Google, open-weight, and frontier providers through one policy surface."
        ),
        migration=(
            "Vertex OpenAI-compatible calls need endpoint, project authentication, key, and model "
            "mapping changes. Native Vertex SDK calls require an OpenAI-compatible adapter."
        ),
        deployment="Google Cloud regional managed model platform",
        api="Vertex SDKs plus OpenAI-compatible endpoints for supported models",
        catalog="Gemini, partner models, and deployable Model Garden models",
        routing="Regional endpoints, quotas, and application-managed fallback",
        observability="Cloud Logging, Monitoring, audit logs, and billing export",
        content="Controlled by Google Cloud service terms and selected partner model",
        verification="Google Cloud IAM, audit evidence, and deployment controls",
        billing="Direct Google Cloud metering and committed-spend programs",
        sources=(
            ComparisonSource(
                "Vertex AI OpenAI-compatible API",
                "https://cloud.google.com/vertex-ai/generative-ai/docs/start/openai",
            ),
            ComparisonSource("Vertex AI Model Garden", "https://cloud.google.com/model-garden"),
        ),
    ),
    _comparison(
        slug="tinfoil",
        name="Tinfoil",
        category="Confidential AI inference",
        summary=(
            "Tinfoil provides a deeply verified confidential model path, including router and "
            "model-enclave attestation. TrustedRouter provides a broader router marketplace and "
            "can select fully verified E2E routes when that privacy floor is required."
        ),
        competitor_fit=(
            "Choose Tinfoil when its available model catalog fits and client-side verification of "
            "the router, model enclave, model weights, and confidential GPU is the top priority."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you need hundreds of models, many ordinary ZDR routes, and "
            "a policy that can prefer Tinfoil-class E2E routes while retaining broader fallback."
        ),
        migration=(
            "Both expose OpenAI-compatible inference. Tinfoil's verification SDK and model names "
            "must be replaced with a TrustedRouter key, base URL, and chosen privacy alias."
        ),
        deployment="Hosted confidential model router and confidential model enclaves",
        api="OpenAI-compatible private inference with verification SDKs",
        catalog="Focused set of models deployed in confidential GPU enclaves",
        routing="Attested router load balances across verified model enclaves",
        observability="Minimal usage metadata designed around enclave confidentiality",
        content="Technically enforced no-retention confidential inference",
        verification="Client verifies router, model enclave, code, weights, GPU, and key binding",
        billing="Managed confidential inference pricing",
        sources=(
            ComparisonSource("Tinfoil technology", "https://tinfoil.sh/technology"),
            ComparisonSource(
                "Tinfoil attestation architecture",
                "https://docs.tinfoil.sh/verification/attestation-architecture",
            ),
        ),
    ),
    _comparison(
        slug="tensorzero",
        name="TensorZero",
        category="Self-hosted AI gateway",
        summary=(
            "TensorZero is an open-source inference and experimentation stack designed for teams "
            "building a data and learning flywheel. TrustedRouter is a managed marketplace focused "
            "on immediate model access, reliability, and privacy proof."
        ),
        competitor_fit=(
            "Choose TensorZero when you want GitOps configuration, self-hosted low-overhead routing, "
            "experimentation, feedback, and a long-term optimization loop in your own stack."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want managed credentials, prepaid routing, broad model "
            "availability, and attested hosted execution without operating the gateway."
        ),
        migration=(
            "Map TensorZero functions and variants to concrete TrustedRouter model IDs or combo "
            "models, then replace the gateway URL and authentication."
        ),
        deployment="Open-source Rust gateway in your infrastructure",
        api="Unified inference and feedback APIs centered on functions and variants",
        catalog="Configured provider and model integrations",
        routing="Variants, experimentation, and application-defined inference strategies",
        observability="ClickHouse-backed inference, feedback, and experiment data",
        content="Determined by your self-hosted storage and configuration",
        verification="Your deployment, source audit, and infrastructure controls",
        billing="Direct provider billing plus your own infrastructure",
        sources=(
            ComparisonSource("TensorZero gateway", "https://www.tensorzero.com/docs/gateway/"),
            ComparisonSource("TensorZero source", "https://github.com/tensorzero/tensorzero"),
        ),
    ),
    _comparison(
        slug="bifrost",
        name="Bifrost",
        category="Self-hosted AI gateway",
        summary=(
            "Bifrost is a high-performance open-source gateway for teams that want a Go package or "
            "HTTP service inside their own systems. TrustedRouter provides the hosted marketplace, "
            "billing, and attested public service around that class of routing."
        ),
        competitor_fit=(
            "Choose Bifrost when gateway overhead, Go integration, plugin control, and running the "
            "entire routing layer in your own environment are the primary concerns."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when you want one managed key, prepaid providers, continuous "
            "synthetics, and public release attestation instead of operating the gateway."
        ),
        migration=(
            "Replace the Bifrost endpoint and provider credentials with the TrustedRouter base URL "
            "and key. Translate fallback configuration into models, provider controls, or aliases."
        ),
        deployment="Open-source Go package or self-hosted HTTP gateway",
        api="Unified provider interface and OpenAI-compatible transport",
        catalog="Configured provider integrations using your credentials",
        routing="Fallback providers and plugin-driven gateway behavior",
        observability="Self-hosted telemetry and Maxim integrations",
        content="Determined by your plugins, telemetry, and deployment",
        verification="Your source audit and infrastructure controls",
        billing="Direct provider billing plus your infrastructure",
        sources=(
            ComparisonSource(
                "Bifrost overview",
                "https://www.getmaxim.ai/docs/bifrost/overview/get-started",
            ),
            ComparisonSource("Bifrost source", "https://github.com/maximhq/bifrost"),
        ),
    ),
    _comparison(
        slug="not-diamond",
        name="Not Diamond",
        category="Intelligent model router",
        summary=(
            "Not Diamond predicts which candidate model best fits each prompt and can train custom "
            "routers from evaluation data. TrustedRouter combines route selection with execution, "
            "billing, provider fallback, and privacy policies."
        ),
        competitor_fit=(
            "Choose Not Diamond when learned per-prompt model selection or a custom router trained "
            "on your own scored examples is the central requirement."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when the router must also execute requests, manage provider "
            "credentials and billing, enforce privacy floors, and expose attestation."
        ),
        migration=(
            "Replace the separate model-selection call and downstream provider execution with a "
            "TrustedRouter alias or combo model. Validate behavior on the same task-specific eval."
        ),
        deployment="Hosted routing API and SDK",
        api="Model-selection APIs plus Python, TypeScript, and REST clients",
        catalog="Candidate models supported by pretrained or custom routers",
        routing="Quality, cost, latency, and custom trained routing",
        observability="Router session IDs and selection results",
        content="Prompts are processed to make routing decisions",
        verification="Hosted service controls and published router documentation",
        billing="Routing service pricing plus downstream model execution costs",
        sources=(
            ComparisonSource(
                "Not Diamond concepts",
                "https://docs.notdiamond.ai/docs/key-concepts",
            ),
            ComparisonSource(
                "Not Diamond custom router",
                "https://docs.notdiamond.ai/docs/router-training-quickstart",
            ),
        ),
    ),
    _comparison(
        slug="martian",
        name="Martian Gateway",
        category="Hosted AI gateway",
        summary=(
            "Martian Gateway exposes a broad hosted model catalog and optimized model routes. "
            "TrustedRouter emphasizes verifiable gateway execution, explicit privacy classes, and "
            "open-source hosted infrastructure."
        ),
        competitor_fit=(
            "Choose Martian when its catalog, optimized route variants, or Martian-specific model "
            "selection best fits the application you are shipping."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when ZDR, E2E, EU, cheap, and automatic routing policies need to "
            "be enforced through an attested gateway."
        ),
        migration=(
            "Both support OpenAI-compatible chat. Replace the Martian base URL and key, then map "
            "provider-prefixed or optimized model identifiers to TrustedRouter IDs."
        ),
        deployment="Hosted multi-provider gateway",
        api="OpenAI-compatible and Anthropic-compatible endpoints",
        catalog="Broad model catalog with provider-prefixed identifiers",
        routing="Direct and optimized model routes",
        observability="Hosted dashboard usage and model metadata",
        content="Controlled by Martian and selected upstream provider terms",
        verification="Hosted service controls and provider documentation",
        billing="Managed gateway pricing across catalog routes",
        sources=(
            ComparisonSource(
                "Martian model catalog",
                "https://gateway-docs.withmartian.com/api-reference/models",
            ),
            ComparisonSource(
                "Martian LiteLLM integration",
                "https://gateway-docs.withmartian.com/integrations/litellm",
            ),
        ),
    ),
    _comparison(
        slug="kong-ai-gateway",
        name="Kong AI Gateway",
        category="Enterprise API gateway",
        summary=(
            "Kong extends an established enterprise API gateway with AI proxy, governance, "
            "observability, and routing plugins. TrustedRouter is a ready-to-use model marketplace "
            "with an attested prompt path."
        ),
        competitor_fit=(
            "Choose Kong when AI traffic must fit an existing Kong or Konnect estate with mature "
            "API governance, authentication, plugins, and platform-team ownership."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when developers need immediate model access and privacy proof "
            "without provisioning and operating a general-purpose enterprise gateway."
        ),
        migration=(
            "Point OpenAI-compatible clients at TrustedRouter and replace Kong consumer credentials. "
            "Recreate required governance using key limits, tags, provider policy, and Broadcast."
        ),
        deployment="Kong Gateway or Konnect control and data planes",
        api="Provider-agnostic chat and completion routes through AI plugins",
        catalog="Configured providers and self-hosted model targets",
        routing="AI Proxy Advanced load balancing and semantic routing",
        observability="Kong plugins, analytics, governance, and API telemetry",
        content="Determined by plugin configuration and deployment architecture",
        verification="Your gateway deployment controls or Konnect service controls",
        billing="Kong platform licensing plus direct provider billing",
        sources=(
            ComparisonSource(
                "Kong AI Gateway",
                "https://docs.konghq.com/gateway/latest/ai-gateway/",
            ),
            ComparisonSource(
                "Kong LLM routing recipe",
                "https://developer.konghq.com/cookbooks/basic-llm-routing/",
            ),
        ),
    ),
    _comparison(
        slug="lmrouter",
        name="LMRouter",
        category="Hosted model marketplace",
        summary=(
            "LMRouter provides OpenAI and Anthropic-compatible endpoints across text, image, audio, "
            "and other models with prepaid and BYOK access. TrustedRouter adds attested open-source "
            "gateway execution and privacy-specific routing aliases."
        ),
        competitor_fit=(
            "Choose LMRouter when its endpoint coverage, catalog, BYOK behavior, or published "
            "transaction-fee model best fits your workload."
        ),
        trustedrouter_fit=(
            "Choose TrustedRouter when realtime content-stateless routing and a live source-to-build "
            "attestation trail are required."
        ),
        migration=(
            "OpenAI-compatible calls usually move with a base URL, key, and model-ID change. "
            "Anthropic clients should use TrustedRouter's Messages endpoint."
        ),
        deployment="Hosted unified model API",
        api="OpenAI-compatible and Anthropic-compatible endpoints",
        catalog="Text, image, embedding, audio, and video model categories",
        routing="Model selection through a unified hosted endpoint",
        observability="Usage metadata for billing and monitoring",
        content="Transient processing with temporary cache for selected stateful features",
        verification="Hosted service controls and privacy policy",
        billing="No model markup; published 4.8% plus $0.35 credit top-up fee",
        sources=(
            ComparisonSource("LMRouter overview", "https://docs.lmrouter.com/features-overview"),
            ComparisonSource("LMRouter pricing", "https://docs.lmrouter.com/pricing"),
            ComparisonSource("LMRouter privacy", "https://docs.lmrouter.com/privacy"),
        ),
    ),
)


COMPETITOR_COMPARISON_BY_SLUG: dict[str, CompetitorComparison] = {
    comparison.slug: comparison for comparison in COMPETITOR_COMPARISONS
}


def competitor_comparison(slug: str) -> CompetitorComparison | None:
    return COMPETITOR_COMPARISON_BY_SLUG.get(slug.strip().lower())


def related_comparisons(
    comparison: CompetitorComparison,
    *,
    limit: int = 4,
) -> tuple[CompetitorComparison, ...]:
    same_category = [
        row
        for row in COMPETITOR_COMPARISONS
        if row.slug != comparison.slug and row.category == comparison.category
    ]
    others = [
        row
        for row in COMPETITOR_COMPARISONS
        if row.slug != comparison.slug and row.category != comparison.category
    ]
    return tuple((same_category + others)[:limit])
