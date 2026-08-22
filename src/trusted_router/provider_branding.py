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
    "alibaba": ProviderBrand("https://www.alibabacloud.com/"),
    "anthropic": ProviderBrand("https://www.anthropic.com/"),
    "atlas-cloud": ProviderBrand("https://www.atlascloud.ai/"),
    "baseten": ProviderBrand("https://www.baseten.co/"),
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
    "databricks": ProviderBrand("https://www.databricks.com/"),
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
    "novita": ProviderBrand("https://novita.ai/"),
    "openai": ProviderBrand("https://openai.com/"),
    "openrouter-exclusive": ProviderBrand("https://openrouter.ai/stealth/ox-alpha"),
    "parasail": ProviderBrand("https://www.parasail.io/"),
    "pearl": ProviderBrand("https://pearlresearch.ai/"),
    "phala": ProviderBrand("https://phala.network/"),
    "runway": ProviderBrand("https://runwayml.com/"),
    "siliconflow": ProviderBrand("https://www.siliconflow.com/"),
    "streamlake": ProviderBrand("https://www.streamlake.ai/"),
    "telnyx": ProviderBrand("https://telnyx.com/products/inference"),
    "thinkingmachines": ProviderBrand("https://thinkingmachines.ai/"),
    "tinfoil": ProviderBrand("https://tinfoil.sh/"),
    "together": ProviderBrand("https://www.together.ai/"),
    "trustedrouter": ProviderBrand("https://trustedrouter.com/"),
    "venice": ProviderBrand("https://venice.ai/"),
    "voyage": ProviderBrand("https://www.voyageai.com/"),
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
