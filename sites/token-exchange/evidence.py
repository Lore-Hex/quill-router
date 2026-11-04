"""Render empty evidence panels; request-time data is the only value source."""


def render_evidence():
    catalogue = (
        '<div class="catalogue evidence-reveal"><p class="catalogue-label">Confidential models</p>'
        '<p class="catalogue-summary">Tinfoil · Confidential + E2EE</p>'
        '<div class="privacy-comparison" data-live-prices>'
        '<a href="https://trustedrouter.com/models?filter=e2e">Browse Tinfoil model routes</a></div>'
        '<p class="pricing-unit" data-live-price-unit hidden>USD / 1M tokens · input / output</p></div>'
    )
    health = (
        '<div class="trust-evidence" data-evidence-profile="shared-gcp">'
        '<figure class="uptime-panel evidence-reveal"><figcaption data-live-caption data-uptime-label="Shared GCP uptime · 24h">Shared GCP service status</figcaption>'
        '<div data-live-services></div><div class="evidence-footer"><a href="https://trustedrouter.com/status">Service status</a></div></figure>'
        '<section class="attestation-panel evidence-reveal" aria-labelledby="attestation-title">'
        '<h3 id="attestation-title">Attestation</h3><div data-live-attestation></div>'
        '<div class="evidence-footer"><a href="https://trustedrouter.com/trust/gcp-release.json">Published release</a></div></section></div>'
    )
    return catalogue, health


# Editorial illustrations, not measured charts. Hidden from assistive technology.
SECTOR_ART = (
    '<path d="M5 3h10l4 4v5M15 3v4h4M5 3v18h7M8 8h4M8 12h3"/>'
    '<circle cx="16" cy="16" r="4"/><path d="m19 19 3 3"/>',
    '<path d="M5 3h10l4 4v6M15 3v4h4M5 3v18h7M8 9h5M8 13h3m2 5 3 3 6-7"/>',
    '<rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 10h18M7 15h3m4 0h3"/>',
)


def sector_art(index):
    return ('<div class="sector-art" aria-hidden="true"><svg viewBox="0 0 24 24" '
            'focusable="false">' + SECTOR_ART[index] + '</svg></div>')
