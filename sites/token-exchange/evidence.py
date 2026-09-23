"""Render sourced snapshots without network access or application imports."""
import html
import json
from datetime import datetime
from pathlib import Path


def date_label(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d %b %Y · %H:%M UTC')


def render_evidence(data=None):
    if data is None:
        data = json.loads(Path(__file__).with_name('evidence.json').read_text())
    columns = []
    for route in data['routes']:
        prices = [float(route['pricing'][key]) * 1_000_000 for key in ('prompt', 'completion')]
        name, description = html.escape(route['label']), html.escape(route['provider'])
        columns.append(
            '<a class="privacy-column" href="https://trustedrouter.com/providers"><div class="privacy-choice"><h3>' + name
            + ' <svg class="link-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 19 19 5M5 5h14v14"/></svg></h3><p>' + description + '</p></div><div class="privacy-provider">'
            + html.escape(route['privacy']) + '</div><div class="privacy-price">'
            + f'≈ ${prices[0]:.2f} / ${prices[1]:.2f}</div></a>'
        )
    catalogue = (
        '<div class="catalogue evidence-reveal">'
        '<div class="privacy-comparison">' + ''.join(columns) + '</div>'
        '<p class="pricing-unit">USD / 1M tokens · input / output<br>As of '
        + html.escape(date_label(data['captured_at'])) + '</p></div>'
    )
    status = data['status']
    bars = []
    counts = {'up': 0, 'degraded': 0, 'down': 0, 'unknown': 0}
    for bucket in status['history']:
        state = bucket['status'] if bucket['status'] in counts else 'unknown'
        if not bucket['sample_count']:
            state = 'unknown'
        counts[state] += 1
        title = f"{date_label(bucket['bucket_start'])}: {state}"
        bars.append(f'<span class="health-bar {state}" title="{html.escape(title)}"></span>')
    uptime = '—' if status['uptime'] is None else f"{status['uptime']:.2f}<small>%</small>"
    summary = ', '.join(f'{count} {state}' for state, count in counts.items() if count)
    health = (
        '<figure class="health-panel evidence-reveal"><figcaption>US East Regional API</figcaption>'
        '<div class="health-metric"><strong>' + uptime + '</strong><span>24-hour availability</span></div>'
        '<p>Attested TLS reachability &amp; trust checks</p>'
        '<div class="health-bars" role="img" aria-label="Hourly monitoring history: '
        + html.escape(summary, quote=True) + '">' + ''.join(bars) + '</div>'
        '<div class="health-key">'
        + ''.join('<span><i class="' + state + '"></i>' + label + '</span>'
                  for state, label in (('up', 'Operational'), ('degraded', 'Degraded'),
                                       ('down', 'Down'), ('unknown', 'No data')) if counts[state])
        + '</div>'
        '<div class="evidence-footer"><span>Recorded ' + html.escape(date_label(status['checked_at']))
        + '</span><a href="https://trustedrouter.com/status">View current status <svg class="link-arrow" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 19 19 5M5 5h14v14"/></svg></a></div></figure>'
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
