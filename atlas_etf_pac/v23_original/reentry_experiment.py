#!/usr/bin/env python3
"""Compare original V2.3 against lower first-satellite rebound thresholds.
ORIGINAL V2.3 engine is imported byte-for-byte, preserving all exit logic,
core stress gates, costs, and annual stamp/tax proxies. No portfolio mutations.
"""
from __future__ import annotations
import argparse, json, math, copy, datetime as dt, sys
from pathlib import Path
import run_extended as v23

HERE=Path(__file__).resolve().parent

def run():
    ap=argparse.ArgumentParser()
    ap.add_argument('--end',default='2025-12-31')
    ap.add_argument('--starts',default='1926-07-01,2016-01-04')
    ap.add_argument('--thresholds',default='0.03,0.025,0.02,0.015')
    ap.add_argument('--online',action='store_true')
    ap.add_argument('--out',default=str(HERE/'reentry_comparison.json'))
    args=ap.parse_args()
    engine_path,engine_sha=v23.check_reference()
    v=v23.load_original_engine(engine_path)
    v.xirr=v23.stable_xirr_factory(v)
    frozen_cfg=json.loads((v23.EXTRACT/'config.json').read_text())
    pinned=json.loads((v23.EXTRACT/'data/stress_scores_precomputed.json').read_text())
    y,m=map(int,pinned['start'].split('-'));scores={}
    for val in pinned['scores']:
        scores[f'{y:04}-{m:02}']=int(val);m+=1
        if m==13:m=1;y+=1
    rows,coverage=v23.build_extension(v,args.end,args.online)
    v.load_ff=lambda start,end:[dict(q) for q in rows if start<=q['date']<=end]
    output=Path(args.out).parent
    output.mkdir(parents=True,exist_ok=True)
    all_runs={}
    thresholds=[float(q) for q in args.thresholds.split(',')]
    for start in args.starts.split(','):
        all_runs[start]={}
        for threshold in thresholds:
            cfg=copy.deepcopy(frozen_cfg)
            cfg['start_date']=start;cfg['end_date']=args.end
            cfg['satellite_rebound']=threshold
            v23.stress_override(v,'carry',scores)
            sim=v.Simulator(cfg,output)
            z,nav,flows=sim.run()
            benchmark=v23.passive_bh(v,sim.rows,sim.price,cfg)
            nav2026=[r for r in nav if r['date'].startswith('2026-')]
            prev=next((r for r in reversed(nav) if r['date']<='2025-12-31'),None)
            ret2026=(nav2026[-1]['twr_index']/prev['twr_index']-1) if (prev and nav2026) else None
            def segment_stats(start,end):
                part=[r for r in nav if start<=r['date']<=end]
                if len(part)<2:return None
                tw0=part[0]['twr_index']
                twlast=part[-1]['twr_index']
                years=v.years_between(part[0]['date'],part[-1]['date'])
                return {'cagr_pct':100*((twlast/tw0)**(1/years)-1),
                        'maxdd_pct':100*v.max_dd([q['twr_index']/tw0 for q in part]),
                        'first':part[0]['date'],'last':part[-1]['date']}
            segments={label:segment_stats(a,b) for label,a,b in (
                ('1926_1969','1926-07-01','1969-12-31'),
                ('1970_1999','1970-01-01','1999-12-31'),
                ('2000_2025','2000-01-01','2025-12-31'),
                ('2016_2025','2016-01-04','2025-12-31'),
                ('2026_ytd','2025-12-31','2026-11-11'),
            )}
            data={
                'start':start,'end':args.end,'satellite_rebound':threshold,
                'cagr_pct':z['twr_cagr_net']*100,'maxdd_pct':z['max_drawdown']*100,
                'xirr_pct':z['xirr']*100,'final_liquidated_eur':z['final_liquidated_eur'],
                'orders_total':z['orders_total'],'orders_strategy':z['orders_strategy'],
                'realized_taxes_eur':z['realized_taxes_eur'],
                'fees_eur':z['fixed_fees_eur'],'slippage_eur':z['slippage_proxy_eur'],
                'stamp_eur':z['stamp_proxy_eur'],'2026_return_pct':ret2026*100 if ret2026 is not None else None,
                'subperiods':segments,
                '2026_satellite_out_sessions':sum(int(q['satellite_in'])==0 for q in nav2026),
                '2026_sales':[q for q in sim.events if q['date']>='2026-01-01' and q['kind']=='STRATEGY_EQ_SELL'],
                '2026_reentries':[q for q in sim.events if q['date']>='2026-01-01' and q['kind']=='STRATEGY_EQ_BUY'],
                'benchmark':{k:benchmark[k] for k in ['twr_cagr_net','max_drawdown','xirr','final_liquidated_eur']}
            }
            key=f'{threshold:.3f}'
            all_runs[start][key]=data
            print(json.dumps({'start':start,'cutoff':args.end,'t':threshold,'cagr':round(data['cagr_pct'],4),'DD':round(data['maxdd_pct'],4),'XIRR':round(data['xirr_pct'],4),'orders':z['orders_total'],'ytd2026':data['2026_return_pct']},ensure_ascii=False),flush=True)
    results={'engine_sha256':engine_sha,'coverage':coverage,'base_is_original':True,'rules_changed':'first satellite rebound only, config satellite_rebound from 0.03 to 0.025/0.02/0.015','results':all_runs}
    Path(args.out).write_text(json.dumps(results,ensure_ascii=False,indent=2,allow_nan=False))

if __name__=='__main__':run()