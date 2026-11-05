#!/usr/bin/env python3
import argparse,json,hashlib
from pathlib import Path
import pandas as pd
p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--data',default='downloaded');a=p.parse_args()
m=pd.read_csv(a.input);root=Path(a.data);rows=[]
for sym,g in m.groupby('primary_symbol'):
    f=root/'prices'/(str(sym).replace('/','_').replace('=','__EQ__')+'.csv')
    r={'symbol':sym,'membership_years':','.join(map(str,sorted(g.effective_year.unique()))),'status':'MISSING','rows':0,'sha256':''}
    if f.exists():
        try:
            x=pd.read_csv(f);x['date']=pd.to_datetime(x.date,errors='coerce');x['adj_close']=pd.to_numeric(x.adj_close,errors='coerce');x=x.dropna(subset=['date','adj_close'])
            r.update(status='AVAILABLE_UNVERIFIED_PIT' if len(x) else 'EMPTY',rows=len(x),first_date=str(x.date.min()),last_date=str(x.date.max()),sha256=hashlib.sha256(f.read_bytes()).hexdigest())
        except Exception as e:r['status']='INVALID: '+str(e)[:100]
    rows.append(r)
(root/'audit').mkdir(parents=True,exist_ok=True);pd.DataFrame(rows).to_csv(root/'audit/price_coverage.csv',index=False)
print(json.dumps({'symbols_total':len(rows),'symbols_with_data':sum(r['rows']>0 for r in rows),'historical_usa100_pit_present':False,'historical_fx_validated':False,'confidence70_backtest_ready':False},indent=2))
