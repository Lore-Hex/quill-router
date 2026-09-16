"""Pure, replay-safe growth projections shared by the scheduled exporter."""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from collections.abc import Mapping


def domain(value: object) -> str:
    """Accept a DNS hostname only, never a URL, email, port, or IP address."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return ""
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
           for part in host.split(".")):
        return ""
    if "." not in host or host.endswith((".local", ".internal", ".localhost", ".invalid", ".test")):
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        return host


DATASET = "trustedrouter-marketing"
SOURCE_DATASET = "trusted-router-logs"
EDGE_REGION = "eu-central-1.aws.edge.axiom.co"
BROWSER_EVENTS = frozenset(
    f"acquisition.{name}" for name in (
        "landing_engaged", "sign_in_opened", "first_call_started", "first_call_failed",
    )
)
CONVERSION_EVENTS = frozenset(
    f"acquisition.{name}" for name in (
        "signup_completed", "api_key_created", "first_successful_api_call",
        "free_credit_exhausted", "checkout_started", "payment_method_saved",
        "credit_purchase_completed", "retained_api_usage_7d",
    )
)
DIMENSIONS = (
    "utm_source", "utm_medium", "utm_campaign", "creative_id",
    "experiment_id", "experiment_cell_id", "landing_path",
)
FIELDS = ("event", "anonymous_fingerprint", "amount_microdollars", "referer_host", *DIMENSIONS)
MAX_ROWS = 20_000
PAGE_SIZE = 1000
LABEL_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_. /:-]{0,119}\Z")
SECRET_RE = re.compile(
    r"(?i)(?:sk[-_]|xaat-|xapt-|bearer|password|secret|token|authorization|"
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|https?://)"
)
PUBLIC_PATH_RE = re.compile(
    r"/(?:|pricing|models|providers|leaderboard|blog|docs|trust|status|"
    r"openrouter-alternative|compare|eu|agents|quickstart)\Z"
)


def timestamp(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        # Axiom CLI emits integer nanoseconds; never use float for these IDs.
        seconds, nanos = divmod(value, 1_000_000_000)
        if not 946684800 <= seconds <= 4102444800:
            raise ValueError("Event timestamp outside supported range")
        date = dt.datetime.fromtimestamp(seconds, dt.UTC)
        return date.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Missing or invalid event timestamp")
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid event timestamp") from None
    if date.tzinfo is None:
        raise ValueError("Event timestamp requires a timezone")
    return date.astimezone(dt.UTC).isoformat()


def safe_label(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if not LABEL_RE.fullmatch(value) or SECRET_RE.search(value):
        return "(redacted)"
    return value


def project_event(
    row: Mapping[str, object], *, source: str,
    enrichment: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Create a fresh allowlisted object; no raw log/body fields cross the boundary."""
    allowed = BROWSER_EVENTS if source in {"cloud_logging", "axiom_browser_archive"} else CONVERSION_EVENTS
    if source not in {"cloud_logging", "axiom", "axiom_browser_archive"} or row.get("event") not in allowed:
        raise ValueError("Unexpected marketing event/source")
    fingerprint = row.get("anonymous_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("Missing or invalid pseudonymous visitor fingerprint")
    event = str(row["event"])
    output: dict[str, object] = {
        "_time": timestamp(row.get("_time")),
        "event": event,
        "anonymous_fingerprint": fingerprint,
        "source": source,
        "schema_version": 2,
        "record_type": "observed_event",
        "referrer_domain": domain(row.get("referer_host")),
    }
    matched = (enrichment or {}).get(fingerprint, {})
    output["customer_domain"] = domain(matched.get("customer_domain"))
    output["first_referrer_domain"] = domain(matched.get("first_referrer_domain"))
    output["customer_domain_verified"] = matched.get("customer_domain_verified") is True
    basis = matched.get("customer_domain_basis")
    output["customer_domain_basis"] = basis if basis in {
        "current_workspace_owner_email", "ambiguous_visitor_multiple_owners",
    } else "not_linked"
    output["domain_observed_at"] = timestamp(matched["domain_observed_at"]) if matched.get("domain_observed_at") else ""
    for field in DIMENSIONS:
        value = row.get(field)
        if field == "landing_path":
            # Dynamic paths can contain account IDs, e-mails, or secrets.
            output[field] = (
                value if isinstance(value, str) and PUBLIC_PATH_RE.fullmatch(value) else "(other)"
            )
        else:
            output[field] = safe_label(value)
    if event == "acquisition.credit_purchase_completed":
        amount = row.get("amount_microdollars")
        if type(amount) is not int or not 0 < amount < 10**15:
            raise ValueError("Purchase amount must be positive integer microdollars")
        output["amount_microdollars"] = amount
    identity = [source, output["_time"], event, fingerprint, output.get("amount_microdollars")]
    output["event_id"] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return output


HASH = re.compile(r'[a-f0-9]{64}\Z')
PUBLIC_PATH = re.compile(r'/(?:|pricing|models|providers|leaderboard|blog|docs|trust|status|choose|marketplace|support|vibe-coders|agents|quickstart|openrouter-alternative|compare|eu|(?:blog|docs|compare)/[a-z0-9-]+)\Z')


def rows(result):
    output = []
    for table in result.get('tables', []):
        names = [x['name'] for x in table['fields']]
        output.extend(dict(zip(names, values, strict=True)) for values in zip(*table['columns'], strict=True))
    return output


def stamp(value):
    return timestamp(value)


def date(value):
    return dt.datetime.fromisoformat(stamp(value).replace('Z', '+00:00'))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_path(value):
    return value if isinstance(value, str) and PUBLIC_PATH.fullmatch(value) and not SECRET_RE.search(value) else '(other)'


def build_journeys(events, daily, end):
    unique = {r['event_id']: r for r in sorted(events, key=lambda r:str(r.get('exported_at') or ''))}
    visitors = defaultdict(list)
    for row in unique.values():
        anon = row.get('anonymous_fingerprint')
        if isinstance(anon, str) and HASH.fullmatch(anon):
            visitors[anon].append(row)
    # Map a workspace only where evidence gives exactly one visitor. This
    # avoids assigning a team's entire token usage to an arbitrary person.
    links = defaultdict(set)
    for anon, group in visitors.items():
        for row in group:
            for field in ('workspace_fingerprint', 'marketing_workspace_fingerprint'):
                if row.get(field):
                    links[row[field]].add(anon)
    usage_by_anon = defaultdict(list)
    for item in daily:
        candidates = set()
        for key in ('workspace_fingerprint', 'marketing_workspace_fingerprint'):
            candidates |= links.get(item[key], set())
        item['anonymous_fingerprint'] = next(iter(candidates)) if len(candidates) == 1 else ''
        item['identity_link_status'] = 'linked' if len(candidates) == 1 else ('ambiguous' if candidates else 'unlinked')
        if len(candidates) == 1:
            usage_by_anon[item['anonymous_fingerprint']].append(item)
    result = []
    for anon, group in visitors.items():
        group.sort(key=lambda r:date(r['_time']))
        observed = [r for r in group if r['event'].startswith('acquisition.')]
        if not observed:
            continue
        first = observed[0]
        stages = {}
        for short in ('landing_engaged','sign_in_opened','signup_completed','api_key_created','first_call_started','first_call_failed','first_successful_api_call','checkout_started','payment_method_saved','credit_purchase_completed','retained_api_usage_7d'):
            matches = [r for r in observed if r['event'] == 'acquisition.'+short]
            stages[short+'_at'] = stamp(matches[0]['_time']) if matches else None
        signup = next((r for r in observed if r['event']=='acquisition.signup_completed'), None)
        acquisition = signup or first
        spend = [r for r in observed if r['event']=='acquisition.credit_purchase_completed']
        item = {
            '_time':stamp(first['_time']), 'event':'growth.journey', 'event_id':digest('journey:'+anon),
            'record_type':'journey_snapshot', 'schema_version':3, 'anonymous_fingerprint':anon,
            'first_observed_at':stamp(first['_time']), 'last_observed_at':stamp(observed[-1]['_time']),
            'utm_source':acquisition.get('utm_source') or '(unknown)',
            'utm_medium':acquisition.get('utm_medium') or '(unknown)',
            'utm_campaign':acquisition.get('utm_campaign') or '',
            'creative_id':acquisition.get('creative_id') or '',
            'landing_path':acquisition.get('landing_path') or '(unknown)',
            'first_source':acquisition.get('first_utm_source') or first.get('utm_source') or '(unknown)',
            'first_medium':acquisition.get('first_utm_medium') or first.get('utm_medium') or '(unknown)',
            'first_landing_path':acquisition.get('first_landing_path') if acquisition.get('first_landing_path') not in (None,'','(other)') else first.get('landing_path') or '(unknown)',
            'first_touch_basis':'stored_cookie' if acquisition.get('first_utm_source') else 'first_retained_event',
            'last_source':observed[-1].get('utm_source') or '(unknown)',
            'last_tagged_landing':observed[-1].get('landing_path') or '(unknown)',
            'referrer_domain':acquisition.get('referrer_domain') or '',
            'customer_domain':next((r.get('customer_domain') for r in group if r.get('customer_domain')), ''),
            'purchase_count':len(spend),
            'purchase_microdollars':sum(int(r.get('amount_microdollars') or 0) for r in spend),
            'linked_usage_days':len({r['_time'] for r in usage_by_anon[anon]}),
            'linked_usage_calls':sum(r['successful_calls'] for r in usage_by_anon[anon]),
            'linked_input_tokens':sum(r['input_tokens'] for r in usage_by_anon[anon]),
            'linked_output_tokens':sum(r['output_tokens'] for r in usage_by_anon[anon]),
            'observed_through':end.isoformat(), **stages,
        }
        for field in ('workspace_fingerprint','marketing_workspace_fingerprint','account_fingerprint'):
            values = {r[field] for r in group if r.get(field)}
            item[field] = next(iter(values)) if len(values)==1 else ''
        result.append(item)
    by_anon = {r['anonymous_fingerprint']:r for r in result}
    for item in daily:
        linked = by_anon.get(item['anonymous_fingerprint'], {})
        for field in ('utm_source','utm_medium','utm_campaign','landing_path','creative_id'):
            item[field] = linked.get(field) or '(unattributed)'
    return result
