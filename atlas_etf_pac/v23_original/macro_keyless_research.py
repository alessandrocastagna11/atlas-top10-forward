#!/usr/bin/env python3
"""Read-only macro near-real-time research on actual EUR ETFs; no live writes."""
import ast, bisect, copy, csv, datetime as dt, hashlib, io, json, math, os, re, statistics, subprocess, sys, types, urllib.parse, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import run_extended as ref
OUT=ref.HERE/'reports/macro_keyless_research.json'
END='2026-11-27'
SNAP='58a747385159cd2b984912a36469e72b340037f6'
TICKERS=('IWD','IWB','IWF','IWN','IWM','IWO')
SERIES=('DAAA','DBAA','DGS10','DGS1MO')
CREDIT_ETFS=('HYG','IEI')
YIELD_TICKERS=('^TNX','^IRX')
def gitfile(p):
    return subprocess.check_output(['git','show',SNAP+':atlas_etf_pac/'+p])
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
def load_daily_fred(s):
    errors=[]
    # Optional official API secret, then public graph CSV and independent table endpoint.
    urls=[]
    key=os.environ.get('FRED_API_KEY','').strip()
    if key:
        urls.append(('FRED_API','https://api.stlouisfed.org/fred/series/observations?'+urllib.parse.urlencode(dict(series_id=s,api_key=key,file_type='json',observation_start='2001-01-01',observation_end=END))))
    urls.extend([('FRED_GRAPH_CSV','https://fred.stlouisfed.org/graph/fredgraph.csv?'+urllib.parse.urlencode(dict(id=s,cosd='2001-01-01',coed=END))),('FRED_TABLE','https://fred.stlouisfed.org/data/'+s+'.txt')])
    urls.insert(0,('ALFRED_CSV','https://alfred.stlouisfed.org/graph/alfredgraph.csv?'+urllib.parse.urlencode(dict(id=s,cosd='2001-01-01',coed=END))))
    best={};meta={}
    for name,url in urls:
        try:
            raw=ref.fetch(url,timeout=22);txt=raw.decode('utf-8-sig','replace')
            if name=='FRED_API':
                pairs=[(q['date'],q['value']) for q in json.loads(txt)['observations']]
            elif name in ('FRED_GRAPH_CSV','ALFRED_CSV'):
                reader=csv.DictReader(io.StringIO(txt));col=next((k for k in reader.fieldnames or [] if k==s or k.startswith(s+'_')),None)
                if col is None:raise ValueError('missing series column')
                pairs=[(q.get('observation_date') or q.get('DATE'),q.get(col)) for q in reader]
            else:
                pairs=re.findall(r'(?m)^\s*(\d{4}-\d{2}-\d{2})\s+([-+]?\d+(?:\.\d+)?)\s*$',txt)
            rows={}
            for d,x in pairs:
                if not d or not '2001-01-01'<=d<=END:continue
                try:v=float(x)
                except (ValueError,TypeError):continue
                if math.isfinite(v):rows[d]=v
            if len(rows)<1000:raise ValueError('insufficient daily observations')
            candidate={'source':name,'first':min(rows),'last':max(rows),'count':len(rows),'sha256':hashlib.sha256(raw).hexdigest()}
            if not best or max(rows)>max(best):best,meta=rows,candidate
            if max(rows)>=shift(END,-5):break
        except Exception as e:
            # Do not log request URL or potential API secrets.
            errors.append(name+':'+type(e).__name__)
    return s,best,dict(meta,errors=errors)
def load_etf(t):
    errors=[]
    start=int(dt.datetime(2001,1,1,tzinfo=dt.timezone.utc).timestamp())
    end=int(dt.datetime.fromisoformat(shift(END,1)).replace(tzinfo=dt.timezone.utc).timestamp())
    for host in ('query1','query2'):
        try:
            url=f'https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(t,safe="")}?period1={start}&period2={end}&interval=1d&includeAdjustedClose=true'
            raw=ref.fetch(url,timeout=30);r=json.loads(raw)['chart']['result'][0]
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
        'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2026/all?_format=csv&page=&type=daily_treasury_yield_curve'):
        try:
            raw=ref.fetch(url,timeout=18);n=len(rows);parse_csv(raw)
            if len(rows)>n:hashes.append(hashlib.sha256(raw).hexdigest())
            if len(rows)>4500 and max(rows)>=shift(END,-5):break
        except Exception as e:errors.append(type(e).__name__)
    if len(rows)<4500 or max(rows,default='')<shift(END,-5):
        def year(y):
            try:
                raw=ref.fetch('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value='+str(y),timeout=15)
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
        ys=[y for y in range(2001,2027) if sum(d.startswith(str(y)) for d in rows)<180 or y==2026]
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

