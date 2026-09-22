"""Register new field types without inventing customer or conversion events."""
import argparse
import datetime as dt
import os

import httpx

from scripts.axiom_growth import model as m


def registration(now):
    strings = ('billing_account_fingerprint', 'billing_identity_basis', 'billing_source',
               'billing_owner_observed_at', 'experiment_exposed_at', 'first_gateway_attempt_at',
               'first_gateway_http_error_at', 'gateway_outcome', 'route', 'started_at',
               'finished_at', 'gateway_status', 'gateway_observed_through',
               'billing_owners_observed_through', 'billing_owner_status')
    return {'_time': now.isoformat(), 'exported_at': now.isoformat(),
            'event': 'growth.schema_registered', 'record_type': 'schema_registration',
            'event_id': m.digest('growth.schema_registered:6'), 'schema_version': 6,
            'paired': False, 'first_gateway_http_status': 0,
            **dict.fromkeys(strings, '')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    row = registration(dt.datetime.now(dt.UTC))
    if not args.apply:
        print('Dry run: register schema metadata only; no customer activity.')
        return
    token = os.environ['GROWTH_AXIOM_TOKEN']
    response = httpx.post('https://' + m.EDGE_REGION + '/v1/ingest/' + m.DATASET,
                          headers={'Authorization': 'Bearer ' + token}, json=[row], timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'Schema registration HTTP {response.status_code}')
    result = response.json()
    if result.get('failed') != 0 or result.get('ingested') != 1:
        raise RuntimeError('Schema registration not accepted')
    print('Registered growth schema 6; no customer activity created.')


if __name__ == '__main__':
    main()
