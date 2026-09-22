"""Aggregate-only marketing audit. Discover schema, deduplicate, then measure.

Run before and after a rollout with the same --end to compare the same cohorts.
This script prints no visitor identifiers and never accesses transactional storage.
"""
import argparse
import datetime as dt
import json

from scripts.axiom_growth import model as m
from scripts.axiom_growth.admin_api import api, query


def queries(fields):
    wanted = ['_time', 'event', 'record_type', 'account_fingerprint', 'anonymous_fingerprint',
              'first_source', 'first_referrer_domain', 'customer_domain_verified', 'first_purchase_at',
              'experiment_cell_id', 'first_landing_path', 'purchase_microdollars', 'identity_link_status',
              'input_tokens', 'output_tokens', 'http_status', 'finish_reason', 'observed_through',
              'last_observed_at', 'attempt_id', 'purchase_path']
    present = [f for f in wanted if f in fields]
    base = f"['{m.DATASET}'] | summarize arg_max(exported_at, " + ', '.join(present) + ') by event_id'
    journey = base + " | where record_type=='journey_snapshot' and isnotempty(account_fingerprint) "
    journey += '| summarize arg_max(exported_at, ' + ', '.join(f for f in present if f != 'account_fingerprint') + ') by account_fingerprint'
    return {
        'usage_7d': base + " | where event=='growth.daily_usage' and _time >= datetime({start7}) and _time < datetime({end}) | summarize rows=count(), account_linked=countif(isnotempty(account_fingerprint)), tokens=sum(input_tokens)+sum(output_tokens), account_linked_tokens=sumif(input_tokens+output_tokens,isnotempty(account_fingerprint))",
        'account_journeys_90d': journey + " | summarize accounts=count(), referrers=countif(isnotempty(first_referrer_domain)), verified_domains=countif(customer_domain_verified==true), buyers=countif(purchase_microdollars>0), purchase_dates=countif(isnotempty(first_purchase_at)), experiment_cells=countif(isnotempty(experiment_cell_id)), unclassified_paths=countif(first_landing_path in ('(other)','(unclassified)','(unknown)'))",
        'first_sources_90d': journey + ' | summarize accounts=count() by first_source | sort by accounts desc | take 50',
        'observed_events_90d': base + " | where record_type=='observed_event' | summarize events=count(), with_status=countif(http_status>=100), with_finish=countif(isnotempty(finish_reason)) by event",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end', default=dt.datetime.now(dt.UTC).isoformat())
    args = parser.parse_args()
    end = m.date(args.end)
    fields = api('GET', f'/v2/datasets/{m.DATASET}/fields')
    names = {f['name'] for f in fields}
    result = {'evaluated_at': dt.datetime.now(dt.UTC).isoformat(), 'cohort_end': end.isoformat(),
              'schema': fields, 'metrics': {}}
    for name, apl in queries(names).items():
        apl = apl.format(start7=(end-dt.timedelta(days=7)).isoformat(), end=end.isoformat())
        result['metrics'][name] = m.rows(query(apl, (end-dt.timedelta(days=90)).isoformat(), end.isoformat()))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
