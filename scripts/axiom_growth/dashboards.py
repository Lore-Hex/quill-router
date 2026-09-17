"""Versioned, organization-shared Axiom growth dashboards and query QA."""
import argparse
import datetime as dt
import json
import urllib.error
import uuid
from pathlib import Path

from scripts.axiom_growth.admin_api import api, query

DATASET='trustedrouter-marketing'
INPUT_LIMIT=1000000
DECL="""declare query_parameters (source_filter:string = '', medium_filter:string = '', campaign_filter:string = '', landing_filter:string = '', visitor_filter:string = '', age_days:long = 0);
"""
FILTER="""
| where isempty(source_filter) or utm_source == source_filter
| where isempty(medium_filter) or utm_medium == medium_filter
| where isempty(campaign_filter) or utm_campaign == campaign_filter
| where isempty(landing_filter) or landing_path == landing_filter
| where isempty(visitor_filter) or anonymous_fingerprint == visitor_filter
"""
SNAPSHOT_FIELDS={
 'growth.journey': ['_time','anonymous_fingerprint','account_fingerprint','workspace_fingerprint','marketing_workspace_fingerprint','utm_source','utm_medium','utm_campaign','creative_id','landing_path','first_source','first_medium','first_landing_path','first_touch_basis','last_source','last_tagged_landing','referrer_domain','customer_domain','purchase_count','purchase_microdollars','linked_usage_days','linked_usage_calls','linked_input_tokens','linked_output_tokens','observed_through','first_observed_at','last_observed_at']+[s+'_at' for s in ['landing_engaged','sign_in_opened','signup_completed','api_key_created','first_call_started','first_call_failed','first_successful_api_call','checkout_started','payment_method_saved','credit_purchase_completed','retained_api_usage_7d']],
 'growth.daily_usage':['_time','anonymous_fingerprint','workspace_fingerprint','marketing_workspace_fingerprint','utm_source','utm_medium','utm_campaign','creative_id','landing_path','model','provider','successful_calls','input_tokens','output_tokens','usage_microdollars','first_call_at','last_call_at','identity_link_status'],
}


def snapshot(event):
    fields=', '.join(SNAPSHOT_FIELDS[event])
    return f"['{DATASET}'] | where event == '{event}' | take {INPUT_LIMIT} | summarize arg_max(exported_at, {fields}) by event_id"


JOURNEY=snapshot('growth.journey')+FILTER+"""
| where datetime_diff('day', now(), _time) >= age_days
| extend engaged=todatetime(landing_engaged_at), signup=todatetime(signup_completed_at), activated=todatetime(first_successful_api_call_at), paid=todatetime(credit_purchase_completed_at), returned=todatetime(retained_api_usage_7d_at)
| extend has_engaged=isnotnull(engaged), has_signup=isnotnull(signup), signup_after_engaged=isnotnull(signup) and isnotnull(engaged) and signup>=engaged, active_after_signup=isnotnull(activated) and isnotnull(signup) and activated>=signup, paid_after_signup=isnotnull(paid) and isnotnull(signup) and paid>=signup
"""
USAGE=snapshot('growth.daily_usage')+FILTER
EVENTS=f"""['{DATASET}'] | where event startswith 'acquisition.'
| take {INPUT_LIMIT}
| summarize arg_max(exported_at, _time, event, anonymous_fingerprint, utm_source, utm_medium, utm_campaign, creative_id, landing_path, referrer_domain, customer_domain, amount_microdollars) by event_id
"""+FILTER
FRESH=f"['{DATASET}'] | where event == 'growth.sync_completed' | summarize arg_max(_time, observed_through) | project Latest_export=todatetime(_time), Observed_through=todatetime(observed_through) | extend Minutes_behind=datetime_diff('minute',now(),Observed_through) | extend Status=iff(isnull(Observed_through) or Minutes_behind>15,'STALE: refresh delayed','Automatic refresh every 5 minutes')"
ONBOARDING=f"""['{DATASET}'] | where event in ('acquisition.onboarding_call_started','acquisition.onboarding_call_succeeded','acquisition.onboarding_call_failed')
| extend attempt_id=tostring(column_ifexists('attempt_id','')), failure_reason=tostring(column_ifexists('failure_reason','')), http_status=tolong(column_ifexists('http_status',0)), elapsed_ms=tolong(column_ifexists('elapsed_ms',0))
| where isnotempty(attempt_id)
| take {INPUT_LIMIT}
| summarize _time=min(_time) by event_id, event, anonymous_fingerprint, attempt_id, failure_reason, http_status, elapsed_ms, utm_source, utm_medium, utm_campaign, landing_path
| extend _time=todatetime(_time)
"""+FILTER
ATTEMPTS=ONBOARDING+"""
| summarize Starts=countif(event=='acquisition.onboarding_call_started'), Successes=countif(event=='acquisition.onboarding_call_succeeded'), Failures=countif(event=='acquisition.onboarding_call_failed'), Started_at=minif(_time,event=='acquisition.onboarding_call_started') by anonymous_fingerprint, attempt_id
| extend Matched_success=Starts>0 and Successes>0 and Failures==0, Matched_failure=Starts>0 and Failures>0 and Successes==0
| extend Complete=Matched_success or Matched_failure, Missing_outcome=Starts>0 and Successes==0 and Failures==0
"""


