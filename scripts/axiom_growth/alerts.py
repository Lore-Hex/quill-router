"""Install an edge-triggered Axiom freshness alert using an operator login."""
import json

from scripts.axiom_growth.admin_api import api

NAME = 'TrustedRouter growth analytics stale'
QUERY = """['trustedrouter-marketing']
| where event == 'growth.sync_completed'
| summarize observed=max(todatetime(observed_through))
| project lag_minutes=coalesce(datetime_diff('minute',now(),observed),long(999999))"""


def main():
    notifiers = api('GET', '/v2/notifiers')
    notifier = next((r for r in notifiers if r['name'] == NAME), None)
    if notifier is None:
        notifier = api('POST', '/v2/notifiers', {'name': NAME, 'properties': {
            'email': {'emails': ['joseph@jperla.com']}}})
    monitors = api('GET', '/v2/monitors')
    previous = next((r for r in monitors if r['name'] == NAME), None)
    body = {'name': NAME, 'description': 'No current growth checkpoint for 15 minutes. Check the isolated growth-sync job; missing data is not zero growth.',
            'type': 'Threshold', 'aplQuery': QUERY, 'columnName': 'lag_minutes',
            'operator': 'Above', 'threshold': 15, 'rangeMinutes': 30,
            'intervalMinutes': 5, 'alertOnNoData': True, 'notifyEveryRun': False,
            'notifyByGroup': False, 'triggerAfterNPositiveResults': 2, 'triggerFromNRuns': 2,
            'notifierIds': [notifier['id']], 'disabled': False}
    result = api('PUT', '/v2/monitors/'+previous['id'], body) if previous else api('POST', '/v2/monitors', body)
    print(json.dumps({'monitor': result['id'], 'name': result['name'], 'disabled': result.get('disabled')}))


if __name__ == '__main__':
    main()
