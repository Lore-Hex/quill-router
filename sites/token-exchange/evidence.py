"""Render sourced snapshots without network access or application imports."""
import html
import json
from datetime import datetime
from pathlib import Path


def date_label(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d %b %Y · %H:%M UTC')


def render_evidence(data=None, compact=False):
    if data is None:
        data = json.loads(Path(__file__).with_name('evidence.json').read_text())
    catalog_url = html.escape(data.get('catalog_url', 'https://trustedrouter.com/providers'), quote=True)
    status_url = html.escape(data.get('status_url', 'https://trustedrouter.com/status'), quote=True)
    columns = []
    for route in data['routes']:
        prices = [float(route['pricing'][key]) * 1_000_000 for key in ('prompt', 'completion')]
        name, description = html.escape(route['label']), html.escape(route['provider'])
        columns.append(
            '<a class="privacy-column" href="' + catalog_url + '"><div class="privacy-choice"><h3>' + name
            + ' <svg class="link-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 19 19 5M5 5h14v14"/></svg></h3><p>' + description + '</p></div><div class="privacy-provider">'
            + html.escape(route['privacy']) + '</div><div class="privacy-price">'
            + f'≈ ${prices[0]:.2f} / ${prices[1]:.2f}</div></a>'
        )
    catalogue = (
        '<div class="catalogue evidence-reveal"><p class="catalogue-label">Confidential models</p><p class="catalogue-summary">Tinfoil · Confidential + E2EE</p>'
        '<div class="privacy-comparison">' + ''.join(columns) + '</div>'
        '<p class="pricing-unit">USD / 1M tokens · input / output</p>'
        + ('' if compact else '<p class="pricing-unit">As of ' + html.escape(date_label(data['captured_at'])) + '</p>') + '</div>'
    )
    services = [*data.get('service_history', []), data['status']]
    rows, present = [], set()
    for service in services:
        bars, counts = [], {'up': 0, 'degraded': 0, 'down': 0, 'unknown': 0}
        for bucket in service['history']:
            state = bucket['status'] if bucket['status'] in counts else 'unknown'
            if not bucket['sample_count']:
                state = 'unknown'
            counts[state] += 1
            title = f"{date_label(bucket['bucket_start'])}: {state}"
            bars.append(f'<span class="health-bar {state}" title="{html.escape(title)}"></span>')
        present.update(state for state, count in counts.items() if count)
        summary = ', '.join(f'{count} {state}' for state, count in counts.items() if count)
        uptime = '—' if service['uptime'] is None else f"{service['uptime']:.2f}<small>%</small>"
        name = html.escape(service['name'])
        rows.append('<div class="service-history"><div class="service-heading"><span>' + name
                    + '</span><strong>' + uptime + '</strong></div>'
                    + '<div class="health-bars" role="img" aria-label="' + name + ': hourly history, '
                    + html.escape(summary, quote=True) + '">' + ''.join(bars) + '</div></div>')
    key = ''.join('<span><i class="' + state + '"></i>' + label + '</span>'
                  for state, label in (('up', 'Operational'), ('degraded', 'Degraded'),
                                       ('down', 'Down'), ('unknown', 'No data')) if state in present)
    arrow = '<svg class="link-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 19 19 5M5 5h14v14"/></svg>'
    health = ('<div class="trust-evidence"><figure class="uptime-panel evidence-reveal">'
              '<figcaption>' + html.escape(data.get('uptime_label', 'Uptime')) + '</figcaption>'
              + ''.join(rows) + '<div class="health-key">' + key + '</div>'
              '<div class="evidence-footer"><a href="' + status_url + '">View current status ' + arrow + '</a></div></figure>')
    release = data.get('attestation')
    if release:
        digest = html.escape(release.get('measurement') or release['image_digest'])
        review_url = html.escape(release.get('review_url', 'https://trustedrouter.com/trust'), quote=True)
        health += ('<section class="attestation-panel evidence-reveal" aria-labelledby="attestation-title">'
                   '<h3 id="attestation-title">Attestation</h3><p class="digest-label">' + html.escape(release.get('measurement_label', 'Published build digest')) + '</p>'
                   '<code class="build-digest">' + digest + '</code>'
                   '<dl><div><dt>Gateway</dt><dd>' + html.escape(release.get('gateway', 'GCP Confidential Space')) + '</dd></div>'
                   '<div><dt>' + html.escape(release.get('source_label', 'Open-source build')) + '</dt><dd>' + html.escape(release['source_commit'][:8]) + '</dd></div>'
                   '<div><dt>Verification</dt><dd>' + html.escape(release.get('verification', 'TLS-bound attestation')) + '</dd></div>'
                   '<div><dt>Location</dt><dd>Not proven by attestation</dd></div></dl>'
                   '<div class="evidence-footer"><a href="' + review_url + '">' + html.escape(release.get('review_label', 'Verify live attestation')) + ' ' + arrow + '</a></div></section>')
    health += ('</div><p class="evidence-provenance">As of '
               + html.escape(date_label(data['captured_at']))
               + ' · 24-hour snapshot</p>')
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