def onboarding_query():
    return ATTEMPTS+"""
| summarize Started_attempts=countif(Starts>0), Matched_completed=countif(Complete), Visible_answer_successes=countif(Matched_success), Failures=countif(Matched_failure), Pending=countif(Missing_outcome and todatetime(Started_at)>ago(5m)), Missing_outcome_5m=countif(Missing_outcome and todatetime(Started_at)<=ago(5m)), Orphan_outcomes=countif(Starts==0), Conflicting_outcomes=countif(Successes>0 and Failures>0)
| extend Success_pct=iff(Matched_completed>0,round(100.0*Visible_answer_successes/Matched_completed,2),real(null)), Failure_pct=iff(Matched_completed>0,round(100.0*Failures/Matched_completed,2),real(null))
"""


def funnel_query():
    return JOURNEY+"""
| summarize engaged_n=countif(has_engaged), signup_n=countif(signup_after_engaged), activated_n=countif(signup_after_engaged and active_after_signup), all_signups=countif(has_signup), all_activated=countif(active_after_signup), buyers=countif(paid_after_signup), eligible_return=countif(active_after_signup and datetime_diff('day',now(),signup)>=7), returning=countif(active_after_signup and isnotnull(returned) and returned>=activated and returned>=signup+7d)
| extend steps=pack_array(
bag_pack('Step','01 Tagged engagement','Reached',engaged_n,'Base',engaged_n,'From','Observed engaged visitors'),
bag_pack('Step','02 Signup','Reached',signup_n,'Base',engaged_n,'From','Tagged engagement'),
bag_pack('Step','03 First successful API call','Reached',activated_n,'Base',signup_n,'From','Engaged then signup'),
bag_pack('Step','04 Purchase branch','Reached',buyers,'Base',all_signups,'From','All observed signups'),
bag_pack('Step','05 Return-use branch','Reached',returning,'Base',eligible_return,'From','Activated signup cohorts aged 7+ days'))
| mv-expand steps
| project Step=tostring(steps.Step), Reached=tolong(steps.Reached), Eligible=tolong(steps.Base), From=tostring(steps.From)
| extend Conversion_percent=iff(Eligible>0,round(100.0*Reached/Eligible,2),real(null)), Dropoff=Eligible-Reached
| sort by Step asc
"""


def by_dimension(dim):
    return JOURNEY+f"""
| summarize Engaged=countif(has_engaged), Signups=countif(has_signup), Activated=countif(active_after_signup), Purchasers=countif(paid_after_signup), Purchased_USD=sum(purchase_microdollars)/1000000.0 by {dim}
| extend Signup_to_activation_pct=iff(Signups>0,round(100.0*Activated/Signups,2),real(null)), Signup_to_purchase_pct=iff(Signups>0,round(100.0*Purchasers/Signups,2),real(null))
| sort by Activated desc, Signups desc | take 40
"""


