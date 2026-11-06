#!/usr/bin/env python3
"""Independent ECB daily EUR FX reference audit for Atlas V4 PAPER.

Historical ECB rates are *retrospective reference observations*, not intraday
execution quotes or a proven time-versioned point-in-time archive. The safe
research convention is to use only a PREVIOUS published business-day fixing.
Yahoo-derived execution quotes remain separately attributed; never silently
replace them with interpolated annual currency values.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,hashlib,io,json,math,urllib.parse,urllib.request
from pathlib import Path
from dataset_daily import DATA,atomic,json_save,csv_write,rows_load

ROOT=DATA/'fx_ecb'
ECB='https://data-api.ecb.europa.eu/service/data/EXR/'
CURRENCIES=('USD','JPY','GBP','CHF','CAD','AUD','DKK','HKD','INR','KRW','SEK','SGD','NOK','BRL','ZAR','TWD')
FIELDS=['date','currency','units_per_eur','source','first_observed_at_utc','last_checked_at_utc','raw_sha256','backfilled_not_certified_pit']
REVISIONS=['seen_at_utc','currency','date','previous_units_per_eur','new_units_per_eur','new_raw_sha256']

def parse_ecb_csv(raw:bytes, through:dt.date):
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    needed={'TIME_PERIOD','OBS_VALUE','CURRENCY'}
    if not needed.issubset(reader.fieldnames or []):
        raise ValueError('ECB CSV schema missing '+str(sorted(needed-set(reader.fieldnames or []))))
    result={}
    for row in reader:
        cur=(row.get('CURRENCY') or '').upper()
        if not cur or cur not in CURRENCIES:continue
        if row.get('FREQ','D') not in ('D',''):continue
        if row.get('CURRENCY_DENOM','EUR') not in ('EUR',''):continue
        try:
            date=dt.date.fromisoformat(row['TIME_PERIOD'])
            rate=float(row['OBS_VALUE'])
        except (TypeError,ValueError,KeyError):
            continue
        if date>through:raise ValueError('ECB response contains future-dated rates')
        if not math.isfinite(rate) or rate<=0:continue
        key=(cur,date.isoformat())
        if key in result and result[key]!=rate:raise ValueError('ECB duplicate conflicting observation '+str(key))
        result[key]=rate
    if not result:raise ValueError('ECB returned no valid reference observations')
    return result

def api_url(start:dt.date,end:dt.date):
    symbols='+'.join(CURRENCIES)
    params=urllib.parse.urlencode({'startPeriod':start.isoformat(),'endPeriod':end.isoformat(),'format':'csvdata'})
    return ECB+'D.'+symbols+'.EUR.SP00.A?'+params

def download(start,end):
    request=urllib.request.Request(api_url(start,end),
        headers={'Accept':'text/csv','User-Agent':'Atlas-V4-independent-paper-research/1.0'})
    with urllib.request.urlopen(request,timeout=85) as response:raw=response.read(18_000_000)
    if len(raw)>=18_000_000:raise ValueError('ECB download exceeded conservative size bound')
    return raw

def published_prior(series, currency, decision_day, max_age_days=7):
    """Strict previous observation date: never use same-day unpublished FX."""
    candidates=[(dt.date.fromisoformat(row['date']),float(row['units_per_eur']))
                for row in series if row['currency']==currency and row['date']<decision_day.isoformat()]
    if not candidates:raise ValueError('No previous published ECB FX observation: '+currency)
    d,rate=max(candidates,key=lambda x:x[0])
    if (decision_day-d).days>max_age_days:raise ValueError('Stale ECB FX '+currency)
    return d,rate

def merge(records, observed_utc, raw_sha, folder=ROOT):
    folder.mkdir(parents=True,exist_ok=True)
    revisions=rows_load(folder/'revisions.csv')
    summary={}
    for cur in CURRENCIES:
        path=folder/(cur+'.csv')
        previous={r['date']:r for r in rows_load(path)}
        additions=changes=0
        for (currency,day),rate in sorted(records.items()):
            if currency!=cur:continue
            price=format(rate,'.12g')
            old=previous.get(day)
            if old is None:
                previous[day]={'date':day,'currency':cur,'units_per_eur':price,
                    'source':'ECB_EXR_DAILY_REFERENCE','first_observed_at_utc':observed_utc,
                    'last_checked_at_utc':observed_utc,'raw_sha256':raw_sha,
                    'backfilled_not_certified_pit':'true'}
                additions+=1
            elif abs(float(old['units_per_eur'])-rate)>max(1e-10,rate*1e-10):
                revisions.append({'seen_at_utc':observed_utc,'currency':cur,'date':day,
                    'previous_units_per_eur':old['units_per_eur'],
                    'new_units_per_eur':price,'new_raw_sha256':raw_sha})
                old['units_per_eur']=price;old['last_checked_at_utc']=observed_utc
                old['raw_sha256']=raw_sha
                changes+=1
        if additions or changes:
            csv_write(path,[previous[d] for d in sorted(previous)],FIELDS)
        if previous:
            summary[cur]={'count':len(previous),'first':min(previous),'last':max(previous),
                          'new':additions,'revisions':changes}
    if revisions:csv_write(folder/'revisions.csv',revisions,REVISIONS)
    return summary

def run(now=None, getter=None, base='2012-01-01'):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('now must be timezone-aware')
    day=now.date()
    meta=ROOT/'audit/latest.json'
    start=dt.date.fromisoformat(base)
    if meta.exists():start=max(start,day-dt.timedelta(days=45))
    output={'checked_at_utc':now.isoformat(),'source':'ECB EXR daily reference, fetched retrospectively',
        'reference_only':True,'original_confidence70_pit_certified':False,
        'execution_quote_source_unchanged':True,'no_annual_interpolation':True,
        'historical_revisions_time_versioned':False,'request_start':str(start),
        'request_end':str(day)}
    try:
        raw=(getter or download)(start,day)
        records=parse_ecb_csv(raw,day)
        observed=now.isoformat();digest=hashlib.sha256(raw).hexdigest()
        summary=merge(records,observed,digest)
        output.update({'status':'ECB_REFERENCE_UPDATED','raw_sha256':digest,
            'currency_coverage':summary,'currencies_with_history':len(summary),
            'caveat':'Retroactive ECB history is not evidence of a historical timestamped PIT archive'})
    except (OSError,ValueError,KeyError,UnicodeDecodeError,urllib.error.URLError) as exc:
        output.update({'status':'ECB_REFERENCE_UNAVAILABLE','reason':type(exc).__name__+': '+str(exc)[:250]})
    json_save(meta,output)
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--start',default='2012-01-01')
    args=p.parse_args()
    result=run(base=args.start)
    print(json.dumps({'status':result['status'],'currencies_with_history':result.get('currencies_with_history',0),
                      'reason':result.get('reason')},ensure_ascii=False))
