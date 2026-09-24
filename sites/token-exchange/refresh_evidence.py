#!/usr/bin/env python3
"""Refresh the landing page's small, dated public-data snapshot; never runs in build."""
import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
ROUTES = [
    ('z-ai/glm-5.3-flash', 'GLM 5.3 Flash', 'tinfoil'),
    ('deepseek/deepseek-v4.1-flash', 'DeepSeek V4.1 Flash', 'tinfoil'),
    ('openai/gpt-oss-120b', 'GPT OSS 120B', 'tinfoil'),
]
PROFILES = {
    'new-york': dict(origin='https://trustedrouter.com', component='us_east4_regional_api',
                     release_url='https://trustedrouter.com/trust/gcp-release.json',
                     destination='evidence.json'),
    'dubai': dict(origin='https://azure.trustedrouter.com', component='uaenorth_gateway',
                  release_url='https://trust.trustedrouter.com/trust/azure-release.json',
                  destination='evidence-dubai.json'),
    # Shared GCP evidence only: no city-specific serving component is bound.
    'tokyo': dict(origin='https://trustedrouter.com', component=None,
                  release_url='https://trustedrouter.com/trust/gcp-release.json',
                  destination='evidence-tokyo.json'),
    'london': dict(origin='https://trustedrouter.com', component=None,
                   release_url='https://trustedrouter.com/trust/gcp-release.json',
                   destination='evidence-london.json'),
    # Reused by markets without an individually established serving binding.
    'shared-gcp': dict(origin='https://trustedrouter.com', component=None,
                      release_url='https://trustedrouter.com/trust/gcp-release.json',
                      destination='evidence-shared-gcp.json'),
    'europe': dict(origin='https://trustedrouter.com', component='eu_regional_api',
                   release_url='https://trustedrouter.com/trust/gcp-release.json',
                   destination='evidence-europe.json'),
}


def project(endpoints, status, captured_at, release=None, *, market='new-york'):
    profile = PROFILES[market]
    origin = profile['origin']
    routes = []
    for (model, label, provider), payload in zip(ROUTES, endpoints, strict=True):
        row = next(e for e in payload['data']
                   if e['provider'] == provider and e['usage_type'] == 'Credits')
        privacy = row['trustedrouter']
        if provider == 'tinfoil' and not (
                privacy['provider_e2ee'] and privacy['provider_confidential_compute']):
            raise ValueError('Selected E2EE route no longer meets the comparison requirement')
        if not privacy['provider_zero_data_retention']:
            raise ValueError('Selected route no longer declares zero data retention')
        routes.append(dict(model=model, label=label, provider=row['provider_name'],
                           pricing=row['pricing'], privacy=row['trustedrouter']['privacy_tier_label'],
                           source=f'{origin}/v1/models/{model}/endpoints'))
    result = dict(captured_at=captured_at, routes=routes)
    if profile['component']:
        component = next(c for c in status['data']['components']
                         if c['id'] == profile['component'])
        result['status'] = dict(
            name=component['name'], checked_at=component['last_checked_at'],
            uptime=component['uptime_24h_percent'], samples=component['sample_count_24h'],
            history=[{k: h[k] for k in ('bucket_start', 'status', 'sample_count', 'uptime_percent')}
                     for h in component['history'][-24:]],
            source=f'{origin}/status.json')
    result.update(catalog_url=f'{origin}/providers', status_url=f'{origin}/status')

    result['status_generated_at'] = status['data']['generated_at']
    result['service_history'] = [dict(
        name=c['name'], id=c['id'], description=c['description'],
        uptime=c['uptime_24h_percent'], checked_at=c['last_checked_at'],
        history=[{k: h[k] for k in ('bucket_start', 'status', 'sample_count', 'uptime_percent')}
                 for h in c['history'][-24:]])
        for c in status['data']['components'] if c['id'] in ('canonical_api', 'model_inference')]
    if market == 'dubai':
        result['uptime_label'] = 'Azure uptime'
        if not release or release.get('platform') != 'azure-confidential-containers-sev-snp':
            raise ValueError('Dubai requires an Azure release record')
        region = next(r for r in release['regions']
                      if r['attestation_url'] == 'https://api-azure.trustedrouter.com/attestation'
                      and r['origin_hostname'].endswith('.uaenorth.azurecontainer.io'))
        measurement = region['hostdata']
        if not re.fullmatch(r'[0-9a-f]{64}', measurement) or measurement not in release['accepted_hostdata']:
            raise ValueError('Dubai policy measurement is missing or not accepted')
        result['attestation'] = dict(
            platform=release['platform'], measurement=measurement,
            measurement_label='Published policy measurement',
            gateway='Azure Confidential Containers', verification='Microsoft Azure Attestation',
            source_commit=release['source_commit'], source_label='Published source',
            source_commit_provenance=release.get('source_commit_provenance'),
            source=profile['release_url'], review_url=profile['release_url'],
            review_label='Review Azure attestation',
        )
    elif release is not None:
        if ((profile['component'] is None or market == 'europe')
                and release.get('platform') != 'gcp-confidential-space'):
            raise ValueError('Shared GCP evidence requires a GCP release record')
        result['attestation'] = {key: release[key] for key in
            ('platform', 'source_commit', 'image_digest', 'attestation_issuer', 'attestation_audience')}
        result['attestation']['source'] = profile['release_url']
    if profile['component'] is None:
        if release is None:
            raise ValueError('Shared GCP evidence requires a GCP release record')
        if {c['id'] for c in result['service_history']} != {'canonical_api', 'model_inference'}:
            raise ValueError('Shared GCP evidence requires canonical API and inference history')
        result['uptime_label'] = 'Shared GCP uptime'
        result['status_source'] = f'{origin}/status.json'
        result['attestation'].update(review_url=profile['release_url'],
                                     review_label='Review GCP release')
    elif market == 'europe':
        if release is None:
            raise ValueError('Europe evidence requires a GCP release record')
        result['uptime_label'] = 'GCP uptime'
        result['attestation'].update(review_url=profile['release_url'],
                                     review_label='Review GCP release')
    return result


def get(url):
    if urlsplit(url).scheme != 'https':
        raise ValueError('Evidence sources must use HTTPS')
    with urlopen(url, timeout=20) as response:  # noqa: S310 — HTTPS scheme validated above.
        return json.load(response)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', choices=PROFILES, default='new-york')
    args = parser.parse_args()
    profile = PROFILES[args.market]
    origin = profile['origin']
    data = project([get(f'{origin}/v1/models/{m}/endpoints')
                    for m, _, _ in ROUTES], get(f'{origin}/status.json'),
                   datetime.now(UTC).isoformat(),
                   get(profile['release_url']), market=args.market)
    destination = HERE / profile['destination']
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(destination)
