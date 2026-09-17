"""Exercise actual APL funnel arithmetic with ephemeral rows, never ingested."""
import json

from scripts.axiom_growth.admin_api import query
from scripts.axiom_growth.dashboards import DECL, onboarding_query
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


if __name__ == '__main__':
    print(json.dumps(validate()))