def filter_chart():
    filters=[]
    for key,label in [('source_filter','Source'),('medium_filter','Channel / medium'),('campaign_filter','Campaign'),('landing_filter','Landing page'),('visitor_filter','Visitor fingerprint')]:
        filters.append({'active':True,'id':key,'name':label,'type':'search','selectType':'list','options':[{'default':True,'key':'All','value':''}],'apl':{'apl':'','queryOptions':{}}})
    filters.append({'active':True,'id':'age_days','name':'Minimum cohort age','type':'select','selectType':'list','options':[{'default':True,'key':'All ages','value':'0'},{'key':'7+ days','value':'7'},{'key':'14+ days','value':'14'}],'apl':{'apl':'','queryOptions':{}}})
    return {'id':str(uuid.uuid4()),'name':'Filter journeys and sources','type':'SmartFilter','filters':filters,'query':{'apl':'','queryOptions':{}},'numSeries':1,'logo':'','logoDark':''}


def dashboard(uid,name,description,panels):
    charts=[filter_chart()]
    layout=[{'i':charts[0]['id'],'x':0,'y':0,'w':12,'h':2,'minW':4,'minH':1}]
    y=2
    for title,kind,apl,width in panels:
        cid=str(uuid.uuid5(uuid.NAMESPACE_URL,uid+title))
        # Axiom's interactive editor treats blank lines as query boundaries.
        # Keep one contiguous query and explicitly select its entire program.
        program='\n'.join(line for line in (DECL+apl).splitlines() if line.strip())
        selection={'startLineNumber':1,'startColumn':1,'endLineNumber':len(program.splitlines()),'endColumn':len(program.splitlines()[-1])+1}
        chart={'id':cid,'name':title,'type':kind,'datasetId':DATASET,'numSeries':1,
            'query':{'apl':program,'queryOptions':{'datasets':DATASET,'editorContent':program,'selection':json.dumps(selection),'containsTimeFilter':'false','quickRange':'30d'}},
            'modified':int(dt.datetime.now(dt.UTC).timestamp()*1000)}
        if kind=='Statistic':
            chart.update(showChart=False,colorScheme='Blue')
        charts.append(chart)
        h=3 if kind=='Statistic' else 7
        if title.startswith('Data freshness'):
            h=2
        elif title.startswith('Ordered funnel'):
            h=5
        x=0
        if width==6 and len(layout)>1:
            prior=layout[-1]
            if prior['w']==6 and prior['x']==0 and prior['h']==h:
                x,y=6,prior['y']
        layout.append({'i':cid,'x':x,'y':y,'w':width,'h':h,'minW':3,'minH':min(3,h)})
        y+=h
    return {'uid':uid,'dashboard':{'uid':uid,'name':name,'description':description,'owner':'X-AXIOM-EVERYONE',
        'schemaVersion':2,'refreshTime':300,'timeWindowStart':'qr-now-30d','timeWindowEnd':'qr-now',
        'charts':charts,'layout':layout,'datasets':[DATASET]},'overwrite':False}


