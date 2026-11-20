"""Exercise actual APL funnel arithmetic with ephemeral rows, never ingested."""
import json

from scripts.axiom_growth.admin_api import query
from scripts.axiom_growth.dashboards import DECL, REWARD_STAGES, REWARDS_QUERY, onboarding_query
from scripts.axiom_growth.model import rows


def validate():
    cases = {
        'success': ['started', 'succeeded', 'succeeded'],
        'failure': ['started', 'failed'],
        'missing': ['started'],
        'orphan': ['succeeded'],
        'conflict': ['started', 'succeeded', 'failed'],
    }
    values = []
    for attempt, events in cases.items():
        for event in events:
            values.append(','.join([
                "datetime(2026-09-16T00:00:00Z)",
                "datetime(2026-09-16T00:01:00Z)",
                repr('acquisition.onboarding_call_' + event),
                repr(attempt + event), "'visitor'", repr(attempt),
                "'empty_output'" if event == 'failed' else "''", '200', '1000',
                "'test'", "'internal'", "'regression'", "'/'",
            ]))
    fixture = (
        'datatable(_time:datetime, exported_at:datetime, event:string, event_id:string, '
        'anonymous_fingerprint:string, attempt_id:string, failure_reason:string, '
        'http_status:long, elapsed_ms:long, utm_source:string, utm_medium:string, '
        'utm_campaign:string, landing_path:string)[' + ','.join(values) + ']'
    )
    apl = DECL + onboarding_query().replace("['trustedrouter-marketing']", fixture, 1)
    actual = rows(query(apl))[0]
    expected = {
        'Started_attempts': 4, 'Matched_completed': 2,
        'Visible_answer_successes': 1, 'Failures': 1, 'Pending': 0,
        'Missing_outcome_5m': 1, 'Orphan_outcomes': 1, 'Conflicting_outcomes': 1,
        'Success_pct': 50, 'Failure_pct': 50,
    }
    if actual != expected:
        raise ValueError('Matched-attempt APL regression: ' + json.dumps(actual))
    return actual


CONSOLE_COLUMNS = {
    '_time': 'datetime', 'exported_at': 'datetime', 'event': 'string', 'event_id': 'string',
    'anonymous_fingerprint': 'string', 'account_fingerprint': 'string',
    'workspace_fingerprint': 'string', 'utm_source': 'string', 'utm_medium': 'string',
    'utm_campaign': 'string', 'creative_id': 'string', 'landing_path': 'string',
    'referrer_domain': 'string', 'customer_domain': 'string', 'amount_microdollars': 'long',
    'device_category': 'string', 'reward_action': 'string',
}


def console_fixture():
    """Ephemeral console rows: one replayed page view, a rewards funnel, one grant, one QA row."""
    def row(event, event_id, **overrides):
        values = {
            '_time': 'datetime(2026-10-17T00:00:00Z)', 'exported_at': 'datetime(2026-10-17T00:01:00Z)',
            'event': repr('acquisition.' + event), 'event_id': repr(event_id),
            'anonymous_fingerprint': "'visitor'", 'account_fingerprint': "'acct'",
            'utm_source': "'google'", 'utm_medium': "'cpc'", 'utm_campaign': "'launch'",
            'landing_path': "'/'", 'amount_microdollars': '0', 'device_category': "'desktop'",
            **overrides,
        }
        return ','.join(values.get(column, "''") for column in CONSOLE_COLUMNS)
    values = [
        row('console_api_keys_viewed', 'view'),
        row('console_api_keys_viewed', 'view', exported_at='datetime(2026-10-17T00:02:00Z)'),  # replay
        row('social_rewards_invitation_viewed', 'impression'),
        row('social_rewards_opened', 'open'),
        row('social_reward_clicked', 'click1', reward_action="'github'"),
        row('social_reward_clicked', 'click2', reward_action="'github'"),
        # Grants are server-side conversion rows: workspace identity, no account or device.
        row('social_reward_claimed', 'grant', account_fingerprint="''", workspace_fingerprint="'ws'",
            device_category="''", reward_action="'github'"),
        # Diagnostic traffic tagged as internal testing must leave customer panels.
        row('console_api_keys_viewed', 'qa', account_fingerprint="'qa'", utm_medium="'internal'"),
    ]
    columns = ', '.join(f'{name}:{kind}' for name, kind in CONSOLE_COLUMNS.items())
    return 'datatable(' + columns + ')[' + ','.join(values) + ']'


def validate_console():
    fixture = console_fixture()
    apl = DECL + REWARDS_QUERY.replace("['trustedrouter-marketing']", fixture, 1)
    stages = {
        r['Stage']: (r['Records'], r['Accounts'], r['Workspaces'], r['Visitors'])
        for r in rows(query(apl))
    }
    # (Records, Accounts, Workspaces, Visitors) per stage, in REWARD_STAGES order.
    counts = [(1, 1, 0, 1), (1, 1, 0, 1), (1, 1, 0, 1), (2, 1, 0, 1), (1, 0, 1, 1)]
    expected = {label: count for (_, label), count in zip(REWARD_STAGES, counts, strict=True)}
    if stages != expected:
        raise ValueError('Console stage APL regression: ' + json.dumps(stages))
    filtered = DECL.replace("source_filter:string = ''", "source_filter:string = 'bing'", 1)
    if rows(query(filtered + REWARDS_QUERY.replace("['trustedrouter-marketing']", fixture, 1))):
        raise ValueError('Console panels ignore the source filter')
    return stages


if __name__ == '__main__':
    print(json.dumps({'onboarding': validate(), 'console': validate_console()}))
