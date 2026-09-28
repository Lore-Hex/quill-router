"""Render empty evidence panels; request-time data is the only value source."""
import html


def render_evidence(profile='shared-gcp'):
    origin = 'https://azure.trustedrouter.com' if profile == 'dubai' else 'https://trustedrouter.com'
    label = 'Azure uptime' if profile == 'dubai' else ('Shared GCP uptime' if profile == 'shared-gcp' else 'GCP uptime')
    release = ('https://trust.trustedrouter.com/trust/azure-release.json' if profile == 'dubai'
               else 'https://trustedrouter.com/trust/gcp-release.json')
    catalogue = (
        '<div class="catalogue evidence-reveal"><p class="catalogue-label">Confidential models</p>'
        '<p class="catalogue-summary">Tinfoil · Confidential + E2EE</p>'
        '<div class="privacy-comparison" data-live-prices>'
        '<a href="https://trustedrouter.com/models?filter=e2e">View current Tinfoil prices</a></div>'
        '<p class="pricing-unit">USD / 1M tokens · input / output</p></div>'
    )
    health = (
        '<div class="trust-evidence" data-evidence-profile="' + html.escape(profile, quote=True) + '">'
        '<figure class="uptime-panel evidence-reveal"><figcaption>' + label + ' · 24h</figcaption>'
        '<p class="evidence-state" data-live-state>Status unavailable</p>'
        '<div data-live-services></div><div class="evidence-footer"><a href="' + origin + '/status">View current status ↗</a></div></figure>'
        '<section class="attestation-panel evidence-reveal" aria-labelledby="attestation-title">'
        '<h3 id="attestation-title">Attestation</h3><div data-live-attestation>Check unavailable</div>'
        '<div class="evidence-footer"><a href="' + release + '">Review published release ↗</a></div></section></div>'
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
