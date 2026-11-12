#!/usr/bin/env python3
"""Audit 2026 performance attribution from the UNMODIFIED V2.3 original ledger."""
import csv,json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
D=ROOT/'reports_modern'
NAV=D/'atlas_original_v23_nav_daily.csv'
ORD=D/'atlas_original_v23_orders.csv'
OUT=D/'diagnostics_2026.json'
def rows(file):
    with file.open(newline='') as f:return list(csv.DictReader(f))
def floatv(x,key):return float(x[key])
def maximum_drawdown(values):
    peak=values[0];worst=0
    for x in values:
        peak=max(peak,x);worst=min(worst,x/peak-1)
    return worst
def yearly(daily,year):
    previous=[x for x in daily if x['date']<f'{year}-01-01']
    current=[x for x in daily if x['date'].startswith(str(year))]
    if not previous or not current:raise ValueError('Need previous and current year')
    last_prev=previous[-1];last=current[-1]
    a=floatv(last,'twr_index')/floatv(last_prev,'twr_index')-1
    market=floatv(last,'market_proxy')/floatv(last_prev,'market_proxy')-1
    period_values=[floatv(last_prev,'twr_index')]+[floatv(q,'twr_index') for q in current]
    state={}
    for field in ('core_in','satellite_in'):
        state[field+'_out_sessions']=sum(int(q[field])==0 for q in current)
        intervals=[];start=None
        for q in current:
            if int(q[field])==0 and start is None:start=q['date']
            if int(q[field])==1 and start is not None:
                intervals.append({'start':start,'end_exclusive':q['date']})
                start=None
        if start:intervals.append({'start':start,'end_inclusive':current[-1]['date']})
        state[field+'_off_periods']=intervals
    return {'begin_snapshot':last_prev['date'],'end_session':last['date'],
            'atlas_twr_return_pct':round(a*100,5),
            'market_ff_proxy_return_pct':round(market*100,5),
            'atlas_minus_market_pct_points':round((a-market)*100,5),
            'atlas_intrayear_drawdown_pct':round(maximum_drawdown(period_values)*100,5),
            'atlas_marked_nav_start_eur':round(floatv(last_prev,'nav_eur'),2),
            'atlas_marked_nav_end_eur':round(floatv(last,'nav_eur'),2),
            'market_index_start':round(floatv(last_prev,'market_proxy'),4),
            'market_index_end':round(floatv(last,'market_proxy'),4),
            'state':state}
def main():
    daily=rows(NAV);orders=rows(ORD)
    summary=json.loads((D/'summary.json').read_text())
    result={'source':'Atlas V2.3 original frozen state machine, existing 2016-2026 daily NAV and order ledgers',
            'cutoff':'2026-11-11',
            'research_not_live':True,
            'performance_years':{str(y):yearly(daily,y) for y in (2024,2025,2026)},
            'order_years':{}}
    for y in (2024,2025,2026):
        yearly_orders=[q for q in orders if q['date'].startswith(str(y))]
        result['order_years'][str(y)]={
            'count':len(yearly_orders),
            'sells':sum(q['side']=='SELL' for q in yearly_orders),
            'strategic_orders':[{
                'date':q['date'],'kind':q['kind'],'note':q['note'],
                'gross_eur':round(float(q['gross_eur']),2),
                'tax_eur':round(float(q['tax_eur']),2),
                'slippage_eur':round(float(q['slippage_eur']),2),
            } for q in yearly_orders if not q['kind'].startswith('PAC_')],
            'tax_total_eur':round(sum(float(q['tax_eur']) for q in yearly_orders),2),
        }
    result['data_quality']={
        'official_ff_asof':summary['coverage']['official'].get('max_session'),
        'spy_proxy_start':summary['coverage'].get('proxy_start'),
        'unknown_2026_macro_months':summary['scenarios']['carry']['unknown_macro_months'],
        '2026_stress_gate_events':summary['scenarios']['carry']['stress_events_2026']
    }
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False))
    print(json.dumps(result,indent=2,ensure_ascii=False))
if __name__=='__main__':main()