def build():
    freshness=('Data freshness: stale after 15 minutes','Table',FRESH,12)
    return [
      dashboard('tr-growth-funnel-v1','TR Growth | Conversion Funnel',
        'Continuously refreshed cohorts of pseudonymous visitors, not total people. First step is a recorded 1.5s engagement, not every first visit. Purchase and 7+ day return are branches, not mandatory linear steps. Conversion timestamps must be ordered. Age filter compares mature cohorts. Source gaps and cookie resets remain gaps. Automatic incremental refresh every five minutes. Browser attribution is missing Aug 23 15:56 UTC to Sep 4 18:52 UTC; missing visits are not zero traffic. Usage and conversions remain available.',[
        freshness,
        ('Daily event coverage: missing visits are not zero traffic','Table',EVENTS+" | summarize Engagement_events=countif(event=='acquisition.landing_engaged'), Signup_events=countif(event=='acquisition.signup_completed'), Activation_events=countif(event=='acquisition.first_successful_api_call'), Purchase_events=countif(event=='acquisition.credit_purchase_completed') by Day=bin(_time,1d) | extend Browser_coverage=case(Day>=datetime(2026-08-24) and Day<datetime(2026-09-04),'Missing visitor attribution',Day==datetime(2026-08-23) or Day==datetime(2026-09-04),'Partial visitor attribution','Observed records') | sort by Day desc",12),
        ('Ordered funnel and conversion percentages','Table',funnel_query(),12),
        ('Welcome test: matched attempts only (browser-reported, not all API traffic)','Table',onboarding_query(),12),
        ('Welcome test failures: reason and HTTP status','Table',ONBOARDING+" | where event=='acquisition.onboarding_call_failed' | summarize Attempts=count() by failure_reason, http_status | sort by Attempts desc",12),
        ('Legacy welcome clicks: unpaired, not an API failure rate','Table',EVENTS+" | where event in ('acquisition.first_call_started','acquisition.first_call_failed') | summarize Events=count(), Visitors=dcount(anonymous_fingerprint) by event",12),
        ('Observed signup visitors','Statistic',JOURNEY+' | summarize Signups=countif(has_signup)',6),
        ('Signup to activation (%)','Statistic',JOURNEY+' | summarize S=countif(has_signup), A=countif(active_after_signup) | project Activation_pct=iff(S>0,round(100.0*A/S,2),real(null))',6),
        ('Signup to purchase (%)','Statistic',JOURNEY+' | summarize S=countif(has_signup), P=countif(paid_after_signup) | project Paid_pct=iff(S>0,round(100.0*P/S,2),real(null))',6),
        ('Observed credit purchases (USD, not revenue)','Statistic',EVENTS+" | where event=='acquisition.credit_purchase_completed' | summarize Credit_purchase_USD=sum(amount_microdollars)/1000000.0",6),
        ('Signup cohorts: activation and paid conversion','Table',JOURNEY+' | where has_signup | summarize Signups=count(), Activated=countif(active_after_signup), Buyers=countif(paid_after_signup) by Signup_day=bin(signup,1d) | extend Activated_pct=round(100.0*Activated/Signups,2), Paid_pct=round(100.0*Buyers/Signups,2) | sort by Signup_day desc',12),
        ('All tagged actions over time','TimeSeries',EVENTS+' | summarize Events=count() by bin(_time,1d), event',12),
        ('Onboarding and payment moments','Table',EVENTS+' | summarize Events=count(), Visitors=dcount(anonymous_fingerprint), Last_seen=max(_time) by event | extend Last_seen=todatetime(Last_seen) | sort by Visitors desc',12),
      ]),
      dashboard('tr-growth-attribution-v1','TR Growth | Channels and Content',
        'Source is utm_source; channel is utm_medium. landing_path is the last tagged campaign landing at the event, not the immediately preceding page. First touch may be the first retained event; inspect its basis. Referrer is hostname only. Missing historic paths remain (other). Purchases are credit top-ups, not recognized revenue. No customer data is sent to ad platforms. Browser attribution is missing Aug 23 15:56 UTC to Sep 4 18:52 UTC.',[
        freshness,
        ('Channels driving activation and purchases','Table',by_dimension('utm_medium'),12),
        ('Sources driving activation and purchases','Table',by_dimension('utm_source, utm_medium'),12),
        ('Landing content at signup','Table',by_dimension('landing_path'),12),
        ('Campaign and creative outcomes','Table',by_dimension('utm_source, utm_campaign, creative_id'),12),
        ('First source versus latest tagged source','Table',JOURNEY+' | summarize Visitors=count(), Signups=countif(has_signup), Activated=countif(active_after_signup), Purchasers=countif(paid_after_signup) by first_source, last_source, first_touch_basis | sort by Signups desc | take 40',12),
        ('Content carried by each conversion event','Table',EVENTS+" | where event in ('acquisition.signup_completed','acquisition.first_successful_api_call','acquisition.credit_purchase_completed') | summarize Visitors=dcount(anonymous_fingerprint), Events=count() by event, landing_path, utm_source, utm_campaign | sort by Visitors desc | take 60",12),
        ('Referring domains','Table',by_dimension('referrer_domain'),12),
        ('Tag and content coverage','Table',JOURNEY+" | summarize Journeys=count(), Known_source=countif(utm_source!='(unknown)'), Named_campaign=countif(isnotempty(utm_campaign)), Known_landing=countif(landing_path !in ('(other)','(unknown)')), Stored_first_touch=countif(first_touch_basis=='stored_cookie') | extend Tagged_campaign_pct=round(100.0*Named_campaign/Journeys,1), Known_landing_pct=round(100.0*Known_landing/Journeys,1)",12),
      ]),
      dashboard('tr-growth-journeys-v1','TR Growth | Journey Explorer',
        'Paste a full anonymous_fingerprint in Visitor fingerprint to see chronological observed actions and linked workspace usage. Anonymous IDs are browser-cookie pseudonyms; account/workspace fingerprints are distinct. A workspace may represent multiple people. No prompts, outputs, names or full emails. Timeline omits recovered history.* duplicates and is not a complete clickstream. Last tagged landing is not LAST PAGE.',[
        freshness,
        ('Find a visitor and their conversion milestones','Table',JOURNEY+' | project anonymous_fingerprint, utm_source, utm_medium, utm_campaign, landing_path, first_observed_at, signup_completed_at, first_successful_api_call_at, credit_purchase_completed_at, retained_api_usage_7d_at, last_observed_at | sort by last_observed_at desc | take 100',12),
        ('Chronological tagged actions (first 300 matches)','Table',EVENTS+' | project Occurred_at=todatetime(_time), anonymous_fingerprint, event, utm_source, utm_medium, utm_campaign, creative_id, landing_path, referrer_domain, amount_microdollars | sort by Occurred_at asc | take 300',12),
        ('Welcome test attempts and outcomes','Table',ONBOARDING+' | project Occurred_at=todatetime(_time), anonymous_fingerprint, attempt_id, event, http_status, failure_reason, elapsed_ms | sort by Occurred_at desc | take 100',12),
        ('Linked daily token consumption','Table',USAGE+' | project Day=todatetime(_time), anonymous_fingerprint, marketing_workspace_fingerprint, identity_link_status, model, provider, successful_calls, input_tokens, output_tokens, usage_microdollars | sort by Day desc | take 100',12),
        ('Identity links and coverage','Table',JOURNEY+" | summarize Journeys=count(), Account_link=countif(isnotempty(account_fingerprint)), Workspace_link=countif(isnotempty(workspace_fingerprint) or isnotempty(marketing_workspace_fingerprint)), With_usage=countif(linked_usage_days>0) | extend Workspace_link_pct=round(100.0*Workspace_link/Journeys,1)",12),
      ]),
      dashboard('tr-growth-usage-v1','TR Growth | Daily Active and Tokens',
        'GCP ClickHouse organic successful-generation snapshots; synthetic probes excluded. Active workspaces, not individual-person DAU. Known workspaces only in activity counts; unlinked usage retained. UTC daily buckets; current day partial. Input includes cache reads where present; no separate cache split in this source. Other-cloud coverage and internal staff exclusions are not established. Cost is usage metadata, not cash revenue. Automatic incremental refresh every five minutes; check freshness.',[
        freshness,
        ('Daily active workspaces (known IDs)','TimeSeries',USAGE+" | where isnotempty(marketing_workspace_fingerprint) | summarize by _time, marketing_workspace_fingerprint | summarize Active_workspaces=count() by bin(_time,1d)",12),
        ('Daily input and output tokens','TimeSeries',USAGE+' | summarize Input_tokens=sum(input_tokens), Output_tokens=sum(output_tokens) by bin(_time,1d)',12),
        ('Daily successful generations and usage cost','Table',USAGE+' | summarize Successful_generations=sum(successful_calls), Input_tokens=sum(input_tokens), Output_tokens=sum(output_tokens), Usage_USD=sum(usage_microdollars)/1000000.0 by Day=bin(_time,1d) | sort by Day desc',12),
        ('Models and providers used','Table',USAGE+' | summarize Calls=sum(successful_calls), Input_tokens=sum(input_tokens), Output_tokens=sum(output_tokens), Usage_USD=sum(usage_microdollars)/1000000.0 by model, provider | sort by Calls desc | take 40',12),
        ('Source to downstream token consumption','Table',USAGE+' | summarize Calls=sum(successful_calls), Tokens=sum(input_tokens)+sum(output_tokens), Usage_USD=sum(usage_microdollars)/1000000.0 by utm_source, utm_medium, identity_link_status | sort by Calls desc',12),
        ('Usage linkage: do not hide unattributed traffic','Table',USAGE+' | summarize Aggregate_rows=count(), Calls=sum(successful_calls), Input_tokens=sum(input_tokens), Output_tokens=sum(output_tokens) by identity_link_status | sort by Calls desc',12),
        ('Frequency of use by workspace','Table',USAGE+" | where isnotempty(marketing_workspace_fingerprint) | summarize Active_days=dcount(_time), Calls=sum(successful_calls), Input_tokens=sum(input_tokens), Output_tokens=sum(output_tokens), Last_call=max(todatetime(last_call_at)) by marketing_workspace_fingerprint | extend Last_call=todatetime(Last_call) | sort by Calls desc | take 100",12),
      ]),
    ]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--validate',action='store_true')
    args=parser.parse_args()
    dashboards=build()
    Path('dashboard-definitions.json').write_text(json.dumps(dashboards,indent=2))
    failures=[]
    validations=[]
    if args.validate or args.publish:
        from scripts.axiom_growth.validate_onboarding import validate
        validate()
        print('PASS matched-attempt APL arithmetic fixture',flush=True)
        result=query(f"['{DATASET}'] | summarize Rows=count()")
        raw_rows=result['tables'][0]['columns'][0][0]
        if raw_rows >= INPUT_LIMIT:
            raise SystemExit('Input bound reached; partition queries before publication')
        for document in dashboards:
            for chart in document['dashboard']['charts']:
                apl=chart['query']['apl']
                if not apl:
                    continue
                try:
                    result=query(apl)
                    counts=[len(t['columns'][0]) if t['columns'] else 0 for t in result['tables']]
                    validations.append({'dashboard':document['uid'],'chart':chart['name'],'rows':sum(counts),'partial':False,'estimated':False})
                    print('PASS '+chart['name']+' '+str(counts),flush=True)
                except urllib.error.HTTPError as exc:
                    error=exc.read().decode()[:1500]
                    failures.append({'chart':chart['name'],'error':error})
                    print('FAIL '+chart['name']+' '+error,flush=True)
                except ValueError as exc:
                    failures.append({'chart':chart['name'],'error':str(exc)})
                    print('FAIL '+chart['name']+' '+str(exc),flush=True)
        Path('query-validation.json').write_text(json.dumps({'passed':validations,'failed':failures},indent=2))
        if failures:
            raise SystemExit('Dashboard publication blocked by query failures')
    if args.publish:
        existing={r['uid']:r for r in api('GET','/v2/dashboards')}
        published=[]
        for document in dashboards:
            prior=existing.get(document['uid'])
            if prior:
                document['version']=prior['version']
                response=api('PUT','/v2/dashboards/uid/'+prior['uid'],document)
            else:
                response=api('POST','/v2/dashboards',document)
            published.append(response)
            print(json.dumps({'name':document['dashboard']['name'],'result':response.get('status'),'id':response.get('dashboard',{}).get('id')}),flush=True)
        Path('published-dashboards.json').write_text(json.dumps(published,indent=2))


if __name__=='__main__':
    main()
