"""Local provider branding used by public catalog and SEO pages.

Logos are vendored under ``static/provider-logos`` so public pages never make
third-party image requests. The homepage URLs are informational and are kept
separate from provider policy sources in the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderBrand:
    homepage_url: str
    description: str = ""
    company_details: tuple[tuple[str, str], ...] = ()
    resources: tuple[tuple[str, str], ...] = ()


PROVIDER_BRANDS: dict[str, ProviderBrand] = {
    "aion-labs": ProviderBrand("https://www.aionlabs.ai/"),
    "akashml": ProviderBrand("https://akashml.com/"),
    "arcee": ProviderBrand("https://www.arcee.ai/"),
    "baidu": ProviderBrand("https://intl.cloud.baidu.com/en/product/qianfan.html"),
    "byteplus": ProviderBrand("https://www.byteplus.com/en/product/modelark"),
    "inception": ProviderBrand("https://www.inceptionlabs.ai/"),
    "io-net": ProviderBrand("https://io.net/ai/"),
    "jina": ProviderBrand("https://jina.ai/"),
    "krea": ProviderBrand("https://www.krea.ai/"),
    "liquid": ProviderBrand("https://www.liquid.ai/"),
    "mancer": ProviderBrand("https://mancer.tech/"),
    "modal": ProviderBrand("https://modal.com/"),
    "nextbit": ProviderBrand("https://nextbit256.com/"),
    "perceptron": ProviderBrand("https://perceptron.cloud/"),
    "perplexity": ProviderBrand("https://www.perplexity.ai/"),
    "reka": ProviderBrand("https://www.reka.ai/"),
    "riverflow": ProviderBrand("https://www.riverflow.ai/"),
    "sail-research": ProviderBrand("https://www.sailresearch.com/"),
    "sakana": ProviderBrand("https://sakana.ai/"),
    "sambanova": ProviderBrand("https://sambanova.ai/"),
    "scaleway": ProviderBrand("https://www.scaleway.com/"),
    "regolo": ProviderBrand("https://regolo.ai/"),
    "privatemode": ProviderBrand("https://www.privatemode.ai/"),
    "upstage": ProviderBrand("https://www.upstage.ai/"),
    "alibaba": ProviderBrand("https://www.alibabacloud.com/"),
    "anthropic": ProviderBrand("https://www.anthropic.com/"),
    "atlas-cloud": ProviderBrand("https://www.atlascloud.ai/"),
    "azure": ProviderBrand("https://azure.microsoft.com/en-us/products/ai-foundry/"),
    "baseten": ProviderBrand("https://www.baseten.co/"),
    "bfl": ProviderBrand("https://bfl.ai/"),
    "cerebras": ProviderBrand("https://www.cerebras.ai/"),
    "chutes": ProviderBrand("https://chutes.ai/"),
    "cloudflare-workers-ai": ProviderBrand(
        "https://www.cloudflare.com/developer-platform/products/workers-ai/"
    ),
    "cohere": ProviderBrand("https://cohere.com/"),
    "crusoe": ProviderBrand("https://www.crusoe.ai/"),
    "deepinfra": ProviderBrand("https://deepinfra.com/"),
    "deepseek": ProviderBrand("https://www.deepseek.com/"),
    "darkbloom": ProviderBrand("https://darkbloom.dev/"),
    "digitalocean": ProviderBrand("https://www.digitalocean.com/products/gradient-ai-platform"),
    "engy": ProviderBrand("https://engy.ai/"),
    "fal": ProviderBrand("https://fal.ai/"),
    "featherless": ProviderBrand("https://featherless.ai/"),
    "databricks": ProviderBrand("https://www.databricks.com/"),
    "decart": ProviderBrand("https://decart.ai/"),
    "fireworks": ProviderBrand("https://fireworks.ai/"),
    "friendli": ProviderBrand("https://friendli.ai/"),
    "gmi": ProviderBrand("https://www.gmicloud.ai/"),
    "google-ai-studio": ProviderBrand("https://ai.google.dev/"),
    "google-vertex": ProviderBrand("https://cloud.google.com/vertex-ai"),
    "grok": ProviderBrand("https://x.ai/"),
    "huggingface": ProviderBrand("https://huggingface.co/"),
    "inceptron": ProviderBrand("https://www.inceptron.io/"),
    "kimi": ProviderBrand("https://www.kimi.com/"),
    "kling": ProviderBrand("https://kling.ai/"),
    "lightning": ProviderBrand("https://lightning.ai/"),
    "ltx": ProviderBrand("https://ltx.io/"),
    "makora": ProviderBrand("https://www.makora.com/"),
    "meta": ProviderBrand("https://www.meta.com/"),
    "openrouter": ProviderBrand("https://openrouter.ai"),
    "minimax": ProviderBrand("https://www.minimax.io/"),
    "mistral": ProviderBrand("https://mistral.ai/"),
    "morph": ProviderBrand("https://www.morphllm.com/"),
    "nebius": ProviderBrand("https://nebius.com/ai-studio"),
    "neurometric": ProviderBrand("https://www.neurometric.ai/"),
    "nvidia-nim": ProviderBrand("https://build.nvidia.com/"),
    "novita": ProviderBrand("https://novita.ai/"),
    "nscale": ProviderBrand("https://www.nscale.com/"),
    "confidential-ai": ProviderBrand("https://confidential.ai/"),
    "scaledown": ProviderBrand("https://scaledown.ai/"),
    "near-ai": ProviderBrand("https://near.ai/"),
    "openai": ProviderBrand("https://openai.com/"),
    "ovhcloud": ProviderBrand("https://www.ovhcloud.com/"),
    "parasail": ProviderBrand("https://www.parasail.io/"),
    "pearl": ProviderBrand("https://pearlresearch.ai/"),
    "phala": ProviderBrand("https://phala.network/"),
    "poolside": ProviderBrand("https://poolside.ai/"),
    "runway": ProviderBrand("https://runwayml.com/"),
    "recraft": ProviderBrand("https://www.recraft.ai/"),
    "relace": ProviderBrand("https://relace.ai/"),
    "siliconflow": ProviderBrand("https://www.siliconflow.com/"),
    "streamlake": ProviderBrand("https://www.streamlake.ai/"),
    "stepfun": ProviderBrand("https://www.stepfun.com/"),
    "telnyx": ProviderBrand(
        "https://telnyx.com/products/inference",
        description=(
            "Telnyx Inference serves OpenAI-compatible chat completions on "
            "Telnyx-owned GPUs. Marketplace routes include Telnyx-hosted models "
            "only, not its third-party BYOK passthrough catalog."
        ),
        # Company information supplied in Telnyx's marketplace application.
        # Operational contacts and personal phone numbers are not public metadata.
        company_details=(
            ("Legal entity", "Telnyx LLC; Illinois, USA limited liability company"),
            ("Headquarters", "Austin, Texas, USA"),
            ("Registered and operating address", "600 Congress Ave, Floor 14, Austin, TX 78701, USA"),
            ("DUNS", "966115342"),
            ("EIN", "27-0273220"),
            ("CEO", "David Casem"),
            ("Serving regions", "USA, EU, Australia and UAE; availability varies by model. Region availability alone is not a residency guarantee."),
            ("API", "OpenAI-compatible chat completions, streaming, tools, structured output and reasoning content on supported models."),
            ("Catalog", "Authenticated native OpenAI catalog; not yet a Provider Reliability Contract v2 declaration."),
            ("Pricing", "Input, output and cached-input rates refresh hourly from Telnyx's authenticated APIs. Listed routes use default service-tier prices; priority and flex tiers are not enabled through TrustedRouter."),
            ("Policy precedence", "For Personal Data, DPA 12.1 gives precedence to the SCCs and UK Addendum, then the DPA, then the AI Addendum, then the remaining Agreement. Scope and exceptions remain governed by those documents."),
            ("Policy reviewed", "September 22, 2026. Published endpoint-specific ZDR and DPA v3.0; provider statements, not hardware-attested guarantees."),
        ),
        resources=(
            ("API catalog", "https://api.telnyx.com/v2/ai/openai/models"),
            ("Inference pricing", "https://telnyx.com/pricing/inference-api"),
            ("Machine-readable pricing", "https://api.telnyx.com/v2/pricing/products/inference"),
            ("Content retention policy", "https://telnyx.com/privacy-policy"),
            ("Inference retention and endpoint scope", "https://developers.telnyx.com/docs/inference/data-residency"),
            ("Data locality", "https://developers.telnyx.com/docs/account-setup/data-locality"),
            ("Terms of service", "https://telnyx.com/terms-and-conditions-of-service"),
            ("DPA: training and precedence", "https://telnyx.com/legal/data-processing-addendum"),
            ("Subprocessors", "https://telnyx.com/legal/subprocessors"),
            ("Trust center and compliance reports", "https://trust.telnyx.com/"),
            ("Service status", "https://status.telnyx.com"),
            ("Support", "https://support.telnyx.com"),
        ),
    ),
    "tencent": ProviderBrand("https://www.tencentcloud.com/products/tokenhub"),
    "thinkingmachines": ProviderBrand("https://thinkingmachines.ai/"),
    "telluvian": ProviderBrand("https://telluvian.ai/"),
    "tinfoil": ProviderBrand("https://tinfoil.sh/"),
    "together": ProviderBrand("https://www.together.ai/"),
    "trustedrouter": ProviderBrand("https://trustedrouter.com/"),
    "typesafe": ProviderBrand("https://typesafe.ai/"),
    "venice": ProviderBrand("https://venice.ai/"),
    "vercel-ai-gateway": ProviderBrand("https://vercel.com/ai-gateway"),
    "voyage": ProviderBrand("https://www.voyageai.com/"),
    "vultr": ProviderBrand("https://www.vultr.com/"),
    "wafer": ProviderBrand("https://wafer.ai/"),
    "wandb": ProviderBrand("https://wandb.ai/site/inference/"),
    "xiaomi": ProviderBrand("https://www.mi.com/"),
    "zai": ProviderBrand("https://z.ai/"),
    "zero-g": ProviderBrand("https://0g.ai/"),
}


def provider_logo_url(provider_slug: str) -> str:
    """Return a local logo URL, with a local fallback for unknown sample rows."""
    if provider_slug in PROVIDER_BRANDS:
        return f"/static/provider-logos/{provider_slug}.png"
    return "/static/favicon.svg"


def provider_homepage_url(provider_slug: str) -> str | None:
    brand = PROVIDER_BRANDS.get(provider_slug)
    return brand.homepage_url if brand else None


def provider_og_image_url(provider_slug: str) -> str:
    """Return the static provider card path used by Open Graph and Twitter."""
    return f"/static/og/providers/{provider_slug}.png"
