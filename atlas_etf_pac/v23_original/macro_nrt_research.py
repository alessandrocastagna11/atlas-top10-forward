#!/usr/bin/env python3
"""Read-only macro near-real-time research on actual EUR ETFs; no live writes."""
import ast, bisect, copy, csv, datetime as dt, hashlib, io, json, math, os, re, statistics, subprocess, sys, types, urllib.parse, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import run_extended as ref
OUT=ref.HERE/'reports/macro_nrt_research.json'
END='2026-11-27'
SNAP='58a747385159cd2b984912a36469e72b340037f6'
TICKERS=('IWD','IWB','IWF','IWN','IWM','IWO')
SERIES=('DAAA','DBAA','DGS10','DGS1MO')
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
    best={};meta={}
    for name,url in urls:
        try:
            raw=ref.fetch(url,timeout=22);txt=raw.decode('utf-8-sig','replace')
            if name=='FRED_API':
                pairs=[(q['date'],q['value']) for q in json.loads(txt)['observations']]
            elif name=='FRED_GRAPH_CSV':
                pairs=[(q.get('observation_date') or q.get('DATE'),q.get(s)) for q in csv.DictReader(io.StringIO(txt))]
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
            url=f'https://{host}.finance.yahoo.com/v8/finance/chart/{t}?period1={start}&period2={end}&interval=1d&includeAdjustedClose=true'
            raw=ref.fetch(url,timeout=30);r=json.loads(raw)['chart']['result'][0]
            assert r['meta']['currency']=='USD'
            ts=r['timestamp'];a=r['indicators']['adjclose'][0]['adjclose']
            rows={dt.datetime.fromtimestamp(x,dt.timezone.utc).date().isoformat():float(y) for x,y in zip(ts,a) if y is not None and math.isfinite(y) and y>0}
            rows={d:v for d,v in rows.items() if d<=END}
            if len(rows)<2000:raise ValueError('insufficient history')
            return t,rows,dict(source='YAHOO_'+host.upper()+'_ADJCLOSE_USD',first=min(rows),last=max(rows),count=len(rows),sha256=hashlib.sha256(raw).hexdigest(),errors=errors)
        except Exception as e:errors.append(host+':'+type(e).__name__)
    return t,{},dict(errors=errors)
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
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures=[pool.submit(load_daily_fred,s) for s in SERIES]+[pool.submit(load_etf,t) for t in TICKERS]
        got=[f.result() for f in futures]
    inputs={k:a for k,a,m in got};meta={k:m for k,a,m in got}
    print('NRT_SOURCE_AUDIT',json.dumps(meta),flush=True)
    cr=DailyFlag(inputs['DAAA'],inputs['DBAA'],'credit')
    ra=DailyFlag(inputs['DGS10'],inputs['DGS1MO'],'rate')
    etfs=[dated(inputs[t]) for t in TICKERS]
    avail={'A':bool(inputs['DAAA'] and inputs['DBAA']),'B':all(inputs[s] for s in SERIES),'C':all(inputs[s] for s in SERIES+TICKERS),'BREADTH_ONLY':all(inputs[t] for t in TICKERS)}
    # Original previous month uses final revised history, not proven release vintages.
    # Secondary reference 45d after month-end is a conservative publication-delay proxy.
    report={'source_sha256':digest,'snapshot':SNAP,'last':END,'sessions':len(eq),'source_audit':meta,
            'availability':avail,'method':'EUNL/CSBGE3 actual EUR adjusted closes, original native two sleeves and fee rules, zero cash interest. Macro reference current-vintage prior completed month; alternate reference 45 calendar days after month end. Daily FRED 21 observation averages compared with prior 120 completed monthly averages, min 60; credit change 63 obs; FRED lag 3 or 7 calendar days. US breadth 63 prior sessions; lag 1 calendar day or 3 for sensitivity. No vintage certification; no synthetic fill.','runs':[],'scores':{}}
    def baseline(d,lagged=False):
        if not lagged:return original.regime(d)
        m=d[:7]
        while shift(monthend(m),45)>d:m=monthshift(m,-1)
        return original.regime(monthshift(m,1)+'-01')
    for lagged,delay in [(False,3),(True,3),(True,7)]:
        label=('lag45' if lagged else 'original')+'_daily'+str(delay)
        scores={}
        for mode in ('BASE','A','B','C','BREADTH_ONLY'):
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
                if mode in ('C','BREADTH_ONLY'):
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
        for start in ('2010-01-04','2016-01-04','2020-01-02','2022-01-03'):
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
