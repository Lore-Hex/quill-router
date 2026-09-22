"""Pure, replay-safe growth projections shared by the scheduled exporter."""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from uuid import UUID


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
        "onboarding_call_started", "onboarding_call_succeeded", "onboarding_call_failed",
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
ATTEMPT_FIELDS = ("attempt_id", "flow", "http_status", "elapsed_ms", "failure_reason", "finish_reason")
IDENTITY_FIELDS = ("account_fingerprint", "workspace_fingerprint", "marketing_workspace_fingerprint")
FIRST_FIELDS = ("first_utm_source", "first_utm_medium", "first_utm_campaign", "first_creative_id",
                "first_experiment_id", "first_experiment_cell_id", "first_landing_path")
EVIDENCE_FIELDS = ("customer_domain", "customer_domain_verified", "customer_domain_basis",
                   "domain_observed_at", "first_referrer_domain", "first_purchase_at",
                   "identity_link_status", "payment_method")
FIELDS = ("event", "anonymous_fingerprint", "amount_microdollars", "referer_host",
          *DIMENSIONS, *ATTEMPT_FIELDS, *IDENTITY_FIELDS, *FIRST_FIELDS, *EVIDENCE_FIELDS)
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
        "schema_version": 5,
        "record_type": "observed_event",
        "referrer_domain": domain(row.get("referer_host")),
    }
    matched = {**row, **(enrichment or {}).get(fingerprint, {})}
    output["customer_domain"] = domain(matched.get("customer_domain"))
    output["first_referrer_domain"] = domain(matched.get("first_referrer_domain"))
    output["customer_domain_verified"] = matched.get("customer_domain_verified") is True
    basis = matched.get("customer_domain_basis")
    output["customer_domain_basis"] = basis if basis in {
        "current_workspace_owner_email", "current_account_email", "verified_oauth_email",
        "verified_account_email", "unverified_account_email", "ambiguous_visitor_multiple_owners",
    } else "not_linked"
    output["domain_observed_at"] = timestamp(matched["domain_observed_at"]) if matched.get("domain_observed_at") else ""
    for field in DIMENSIONS:
        value = row.get(field)
        if field == "landing_path":
            # Dynamic paths can contain account IDs, e-mails, or secrets.
            output[field] = safe_path(value)
        else:
            output[field] = safe_label(value)
    for field in IDENTITY_FIELDS:
        if HASH.fullmatch(str(row.get(field, ""))):
            output[field] = row[field]
    for field in FIRST_FIELDS:
        output[field] = safe_path(row.get(field)) if field.endswith("path") else safe_label(row.get(field))
    output['utm_source'] = canonical_source(output['utm_source'], output['referrer_domain'])
    if row.get('first_utm_source'):
        output['first_utm_source'] = canonical_source(row['first_utm_source'], output['first_referrer_domain'])
    output['identity_link_status'] = (
        row.get('identity_link_status') if row.get('identity_link_status') in {'linked', 'orphaned', 'ambiguous'}
        else ('linked' if output.get('account_fingerprint') else 'orphaned'))
    if row.get('first_purchase_at'):
        output['first_purchase_at'] = timestamp(row['first_purchase_at'])
    payment_method = row.get('payment_method')
    if payment_method in {'stripe', 'stripe_card', 'stripe_ach', 'paypal', 'usdc', 'stablecoin', 'adyen', 'stripe_auto_refill'}:
        output['payment_method'] = payment_method
        output['purchase_path'] = 'auto_refill' if payment_method == 'stripe_auto_refill' else (
            'stablecoin_api' if payment_method in {'usdc', 'stablecoin'} else 'checkout')
    if event == "acquisition.credit_purchase_completed":
        amount = row.get("amount_microdollars")
        if type(amount) is not int or not 0 < amount < 10**15:
            raise ValueError("Purchase amount must be positive integer microdollars")
        output["amount_microdollars"] = amount
    identity = [source, output["_time"], event, fingerprint, output.get("amount_microdollars")]
    if event.startswith("acquisition.onboarding_call_"):
        attempt = row.get("attempt_id")
        if not isinstance(attempt, str):
            raise ValueError("Missing onboarding attempt ID")
        parsed = UUID(attempt)
        if str(parsed) != attempt or parsed.version != 4 or row.get("flow") != "welcome_test":
            raise ValueError("Invalid onboarding attempt metadata")
        output.update(attempt_id=attempt, flow="welcome_test")
        if event != "acquisition.onboarding_call_started":
            for field, maximum in (("http_status", 599), ("elapsed_ms", 120_000)):
                value = row.get(field)
                if type(value) is not int or not 0 <= value <= maximum:
                    raise ValueError("Invalid onboarding outcome number")
                output[field] = value
            if 0 < output["http_status"] < 100:
                raise ValueError("Invalid onboarding HTTP status")
            if event == "acquisition.onboarding_call_succeeded":
                if not 200 <= output["http_status"] < 300 or row.get("failure_reason"):
                    raise ValueError("Invalid onboarding success")
            else:
                reason = row.get("failure_reason")
                if reason not in {
                    "http_error", "empty_output", "output_budget_exhausted", "invalid_response",
                    "network_error", "timeout", "client_error", "missing_key",
                }:
                    raise ValueError("Invalid onboarding failure reason")
                output["failure_reason"] = reason
            finish = row.get("finish_reason")
            if finish in {"stop", "length", "content_filter", "tool_calls", "unknown"}:
                output["finish_reason"] = finish
        # Replayed delivery of one attempt event must not inflate attempts.
        identity = [source, event, fingerprint, attempt]
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
    if not isinstance(value, str) or not value:
        return '(unknown)'
    if value in {'(unknown)', '(unclassified)', '(account flow)'}:
        return value
    # Keep a small vocabulary, never user-chosen slugs, IDs or query parameters.
    if not value.startswith('/') or value.startswith('//') or len(value) > 4096:
        return '(unclassified)'
    path = value.split('?', 1)[0].split('#', 1)[0].lower().rstrip('/') or '/'
    if path in PUBLIC_LANDINGS:
        return path
    for family in ('/openrouter-alternative/lp/', '/openrouter-alternative/test/'):
        if path.startswith(family):
            return family + '*'
    for family in ('models', 'providers', 'blog', 'docs', 'compare', 'benchmarks', 'rankings'):
        if path.startswith('/' + family + '/'):
            return '/' + family + '/*'
    if path.startswith(('/auth/', '/console/')) or path.endswith('_oauth_callback'):
        return '(account flow)'
    return '(unclassified)'


