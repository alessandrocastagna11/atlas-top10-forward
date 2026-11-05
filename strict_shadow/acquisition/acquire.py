#!/usr/bin/env python3
"""Restartable historical Yahoo research acquisition; not a PIT backtest."""
import argparse,datetime as dt,hashlib,json,time
from pathlib import Path
from urllib.parse import quote
import pandas as pd
import requests
FX=['EURUSD=X','JPYUSD=X','GBPUSD=X','CHFUSD=X','CADUSD=X','AUDUSD=X','DKKUSD=X','HKDUSD=X','INRUSD=X','KRWUSD=X','SARUSD=X','SEKUSD=X','SGDUSD=X','TWDUSD=X','NOKUSD=X','BRLUSD=X','ZARUSD=X']
def get(s,sym,start,end):
    t=lambda d:int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp())
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+quote(sym,safe='')
    for attempt in range(4):
        try:
            r=s.get(url,params={'period1':t(start),'period2':t(end),'interval':'1d','events':'div,splits','includeAdjustedClose':'true'},timeout=40)
            r.raise_for_status(); z=r.json()['chart']; x=(z.get('result') or [None])[0]
            if x is None:raise ValueError(str(z.get('error')))
            q=x['indicators']['quote'][0]; adj=(x['indicators'].get('adjclose') or [{}])[0].get('adjclose') or q['close']
            rows=[]
            for i,ts in enumerate(x.get('timestamp') or []):
                if i>=len(adj) or adj[i] is None:continue
                rows.append({'date':dt.datetime.fromtimestamp(ts,dt.timezone.utc).date().isoformat(),'open':q['open'][i],'high':q['high'][i],'low':q['low'][i],'close':q['close'][i],'adj_close':adj[i],'volume':q['volume'][i],'currency':x.get('meta',{}).get('currency',''),'source':'yahoo_chart_unofficial'})
            if not rows:raise ValueError('empty series')
            return pd.DataFrame(rows).drop_duplicates('date').sort_values('date')
        except Exception:
            if attempt==3:raise
            time.sleep(2**attempt*2)
def main():
    p=argparse.ArgumentParser()
    for key,default in [('input','strict_shadow/acquisition/input/global200_symbols_recovered.csv'),('out','downloaded'),('start','2010-01-01'),('end','2025-12-11')]:p.add_argument('--'+key,default=default)
    a=p.parse_args(); m=pd.read_csv(a.input)
    if 'symbol' not in m.columns:raise ValueError('missing symbol column')
    if len(m)!=288:raise ValueError('unexpected recovered symbol count; aborting')
    root=Path(a.out)
    for d in ['prices','fx','audit']:(root/d).mkdir(parents=True,exist_ok=True)
    jobs=[('prices',x) for x in sorted(set(m.symbol.dropna().astype(str)))]+[('fx',x) for x in FX]
    s=requests.Session();s.headers.update({'User-Agent':'Mozilla/5.0'})
    with (root/'audit/acquisition_log.jsonl').open('a') as log:
        for kind,sym in jobs:
            f=root/kind/(sym.replace('/','_').replace('=','__EQ__')+'.csv')
            if f.exists() and f.stat().st_size>100:continue
            entry={'kind':kind,'symbol':sym,'attempted_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            try:
                x=get(s,sym,a.start,a.end);x.to_csv(f,index=False)
                entry.update(status='OK',rows=len(x),first_date=str(x.date.min()),last_date=str(x.date.max()),sha256=hashlib.sha256(f.read_bytes()).hexdigest())
            except Exception as e:entry.update(status='ERROR',reason=str(e)[:400])
            log.write(json.dumps(entry)+'\n');log.flush()
            print(kind,sym,entry['status'],flush=True);time.sleep(0.7)
if __name__=='__main__':main()