def read_prices(path):
    rows=[dict(date=q['date'],close=float(q['close'])) for q in csv.DictReader(io.StringIO(gitfile(path).decode('utf-8-sig')))]
    assert [q['date'] for q in rows]==sorted({q['date'] for q in rows})
    assert all(q['close']>0 for q in rows)
    return [q for q in rows if q['date']<=END]
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
def main():
    source,digest=ref.check_reference()
    native=ref.load_original_engine(source);native.xirr=ref.stable_xirr_factory(native)
    # Native initialization requires these files although stress is fully supplied by the explicit adapter below.
    native.load_fred_monthly=lambda filename:{}
    native.load_six_portfolios_monthly=lambda :{}
    cfg=json.loads((ref.EXTRACT/'config.json').read_text())
    # exact live macro class and frozen signal engine from immutable snapshot
    root=ref.HERE/'macro_nrt_snapshot';p=root/'v23_original';p.mkdir(parents=True,exist_ok=True)
    file=p/'atlas_v23_live.py';s=gitfile('v23_original/atlas_v23_live.py')
    assert hashlib.sha256(s).hexdigest()=='703e4ecc9509ec8278ecd39e008cdc12f53091cdf8c833e3f7aadc4032cb196e'
    file.write_bytes(s);(p/'pinned_reference.zip').write_bytes(gitfile('v23_original/pinned_reference.zip'))
    v=types.ModuleType('nrt_frozen_live');v.__file__=str(file);sys.modules[v.__name__]=v;exec(compile(s,str(file),'exec'),v.__dict__)
    cfg_live=json.loads(gitfile('config.json'));macro=json.loads(gitfile('live_cache/macro_v23.json'))
    tree=ast.parse(gitfile('engine.py').decode())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CausalStress')
    ns={'v23':v,'ROOT':root,'json':json,'zipfile':zipfile}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),'CausalStress_snapshot','exec'),ns)
    original=ns['CausalStress'](cfg_live,macro)
    eq=read_prices('live_cache/yahoo_EUNL_DE.csv');bd=read_prices('live_cache/yahoo_CSBGE3_MI.csv')
    assert eq[-1]['date']==END and bd[-1]['date']==END
    bk,bv=dated({q['date']:q['close'] for q in bd})
    data=[dict(date=q['date'],ret=q['close']/eq[i-1]['close']-1 if i else 0.,rf=0.) for i,q in enumerate(eq)]
    bonds={q['date']:bv[latest(bk,q['date'])] for q in eq}
    native.load_ff=lambda start,end:[dict(q) for q in data if start<=q['date']<=end]
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures=[pool.submit(load_daily_fred,s) for s in SERIES]+[pool.submit(load_etf,t) for t in TICKERS+CREDIT_ETFS+YIELD_TICKERS]
        treasury=pool.submit(load_treasury)
        got=[f.result() for f in futures]+treasury.result()
    inputs={k:a for k,a,m in got};meta={k:m for k,a,m in got}
    print('NRT_SOURCE_AUDIT',json.dumps(meta),flush=True)
    cr=DailyFlag(inputs['DAAA'],inputs['DBAA'],'credit')
    ra=DailyFlag(inputs['DGS10'],inputs['DGS1MO'],'rate')
    etfs=[dated(inputs[t]) for t in TICKERS]
    cred_proxy=CreditETF(inputs['HYG'],inputs['IEI'])
    official_rate=bool(len(inputs['UST10'])>2000 and len(inputs['UST1M'])>2000 and max(inputs['UST10'])>=shift(END,-5))
    yahoo_rate=bool(inputs['^TNX'] and inputs['^IRX'])
    alt_rate=DailyFlag(inputs['UST10'],inputs['UST1M'],'rate') if official_rate else DailyFlag(inputs['^TNX'],inputs['^IRX'],'rate')
    rate_source='US_TREASURY_10Y_1M' if official_rate else 'YAHOO_10Y_13W_PROXY' if yahoo_rate else 'UNAVAILABLE'
    avail={'A':bool(inputs['DAAA'] and inputs['DBAA']),'B':all(inputs[s] for s in SERIES),'C':all(inputs[s] for s in SERIES+TICKERS),'BREADTH_ONLY':all(inputs[t] for t in TICKERS)}
    avail.update(CREDIT_PROXY=bool(inputs['HYG'] and inputs['IEI']),RATE_ONLY=official_rate or yahoo_rate,FULL_KEYLESS=bool(inputs['HYG'] and inputs['IEI']) and (official_rate or yahoo_rate) and avail['BREADTH_ONLY'])
    # Original previous month uses final revised history, not proven release vintages.
    # Secondary reference 45d after month-end is a conservative publication-delay proxy.
    report={'source_sha256':digest,'snapshot':SNAP,'last':END,'sessions':len(eq),'source_audit':meta,
            'availability':avail,'rate_source':rate_source,'method':'EUNL/CSBGE3 actual EUR adjusted closes, original native two sleeves and fee rules, zero cash interest. Macro reference current-vintage prior completed month; alternate reference 45 calendar days after month end. Daily FRED 21 observation averages compared with prior 120 completed monthly averages, min 60; credit change 63 obs; FRED lag 3 or 7 calendar days. US breadth 63 prior sessions; lag 1 calendar day or 3 for sensitivity. No vintage certification; no synthetic fill.','runs':[],'scores':{},'keyless_method':'HYG/IEI 63-session relative total return, bottom 20th percentile of prior 120 completed monthly observations, min 60. Rates US Treasury official 10Y-1M preferred, Yahoo 10Y-13W yield index if unavailable; not identical to FF RF. Any missing required daily signal falls back to original, reported explicitly.'}
    def baseline(d,lagged=False):
        if not lagged:return original.regime(d)
        m=d[:7]
        while shift(monthend(m),45)>d:m=monthshift(m,-1)
        return original.regime(monthshift(m,1)+'-01')
    for lagged,delay in [(False,3),(True,3),(True,7)]:
        label=('lag45' if lagged else 'original')+'_daily'+str(delay)
        scores={}
        for mode in ('BASE','A','B','C','BREADTH_ONLY','CREDIT_PROXY','RATE_ONLY','FULL_KEYLESS'):
            if mode!='BASE' and not avail[mode]:continue
            entries={};fallback=0;changes=0;up=0;down=0;coverage=0
            for q in eq:
                d=q['date'];b=baseline(d,lagged);z=dict(b);bad=False;used=[]
                if mode in ('A','B','C'):
                    f,stamp=cr.flag(d,delay)
                    if f is None:bad=True
                    else:z['credit']=f;used.append(stamp)
                if mode in ('B','C'):
                    f,stamp=ra.flag(d,delay)
                    if f is None:bad=True
                    else:z['rates']=f;used.append(stamp)
                if mode in ('CREDIT_PROXY','FULL_KEYLESS'):
                    f,stamp=cred_proxy.flag(d,1 if delay==3 else 3)
                    if f is None:bad=True
                    else:z['credit']=f;used.append(stamp)
                if mode in ('RATE_ONLY','FULL_KEYLESS'):
                    f,stamp=alt_rate.flag(d,delay)
                    if f is None:bad=True
                    else:z['rates']=f;used.append(stamp)
                if mode in ('C','BREADTH_ONLY','FULL_KEYLESS'):
                    f,n,stamp=breadth(etfs,d,1 if delay==3 else 3)
                    if f is None:bad=True
                    else:z['breadth']=f;z['breadth_healthy']=n;used.append(stamp)
                if mode!='BASE':
                    if bad or any(z.get(k) is None for k in ('credit','rates','breadth')):
                        z=dict(b);fallback+=1
                    else:
                        z['score']=sum(bool(z[k]) for k in ('credit','rates','breadth'));coverage+=1
                        z['source']='NRT_PROXY';z['nrt_dates']=used
                changes+=z['score']!=b['score'];up+=z['score']>b['score'];down+=z['score']<b['score']
                entries[d]=z
            scores[mode]=entries
            stats=dict(coverage=coverage,fallback=fallback,different=changes,up=up,down=down,latest=entries[END])
            report['scores'][label+'_'+mode]=stats
            print('NRT_SCORE',label,mode,json.dumps(stats,allow_nan=False),flush=True)
        for start in ('2010-01-04','2013-01-02','2016-01-04','2020-01-02','2022-01-03'):
            for mode,entries in scores.items():
                native.Simulator.stress_regime=lambda self,i:entries[self.rows[i]['date']]
                sim=native.Simulator(dict(cfg,start_date=start,end_date=END),OUT.parent)
                sim.bond=[bonds[q['date']] for q in sim.rows]
                z,nav,flows=sim.run()
                assert all(q['nav_eur']>0 for q in nav)
                assert z['orders_total']==sum(z['order_breakdown'].values())
                row=dict(case=label,mode=mode,start=start,cagr=z['twr_cagr_net'],dd=z['max_drawdown'],xirr=z['xirr'],final=z['final_liquidated_eur'],active=z['orders_strategy'],sales=sum(q['side']=='SELL' for q in sim.events),core_exits=[q['date'] for q in sim.events if q['note']=='core exit'])
                report['runs'].append(row)
                print('NRT_RESULT',json.dumps(row,allow_nan=False),flush=True)
    OUT.write_text(json.dumps(report,indent=2,allow_nan=False,default=str))
    print('NRT_COMPLETE',json.dumps({'runs':len(report['runs']),'availability':avail}),flush=True)
if __name__=='__main__':main()