PUBLIC_LANDINGS = frozenset(('/' + p) for p in (
    '', 'pricing', 'models', 'providers', 'leaderboard', 'blog', 'docs', 'trust', 'status',
    'choose', 'marketplace', 'support', 'vibe-coders', 'agents', 'quickstart',
    'openrouter-alternative', 'compare', 'eu', 'token-exchange', 'green-tokens',
    'private-llm-api', 'provider-failover', 'openai-compatible-api', 'migrate', 'chat',
    'openrouter-alternative/experiment', 'openrouter-alternative/quickstart',
    'private-llm-api/quickstart', 'kimi-k3-api', 'glm-5-2-api', 'deepseek-v4-api',
    'latest-model-apis', 'hipaa-llm-api', 'llm-zero-data-retention', 'claude-api-privacy',
))


def canonical_source(value, referrer=''):
    label = safe_label(value).strip().lower()
    host = domain(label) or domain(referrer)
    aliases = {
        'google': ('google.com', 'google.co.uk', 'google.com.hk', 'google.de', 'google.fr', 'google.ca', 'google.co.in', 'google.com.au'),
        'bing': ('bing.com',), 'duckduckgo': ('duckduckgo.com',), 'brave': ('search.brave.com',),
        'yandex': ('yandex.ru', 'yandex.com'), 'kagi': ('kagi.com',),
        'chatgpt': ('chatgpt.com', 'chat.openai.com'), 'perplexity': ('perplexity.ai',),
        'claude': ('claude.ai',), 'gemini': ('gemini.google.com',), 'github': ('github.com',),
        'reddit': ('reddit.com',), 'hackernews': ('news.ycombinator.com',),
        'youtube': ('youtube.com', 'youtu.be'), 'linkedin': ('linkedin.com',),
        'x': ('x.com', 'twitter.com', 't.co'), 'producthunt': ('producthunt.com',),
    }
    # Explicit campaign sources outrank referrer; only canonicalize host-shaped sources.
    if label and label not in {'direct', '(unknown)', '(direct)'} and not domain(label):
        return {'twitter': 'x', 'hn': 'hackernews'}.get(label, label)
    for source, hosts in reversed(list(aliases.items())):
        if any(host == h or host.endswith('.' + h) for h in hosts):
            return source
    return domain(label) or ('direct' if label in {'direct', '(direct)'} else label or host or '(unknown)')


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
    accounts_by_anon = defaultdict(set)
    for anon, group in visitors.items():
        for row in group:
            account = row.get('account_fingerprint')
            if isinstance(account, str) and HASH.fullmatch(account):
                accounts_by_anon[anon].add(account)
            for field in ('workspace_fingerprint', 'marketing_workspace_fingerprint'):
                if row.get(field):
                    links[row[field]].add(anon)
    usage_by_anon = defaultdict(list)
    for item in daily:
        candidates = set()
        for key in ('workspace_fingerprint', 'marketing_workspace_fingerprint'):
            candidates |= links.get(item.get(key), set())
        accounts = set().union(*(accounts_by_anon[a] for a in candidates))
        # A workspace may have multiple browser visitors. Account evidence must
        # agree for all of them before assigning that workspace's usage.
        account = next(iter(accounts)) if len(accounts) == 1 and all(accounts_by_anon[a] for a in candidates) else ''
        item['account_fingerprint'] = account
        item['anonymous_fingerprint'] = next(iter(candidates)) if len(candidates) == 1 else ''
        item['identity_link_status'] = 'linked' if account else ('ambiguous' if len(candidates) > 1 or len(accounts) > 1 else 'orphaned')
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
        # First-touch values on signup were frozen before OAuth. Historical
        # enrichment is evidence, not a second observed conversion.
        referrer = domain(acquisition.get('first_referrer_domain')) or next(
            (domain(r.get('first_referrer_domain')) for r in group if domain(r.get('first_referrer_domain'))),
            domain(first.get('referrer_domain')))
        evidence = next((r for r in reversed(group) if domain(r.get('customer_domain'))), {})
        purchase_times = [stamp(r['first_purchase_at']) for r in group if r.get('first_purchase_at')]
        purchase_times.extend(stamp(r['_time']) for r in spend)
        first_source = canonical_source(acquisition.get('first_utm_source') or first.get('utm_source'), referrer)
        first_path = acquisition.get('first_landing_path')
        if first_path in (None, '', '(other)', '(unknown)', '(unclassified)', '(account flow)'):
            first_path = first.get('landing_path')
        item = {
            '_time':stamp(first['_time']), 'event':'growth.journey', 'event_id':digest('journey:'+anon),
            'record_type':'journey_snapshot', 'schema_version':5, 'anonymous_fingerprint':anon,
            'first_observed_at':stamp(first['_time']), 'last_observed_at':stamp(observed[-1]['_time']),
            'utm_source':canonical_source(acquisition.get('utm_source'), acquisition.get('referrer_domain')),
            'utm_medium':acquisition.get('utm_medium') or '(unknown)',
            'utm_campaign':acquisition.get('utm_campaign') or '',
            'creative_id':acquisition.get('creative_id') or '',
            'landing_path':safe_path(acquisition.get('landing_path')),
            'first_source':first_source,
            'first_utm_source':first_source,
            'first_utm_medium':acquisition.get('first_utm_medium') or first.get('utm_medium') or '',
            'first_utm_campaign':acquisition.get('first_utm_campaign') or first.get('utm_campaign') or '',
            'first_creative_id':acquisition.get('first_creative_id') or first.get('creative_id') or '',
            'experiment_id':acquisition.get('first_experiment_id') or next((r.get('experiment_id') for r in observed if r.get('experiment_id')), ''),
            'experiment_cell_id':acquisition.get('first_experiment_cell_id') or next((r.get('experiment_cell_id') for r in observed if r.get('experiment_cell_id')), ''),
            'first_medium':acquisition.get('first_utm_medium') or first.get('utm_medium') or '(unknown)',
            'first_landing_path':safe_path(first_path),
            'first_touch_basis':'stored_cookie' if acquisition.get('first_utm_source') else 'first_retained_event',
            'last_source':canonical_source(observed[-1].get('utm_source'), observed[-1].get('referrer_domain')),
            'last_tagged_landing':safe_path(observed[-1].get('landing_path')),
            'referrer_domain':domain(acquisition.get('referrer_domain')),
            'first_referrer_domain':referrer, 'first_referrer_host':referrer,
            'customer_domain':domain(evidence.get('customer_domain')),
            'customer_domain_verified':evidence.get('customer_domain_verified') is True,
            'customer_domain_basis':evidence.get('customer_domain_basis') or 'not_linked',
            'first_purchase_at':min(purchase_times, key=date) if purchase_times else None,
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
        item['identity_link_status'] = 'linked' if item['account_fingerprint'] else ('ambiguous' if len(accounts_by_anon[anon])>1 else 'orphaned')
        result.append(item)
    by_anon = {r['anonymous_fingerprint']:r for r in result}
    for item in daily:
        linked = by_anon.get(item['anonymous_fingerprint'], {})
        for field in ('utm_source','utm_medium','utm_campaign','landing_path','creative_id','experiment_id','experiment_cell_id'):
            item[field] = linked.get(field) or '(unattributed)'
    return result
