#!/usr/bin/env python3
"""Atlas Macro Keyless DAILY SHADOW, independent of all portfolio decisions.
Formula provenance: macro_keyless_research.py, commit 26ac3547b10e36fadb680a633763557e1c75f917.
No credentials, no broker, no order creation, no mutation of state.json.
"""
from __future__ import annotations
import bisect,csv,datetime as dt,hashlib,io,json,math,statistics,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo
import exchange_calendars as xcals
import pandas as pd
ROOT=Path(__file__).resolve().parent
CACHE=ROOT/'live_cache/macro_keyless'
REPORT=ROOT/'reports/latest_macro_keyless.json'
STATUS=ROOT/'reports/latest_status.json'
AUDIT=ROOT/'reports/latest_quote_audit.json'
TICKERS=('IWD','IWB','IWF','IWN','IWM','IWO')
CREDIT_ETFS=('HYG','IEI')
YIELD_TICKERS=()
END=None
NY=ZoneInfo('America/New_York')
def fetch(url,timeout=30):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 Atlas Macro Keyless','Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=timeout) as f:return f.read()
def read(p):
    return json.loads(p.read_text(encoding='utf-8'))
def save(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_name(p.name+'.tmp')
    t.write_text(json.dumps(obj,sort_keys=True,allow_nan=False,separators=(',',':'))+'\n',encoding='utf-8')
    t.replace(p)
def shift(d,n):
    return (dt.date.fromisoformat(d)+dt.timedelta(days=n)).isoformat()
def monthshift(m,n):
    y,k=map(int,m.split('-'));q=y*12+k-1+n
    return f'{q//12:04d}-{q%12+1:02d}'
def monthend(m):
    return shift(monthshift(m,1)+'-01',-1)
def quantile(a,p):
    a=sorted(a);i=(len(a)-1)*p;j=int(i)
    return a[j]+(a[min(j+1,len(a)-1)]-a[j])*(i-j)
def load_etf(t):
    errors=[]
    start=int(dt.datetime(2001,1,1,tzinfo=dt.timezone.utc).timestamp())
    end=int(dt.datetime.fromisoformat(shift(END,1)).replace(tzinfo=dt.timezone.utc).timestamp())
    for host in ('query1','query2'):
        try:
            url=f'https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(t,safe="")}?period1={start}&period2={end}&interval=1d&includeAdjustedClose=true'
            raw=fetch(url,timeout=30);r=json.loads(raw)['chart']['result'][0]
            assert r['meta']['currency']=='USD'
            ts=r['timestamp'];a=r['indicators']['quote'][0]['close'] if t in YIELD_TICKERS else r['indicators']['adjclose'][0]['adjclose']
            rows={dt.datetime.fromtimestamp(x,dt.timezone.utc).date().isoformat():float(y) for x,y in zip(ts,a) if y is not None and math.isfinite(y) and y>0}
            rows={d:v for d,v in rows.items() if d<=END and (t not in YIELD_TICKERS or v<=25)}
            if len(rows)<2000:raise ValueError('insufficient history')
            return t,rows,dict(source='YAHOO_'+host.upper()+('_YIELD_INDEX' if t in YIELD_TICKERS else '_ADJCLOSE_USD'),first=min(rows),last=max(rows),count=len(rows),sha256=hashlib.sha256(raw).hexdigest(),errors=errors)
        except Exception as e:errors.append(host+':'+type(e).__name__)
    return t,{},dict(errors=errors)

def load_treasury():
    """Official daily CMT curve, no key. Try public CSV then annual XML."""
    import xml.etree.ElementTree as ET
    errors=[];rows={};hashes=[]
    def parse_csv(raw):
        reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig','replace')))
        for q in reader:
            try:
                d=dt.datetime.strptime(q['Date'],'%m/%d/%Y').date().isoformat()
                a=float(q['10 Yr']);b=float(q['1 Mo'])
                if '2001-01-01'<=d<=END and -2<=a<=25 and -2<=b<=25:rows[d]=(a,b)
            except (KeyError,ValueError,TypeError):continue
    for url in (
        'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/all?_format=csv&page=&type=daily_treasury_yield_curve',
        'https://home.treasury.gov/system/files/276/yield-curve-rates-1990-2024.csv',
        'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/'+END[:4]+'/all?_format=csv&page=&type=daily_treasury_yield_curve'):
        try:
            raw=fetch(url,timeout=18);n=len(rows);parse_csv(raw)
            if len(rows)>n:hashes.append(hashlib.sha256(raw).hexdigest())
            if len(rows)>4500 and max(rows)>=shift(END,-5):break
        except Exception as e:errors.append(type(e).__name__)
    if len(rows)<4500 or max(rows,default='')<shift(END,-5):
        def year(y):
            try:
                raw=fetch('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value='+str(y),timeout=15)
                out={}
                for el in ET.fromstring(raw).iter():
                    if el.tag.rsplit('}',1)[-1]!='properties':continue
                    p={x.tag.rsplit('}',1)[-1]:x.text for x in el}
                    try:
                        d=p['NEW_DATE'][:10];a=float(p['BC_10YEAR']);b=float(p['BC_1MONTH'])
                        if '2001-01-01'<=d<=END and -2<=a<=25 and -2<=b<=25:out[d]=(a,b)
                    except (KeyError,ValueError,TypeError):continue
                return out,hashlib.sha256(raw).hexdigest() if out else None
            except Exception:return {},None
        ys=[y for y in range(2001,int(END[:4])+1) if sum(d.startswith(str(y)) for d in rows)<180 or y==int(END[:4])]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for a,h in pool.map(year,ys):
                rows.update(a)
                if h:hashes.append(h)
    meta=dict(source='US_TREASURY_CMT_OFFICIAL',first=min(rows,default=None),last=max(rows,default=None),count=len(rows),sha256_sources=hashes,errors=errors)
    return [('UST10',{d:v[0] for d,v in rows.items()},meta),('UST1M',{d:v[1] for d,v in rows.items()},meta)]

class CreditETF:
    """Different economic proxy, not Moody's spread: weak 63-session HYG/IEI relative return."""
    def __init__(self,a,b):
        self.days=sorted(set(a)&set(b));ratio=[a[d]/b[d] for d in self.days]
        self.val=[None if i<63 else ratio[i]/ratio[i-63]-1 for i in range(len(ratio))]
        self.monthly={}
        for d,v in zip(self.days,self.val):
            if v is not None:self.monthly[d[:7]]=v
    def flag(self,decision,lag):
        cut=shift(decision,-lag);i=latest(self.days,cut)
        if i<63 or stale(cut,self.days[i]):return None,None
        m=self.days[i][:7];h=[v for k,v in self.monthly.items() if monthshift(m,-120)<=k<m]
        if len(h)<60:return None,None
        return self.val[i]<=quantile(h,.2),self.days[i]

def dated(a):
    keys=sorted(a);return keys,[a[k] for k in keys]
def latest(keys,day):
    return bisect.bisect_right(keys,day)-1
def stale(day,d,limit=10):
    return d<shift(day,-limit)
class DailyFlag:
    def __init__(self,a,b,kind):
        self.kind=kind
        common=sorted(set(a)&set(b))
        self.days=common;self.val=[b[d]-a[d] for d in common] if kind=='credit' else [a[d]-b[d] for d in common]
        self.ma=[None]*len(common)
        self.monthly={}
        group={}
        for i,d in enumerate(common):
            group.setdefault(d[:7],[]).append(self.val[i])
            if i>=20:self.ma[i]=statistics.mean(self.val[i-20:i+1])
        self.monthly={m:statistics.mean(v) for m,v in group.items() if len(v)>=12}
        self.months=sorted(self.monthly)
    def flag(self,decision,lag):
        asof=shift(decision,-lag);i=latest(self.days,asof)
        if i<63 or stale(asof,self.days[i]) or self.ma[i] is None:return None,None
        m=self.days[i][:7];h=[self.monthly[k] for k in self.months if monthshift(m,-120)<=k<m]
        if len(h)<60:return None,None
        x=self.ma[i]
        if self.kind=='credit':
            flag=x>=quantile(h,.8) or (self.ma[i-63] is not None and x-self.ma[i-63]>=.5)
        else:flag=x<=quantile(h,.2)
        return bool(flag),self.days[i]
def breadth(etfs,decision,lag=1):
    cut=shift(decision,-lag)
    flags=[];days=[]
    for keys,vals in etfs:
        i=latest(keys,cut)
        if i<63 or stale(cut,keys[i]):return None,None,None
        flags.append(vals[i]>vals[i-63]);days.append(keys[i])
    return sum(flags)<=2,sum(flags),min(days)

def us_cutoff(now):
    if now.tzinfo is None:raise ValueError('timezone required')
    cal=xcals.get_calendar('XNYS')
    sess=cal.date_to_session(pd.Timestamp(now.astimezone(NY).date()),direction='previous')
    if now<cal.session_close(sess).to_pydatetime()+dt.timedelta(minutes=90):
        sess=cal.previous_session(sess)
    return sess.date().isoformat()
def cache_read(key):
    p=CACHE/(key+'.json')
    try:return read(p)
    except (OSError,ValueError):return {}
def use_cache_or_new(key,new,meta,now,old=None):
    old=cache_read(key) if old is None else old
    prev=old.get('rows',{})
    valid=bool(new) and all(d<=END and math.isfinite(float(x)) for d,x in new.items())
    # No regressions in historical coverage. Do not mix partial adjusted closes with prior history.
    if valid and prev and (not set(prev).issubset(new) or max(new)<max(prev)):
        valid=False
        meta=dict(meta,errors=meta.get('errors',[])+['HISTORY_REGRESSION_REJECTED'])
    if valid:
        payload={'rows':new,'meta':meta,'downloaded_at_utc':now.isoformat()}
        save(CACHE/(key+'.json'),payload)
        return new,dict(meta,acquisition='ONLINE',downloaded_at_utc=now.isoformat())
    if prev:
        return prev,dict(old.get('meta',{}),acquisition='CACHE_AFTER_ERROR',downloaded_at_utc=old.get('downloaded_at_utc'),refresh_errors=meta.get('errors',[]))
    return {},dict(meta,acquisition='UNAVAILABLE')
def already_current(key,expected,now):
    old=cache_read(key)
    rows=old.get('rows',{})
    if not rows:return None
    if max(rows)==expected and str(old.get('downloaded_at_utc',''))[:10]==now.date().isoformat():
        return rows,dict(old.get('meta',{}),acquisition='CACHE_VERIFIED_TODAY',downloaded_at_utc=old.get('downloaded_at_utc'))
    return None
def acquire(now):
    out={};audit={}
    def one(t):
        prior=already_current(t,END,now)
        if prior:return t,*prior
        k,rows,meta=load_etf(t)
        # Discard any non-session timestamps, and never accept incomplete history as fresh.
        cal=xcals.get_calendar('XNYS')
        rows={d:v for d,v in rows.items() if cal.is_session(pd.Timestamp(d))}
        if rows:meta=dict(meta,first=min(rows),last=max(rows),count=len(rows))
        return k,*use_cache_or_new(k,rows,meta,now)
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures=[pool.submit(one,t) for t in TICKERS+CREDIT_ETFS]
        ready=[already_current(k,END,now) for k in ('UST10','UST1M')]
        treasury=None if all(ready) else pool.submit(load_treasury)
        for f in futures:
            k,a,m=f.result();out[k]=a;audit[k]=m
        if treasury:
            pairs=treasury.result()
            for k,rows,meta in pairs:
                a,m=use_cache_or_new(k,rows,meta,now);out[k]=a;audit[k]=m
        else:
            for k,(a,m) in zip(('UST10','UST1M'),ready):out[k]=a;audit[k]=m
    for k in out:
        audit[k]['last']=max(out[k],default=None)
        audit[k]['count']=len(out[k])
    return out,audit
def score(inputs,asof,credit_lag=1,rate_lag=3,breadth_lag=1):
    credit=CreditETF(inputs.get('HYG',{}),inputs.get('IEI',{}))
    rates=DailyFlag(inputs.get('UST10',{}),inputs.get('UST1M',{}),'rate')
    etfs=[dated(inputs.get(t,{})) for t in TICKERS]
    c,cd=credit.flag(asof,credit_lag)
    r,rd=rates.flag(asof,rate_lag)
    b,n,bd=breadth(etfs,asof,breadth_lag)
    return {'asof':asof,'score':sum((c,r,b)) if all(x is not None for x in (c,r,b)) else None,
            'credit':c,'rates':r,'breadth':b,'breadth_healthy':n,
            'credit_observation':cd,'rates_observation':rd,'breadth_observation':bd,
            'lags_calendar_days':{'credit':credit_lag,'rates':rate_lag,'breadth':breadth_lag}}
def report(now=None,original_status=None):
    global END
    now=(now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    END=us_cutoff(now)
    old=original_status or {}
    orig=old.get('current_recomputed_signal') or old.get('latest_signal') or {}
    session=orig.get('decision_session') or old.get('last_nav_session')
    result={'checked_at_utc':now.isoformat(),'mode':'SHADOW_ONLY_NO_ORDER_INFLUENCE',
            'model':'ATLAS_MACRO_KEYLESS_V1','expected_us_close':END,'original_session':session,
            'original_stress':orig.get('stress'),'research_reference':'26ac3547b10e36fadb680a633763557e1c75f917',
            'rate_source':'US_TREASURY_10Y_1M','credit_source':'HYG_IEI_RELATIVE_63_SESSION',
            'breadth_source':list(TICKERS),'no_broker_execution':True,
            'note':'Current monitor uses latest completed US observations. Decision comparison reproduces tested 1/3/1 calendar-day lags; no effect on original V2.3 signals or orders.'}
    try:
        inputs,meta=acquire(now)
        result['sources']=meta
        current=score(inputs,END,0,0,0)
        causal=score(inputs,session) if session else None
        # All eight ETF histories must reach the expected completed US session.
        # Treasury can publish on a different holiday calendar; allow at most 5 calendar days,
        # always report actual observation dates. Never mark missing input as a zero stress flag.
        missing=[k for k in TICKERS+CREDIT_ETFS if max(inputs.get(k,{}),default='')!=END]
        for k in ('UST10','UST1M'):
            d=max(inputs.get(k,{}),default='')
            if not d or d<shift(END,-5):missing.append(k)
        if current['score'] is None:missing.append('INSUFFICIENT_SIGNAL_HISTORY')
        result.update(status='CURRENT_SHADOW' if not missing else 'DEGRADED_SHADOW',
                      missing_or_stale=missing,latest_monitor=current,decision_comparison=causal)
        if missing:
            result['latest_monitor']['score']=None
        original_score=(orig.get('stress') or {}).get('score')
        result['decision_score_delta']=causal['score']-original_score if causal and causal['score'] is not None and original_score is not None and not missing else None
    except Exception as exc:
        result.update(status='BLOCKED_SHADOW',error=type(exc).__name__+': '+str(exc)[:160],
                      latest_monitor=None,decision_comparison=None,decision_score_delta=None)
    save(REPORT,result)
    # Keep all original fields and append namespaced information; never touch state.json.
    for p in (STATUS,AUDIT):
        if p.exists():
            obj=read(p);obj['macro_keyless_shadow']=result;save(p,obj)
    print('MACRO_KEYLESS_SHADOW',json.dumps({k:v for k,v in result.items() if k!='sources'},allow_nan=False),flush=True)
    return 0 if result['status']=='CURRENT_SHADOW' else 3

if __name__=='__main__':
    raise SystemExit(report(original_status=read(STATUS) if STATUS.exists() else {}))
