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


PROVIDER_BRANDS: dict[str, ProviderBrand] = {
    "aion-labs": ProviderBrand("https://www.aionlabs.ai/"),
    "akashml": ProviderBrand("https://akashml.com/"),
    "arcee": ProviderBrand("https://www.arcee.ai/"),
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
    "digitalocean": ProviderBrand("https://www.digitalocean.com/products/gradient-ai-platform"),
    "engy": ProviderBrand("https://engy.ai/"),
    "featherless": ProviderBrand("https://featherless.ai/"),
    "databricks": ProviderBrand("https://www.databricks.com/"),
    "decart": ProviderBrand("https://decart.ai/"),
    "fireworks": ProviderBrand("https://fireworks.ai/"),
    "friendli": ProviderBrand("https://friendli.ai/"),
    "gmi": ProviderBrand("https://www.gmicloud.ai/"),
    "google-ai-studio": ProviderBrand("https://ai.google.dev/"),
    "google-vertex": ProviderBrand("https://cloud.google.com/vertex-ai"),
    "grok": ProviderBrand("https://x.ai/"),
    "inceptron": ProviderBrand("https://www.inceptron.io/"),
    "kimi": ProviderBrand("https://www.kimi.com/"),
    "kling": ProviderBrand("https://kling.ai/"),
    "lightning": ProviderBrand("https://lightning.ai/"),
    "ltx": ProviderBrand("https://ltx.io/"),
    "makora": ProviderBrand("https://www.makora.com/"),
    "meta": ProviderBrand("https://www.meta.com/"),
    "minimax": ProviderBrand("https://www.minimax.io/"),
    "mistral": ProviderBrand("https://mistral.ai/"),
    "morph": ProviderBrand("https://www.morphllm.com/"),
    "nebius": ProviderBrand("https://nebius.com/ai-studio"),
    "neurometric": ProviderBrand("https://www.neurometric.ai/"),
    "nvidia-nim": ProviderBrand("https://build.nvidia.com/"),
    "novita": ProviderBrand("https://novita.ai/"),
    "openai": ProviderBrand("https://openai.com/"),
    "openrouter-exclusive": ProviderBrand("https://openrouter.ai/stealth/ox-alpha"),
    "ovhcloud": ProviderBrand("https://www.ovhcloud.com/"),
    "parasail": ProviderBrand("https://www.parasail.io/"),
    "pearl": ProviderBrand("https://pearlresearch.ai/"),
    "phala": ProviderBrand("https://phala.network/"),
    "runway": ProviderBrand("https://runwayml.com/"),
    "recraft": ProviderBrand("https://www.recraft.ai/"),
    "relace": ProviderBrand("https://relace.ai/"),
    "siliconflow": ProviderBrand("https://www.siliconflow.com/"),
    "streamlake": ProviderBrand("https://www.streamlake.ai/"),
    "stepfun": ProviderBrand("https://www.stepfun.com/"),
    "telnyx": ProviderBrand("https://telnyx.com/products/inference"),
    "thinkingmachines": ProviderBrand("https://thinkingmachines.ai/"),
    "tinfoil": ProviderBrand("https://tinfoil.sh/"),
    "together": ProviderBrand("https://www.together.ai/"),
    "trustedrouter": ProviderBrand("https://trustedrouter.com/"),
    "venice": ProviderBrand("https://venice.ai/"),
    "voyage": ProviderBrand("https://www.voyageai.com/"),
    "vultr": ProviderBrand("https://www.vultr.com/"),
    "wafer": ProviderBrand("https://wafer.ai/"),
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
