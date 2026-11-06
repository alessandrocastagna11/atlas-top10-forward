#!/usr/bin/env python3
"""Atlas Top 10 v0.7 OFFLINE launch replay from persisted quotes to 2026-11-05.

DIAGNOSTIC ONLY. Does not write official forward strategies or amend their ledgers.
Do not mistake historical fills for actual observed forward trades.
"""
import copy,datetime as dt,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import engine as E

OUT=Path(__file__).resolve().parent/'launch_replay_2026_11_05'
CUTOFF='2026-11-05'
DATES=('2026-11-04','2026-11-05')
STRATEGIES=('MA10_Official','MA6_MA12_Challenger')

def fresh_history(full,day):
    # Reconstruct each decision with only quotes/corporate actions dated <= that day.
    out={}
    for symbol,src in full.items():
        rec=copy.deepcopy(src)
        for key in ('bars','dividends','splits'):
            rec[key]={d:v for d,v in src.get(key,{}).items() if d<=day}
        out[symbol]=rec
    return out

def build():
    raw=json.loads((ROOT/'market_data/latest_snapshot.json').read_text())
    full=raw['data']
    if raw.get('asof')!=CUTOFF:
        raise ValueError('Frozen last-asof 2026-11-05 snapshot required, got '+str(raw.get('asof')))
    digest=hashlib.sha256(json.dumps(full,sort_keys=True).encode()).hexdigest()
    audit={'kind':'RETROSPECTIVE_2_SESSION_LAUNCH_REPLAY_NOT_FORWARD',
           'cutoff':CUTOFF,'data_source':raw.get('source'),
           'data_digest_sha256':digest,'retrospective_signal_only':True,
           'replay_with_future_knowledge_beyond_asof':False,'strategies':{}}
    for name in STRATEGIES:
        folder=ROOT/'strategies'/name
        cfg=json.loads((folder/'config.json').read_text())
        state=json.loads((folder/'state.json').read_text())
        # A re-run uses the frozen uninitialized 3 November seed only.
        if state['started_at']!='2026-11-03' or state['initialized'] or state['last_update']!='2026-11-03':
            raise ValueError('Do not replay from mutated paper state: '+name)
        result={'status':'UNSET','dates':[],'orders':[],'blocked':[],'initial_capital_eur':10000.0}
        for day in DATES:
            data=fresh_history(full,day)
            needed=[x['symbol'] for x in cfg['basket']]+[cfg['benchmark']['symbol'],'EURUSD=X','EURSAR=X']
            missing=[s for s in needed if day not in data.get(s,{}).get('bars',{})]
            if missing:
                result['blocked'].append({'date':day,'reason':'MISSING_EXACT_PRICE','symbols':missing})
                continue
            before=copy.deepcopy(state)
            try:
                orders,nav=E.step(state,cfg,data,day)
                if nav:
                    state['nav_history'].append(nav)
                    state['order_history'].extend(orders)
                result['dates'].append({'date':day,'nav_eur':nav['portfolio_nav_eur'] if nav else None,
                     'benchmark_nav_eur':nav['benchmark_nav_eur'] if nav else None,
                     'position_count':len(state['positions']),
                     'buys':sum(x['side']=='BUY' and x['symbol']!=cfg['benchmark']['symbol'] for x in orders),
                     'benchmark_orders':sum(x['symbol']==cfg['benchmark']['symbol'] for x in orders),
                     'status':'SIMULATED' if nav else 'NO_CHANGE'})
                result['orders'].extend(orders)
            except (RuntimeError,ValueError,KeyError) as e:
                state=before
                result['blocked'].append({'date':day,'reason':str(e)[:300]})
        navs=[x for x in result['dates'] if x['nav_eur'] is not None]
        result['status']='COMPLETE_DIAGNOSTIC' if len(navs)==len(DATES) and not result['blocked'] else 'PARTIAL_DIAGNOSTIC'
        result['portfolio_nav_eur']=navs[-1]['nav_eur'] if navs else 10000.
        result['benchmark_nav_eur']=navs[-1]['benchmark_nav_eur'] if navs else 10000.
        result['two_session_return_pct']=round((result['portfolio_nav_eur']/10000.-1)*100,5)
        result['benchmark_two_session_return_pct']=round((result['benchmark_nav_eur']/10000.-1)*100,5)
        result['nonbenchmark_orders']=sum(x['symbol']!=cfg['benchmark']['symbol'] for x in result['orders'])
        result['fees_eur']=round(sum(x['commission_eur'] for x in result['orders'] if x['symbol']!=cfg['benchmark']['symbol']),2)
        result['end_cash_eur']=round(state['cash_eur'],2)
        audit['strategies'][name]=result
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'result.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False)+'\n')
    md=['# Atlas Top 10 — test retrospettivo di avvio al 5 novembre 2026','',
       '**NON È IL FORWARD UFFICIALE**. Usa lo snapshot scaricato dopo le sedute e ricrea',
       'ipotetici acquisti al CLOSE 4 novembre, senza affermare che tali esecuzioni',
       'sarebbero state realizzabili esattamente a quel prezzo. Il ledger ufficiale resta immutato.','',
       'L\'universo è stato congelato il 3 novembre: la simulazione metodologicamente',
       'ammissibile con il paniere v0.7 attuale riguarda il 4–5 novembre, non il 2025.','',
       '| Strategia | Stato | NAV finale € | Benchmark finale € | P/L % | Ordini azionari | Commissioni € |',
       '|---|---|---:|---:|---:|---:|---:|']
    for k,x in audit['strategies'].items():
        md.append(f"| {k} | {x['status']} | {x['portfolio_nav_eur']:,.2f} | {x['benchmark_nav_eur']:,.2f} | {x['two_session_return_pct']:+.3f}% | {x['nonbenchmark_orders']} | {x['fees_eur']:.2f} |")
        if x['blocked']:md.append(f"\n- {k}: blocchi "+json.dumps(x['blocked'],ensure_ascii=False))
    md+=['','Fonte: `market_data/latest_snapshot.json`; hash sha256 dei dati: `'+digest+'`.','',
        'IUSQ.DE del 5 novembre proviene dal singolo snapshot Yahoo: contrasto con altre fonti',
        'non risolto. Esecuzioni storiche dei soli prezzi di CLOSE, non prove di eseguibilità.','',
        'Un backtest pluriennale di v0.8 fino al 2025 è separato; non è stato esteso',
        'al 2026 con falsi universi retroattivi o dati futuri.']
    (OUT/'REPORT.md').write_text('\n'.join(md)+'\n')
    print(json.dumps({k:{p:v[p] for p in ('status','portfolio_nav_eur','benchmark_nav_eur','nonbenchmark_orders','blocked')} for k,v in audit['strategies'].items()},ensure_ascii=False))
    return audit

if __name__=='__main__':build()
