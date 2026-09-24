#!/usr/bin/env python3
"""Refresh the landing page's small, dated public-data snapshot; never runs in build."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
ROUTES = [
    ('z-ai/glm-5.3-flash', 'GLM 5.3 Flash', 'tinfoil'),
    ('deepseek/deepseek-v4.1-flash', 'DeepSeek V4.1 Flash', 'tinfoil'),
    ('openai/gpt-oss-120b', 'GPT OSS 120B', 'tinfoil'),
]


def project(endpoints, status, captured_at, release=None):
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
                           source=f'https://trustedrouter.com/v1/models/{model}/endpoints'))
    component = next(c for c in status['data']['components']
                     if c['id'] == 'us_east4_regional_api')
    result = dict(captured_at=captured_at, routes=routes, status=dict(
        name=component['name'], checked_at=component['last_checked_at'],
        uptime=component['uptime_24h_percent'], samples=component['sample_count_24h'],
        history=[{k: h[k] for k in ('bucket_start', 'status', 'sample_count', 'uptime_percent')}
                 for h in component['history'][-24:]],
        source='https://trustedrouter.com/status.json'))

    result['status_generated_at'] = status['data']['generated_at']
    result['service_history'] = [dict(
        name=c['name'], id=c['id'], description=c['description'],
        uptime=c['uptime_24h_percent'], checked_at=c['last_checked_at'],
        history=[{k: h[k] for k in ('bucket_start', 'status', 'sample_count', 'uptime_percent')}
                 for h in c['history'][-24:]])
        for c in status['data']['components'] if c['id'] in ('canonical_api', 'model_inference')]
    if release is not None:
        result['attestation'] = {key: release[key] for key in
            ('platform', 'source_commit', 'image_digest', 'attestation_issuer', 'attestation_audience')}
        result['attestation']['source'] = 'https://trustedrouter.com/trust/gcp-release.json'
    return result


def get(url):
    with urlopen(url, timeout=20) as response:
        return json.load(response)


if __name__ == '__main__':
    data = project([get(f'https://trustedrouter.com/v1/models/{m}/endpoints')
                    for m, _, _ in ROUTES], get('https://trustedrouter.com/status.json'),
                   datetime.now(timezone.utc).isoformat(),
                   get('https://trustedrouter.com/trust/gcp-release.json'))
    destination = HERE / 'evidence.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(destination)
