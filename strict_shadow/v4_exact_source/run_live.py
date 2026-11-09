#!/usr/bin/env python3
"""Run only the archived Confidence70 core + original tax simulator for PAPER.

As-of provenance is written before a trade, and the next-run portfolio is
accepted only if all prior orders match the committed ledger exactly.
Never submits broker orders and never writes the Cohort Proxy or strict inbox.
"""
from __future__ import annotations
import datetime as dt, hashlib,json,math,os,sys,traceback
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from scipy import optimize
from live_model import HERE,CACHE,LIVE,START_PAPER,ENGINE,INITIAL,SECTOR,latest_vintages,get_adjusted,build_model,evaluate_model
from live_model import v1,v2,t9,inc,frozen,LIVE
if str(LIVE) not in sys.path:sys.path.insert(0,str(LIVE))
from paper import earliest_open_utc

UTC=dt.timezone.utc
NY=ZoneInfo('America/New_York')
FORWARD=HERE/'forward'
STATE=FORWARD/'state.json';STATUS=FORWARD/'latest_status.json'
PREOPEN=FORWARD/'preopen'


def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,ensure_ascii=False,sort_keys=True,default=str,allow_nan=False)+'\n',encoding='utf8')
    tmp.replace(path)


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def serialize(result):
    cols=['scenario','strategy','decision_date','date','action','ticker','shares','reference_price_eur',
          'execution_price_eur','reference_value_eur','fee_eur','slippage_bps_per_side',
          'capital_gains_tax_eur','reason']
    rows=[]
    if result is None:return []
    for d in result.orders.to_dict('records'):
        row={}
        for key in cols:
            x=d.get(key)
            if isinstance(x,pd.Timestamp):x=x.isoformat()
            if isinstance(x,float) and not math.isfinite(x):x=None
            row[key]=x
        rows.append(row)
    return rows


def status_base(now):
    return {'checked_at_utc':now.isoformat(),'engine':'FROZEN_ATLAS_V4_CONFIDENCE70',
            'capital_initial_eur':INITIAL,'paper_only':True,'real_money_orders':0,
            'historical_alpha_code':'VERBATIM_REGION_SLOT_ATLAS_FROM_2012_2025',
            'tax_simulator':'FROZEN_T9_WITH_TERMINAL_LIQUIDATION_DISABLED',
            'universe_market_cap_pit_certified':False,
            'data_universe':'AS_RECORDED_ACWI_ETF_HOLDING_PROXY_2026',
            'frozen_replay_parity':'NOT_YET_PROVEN_FOR_2026_DAILY_INPUTS',
            'proxy_ledger_unchanged':True,'strict_exact_inbox_unchanged':True,
            'status':'PENDING_DATA'}


def positions_asof(ledger,decision):
    positions={}
    for row in ledger:
        if row['ticker']=='SPY' or str(row['date'])[:10]>str(decision):continue
        key=row['ticker']; qty=float(row['shares']);price=float(row['execution_price_eur']);fee=float(row['fee_eur'])
        if row['action']=='BUY':
            amount=qty*price+fee
            if key in positions:
                positions[key].shares+=qty;positions[key].cost_basis_eur+=amount
            else:positions[key]=v2.Position(qty,amount,pd.Timestamp(row['date']))
        else:
            if key not in positions or positions[key].shares+1e-6<qty:
                raise ValueError('Prior paper ledger sells unknown shares: '+key)
            pos=positions[key]
            fraction=qty/pos.shares
            pos.shares-=qty;pos.cost_basis_eur*=1-fraction
            if pos.shares<1e-8:del positions[key]
    return positions


def preview(m,meta,asof,now,old_orders):
    """Record source-hashed model decision BEFORE globally earliest next opening.

    This may be a risk-only date. The simulator decides on actual weekly dates.
    Here we attest only the data snapshot's temporal existence and integrity.
    """
    date=pd.Timestamp(asof)
    # Attest the actual original chooser's current candidates, not every
    # symbol in the 300-name research universe (which would incorrectly put
    # Australian market cutoffs on baskets that contain no Australian stock).
    cfg=m.bundle['cfg']
    date=pd.Timestamp(asof)
    selector,_,_,_=m.callbacks(None)
    scores=m.path_score.loc[date]
    eligible=m.eligible.loc[date]&m.membership.loc[date]
    ranks=scores.where(eligible).rank(ascending=False,method='min')
    selected=selector(date,scores,ranks,eligible,positions_asof(old_orders,date),cfg)
    selected_symbols=[meta['ticker_to_market_symbol'][x] for x in selected]
    if m.bear_floor.get(date,0)>0:selected_symbols.append('SPY')
    if not selected_symbols:selected_symbols=['SPY']
    tomorrow=asof+dt.timedelta(days=1)
    while tomorrow.weekday()>=5:tomorrow+=dt.timedelta(days=1)
    # All 300 plausible symbols matter for earliest global open: even absent a
    # target we cannot assert next-day open fills after Asia has opened.
    earliest=earliest_open_utc(selected_symbols,tomorrow)
    cutoff=earliest-dt.timedelta(minutes=30)
    source_files=list(CACHE.glob('*.csv.gz'))
    source_digest=hashlib.sha256(''.join(sorted(digest(p) for p in source_files)).encode()).hexdigest()
    fingerprint=[(key,round(float(scores.get(key,0)),8)) for key in selected]
    decision_digest=hashlib.sha256(json.dumps({'selected':fingerprint,
          'risk_state':round(float(m.pr_regime.loc[date])*float(m.pr_t8.loc[date]),8)},
          sort_keys=True).encode()).hexdigest()
    record={'decision_session':str(asof),'execution_session':str(tomorrow),
            'decision_model_sha256':decision_digest,
            'produced_at_utc':now.isoformat(),'earliest_open_utc':earliest.isoformat(),
            'cutoff_utc':cutoff.isoformat(),'model_source_sha256':source_digest,
            'roster_sha256':meta['roster_sha256'],'engine_id':ENGINE,
            'market_data_first_observed_before_open':now<cutoff,
            'candidate_count':meta['active_roster_names'],'preopen_selected_symbols':selected_symbols,
            'risk_state':float(m.pr_regime.loc[date])*float(m.pr_t8.loc[date])}
    if now>=cutoff:
        return {'status':'MISSED_GLOBAL_PREOPEN_CUTOFF',**record}
    path=PREOPEN/(str(asof)+'.json')
    if not path.is_file():write_json(path,record)
    return {'status':'ATTESTED_BEFORE_GLOBAL_OPEN',**record}


def order_timecheck(rows,last_orders,model):
    prior=last_orders
    if len(rows)<len(prior) or rows[:len(prior)]!=prior:
        raise ValueError('Append-only paper ledger is not a prefix: data revisions/portfolio drift')
    new=rows[len(prior):]
    for row in new:
        if str(row.get('reason'))=='end_of_test':raise ValueError('Historical terminal liquidation appeared in PAPER')
        dec=str(row.get('decision_date'))[:10]
        execution=str(row.get('date'))[:10]
        p=PREOPEN/(dec+'.json')
        if not p.is_file():raise ValueError('No committed pre-open decision: '+dec)
        evidence=json.loads(p.read_text())
        if not evidence.get('market_data_first_observed_before_open'):
            raise ValueError('Late/unattested decision: '+dec)
        if evidence.get('execution_session')!=execution:
            raise ValueError('Decision/execution session mismatch: '+dec)
        # Fail closed if refreshed or restated history would modify what the
        # model could have scored before the opening auction.
        date=pd.Timestamp(dec)
        cfg=model.bundle['cfg']
        select,_,_,_=model.callbacks(None)
        score=model.path_score.loc[date]
        eligible=model.eligible.loc[date]&model.membership.loc[date]
        ranks=score.where(eligible).rank(ascending=False,method='min')
        chosen=select(date,score,ranks,eligible,positions_asof(last_orders,date),cfg)
        fingerprint=[(key,round(float(score.get(key,0)),8)) for key in chosen]
        calc=hashlib.sha256(json.dumps({'selected':fingerprint,
            'risk_state':round(float(model.pr_regime.loc[date])*float(model.pr_t8.loc[date]),8)},
            sort_keys=True).encode()).hexdigest()
        if calc!=evidence.get('decision_model_sha256'):
            raise ValueError('Original model replay differs from committed pre-open decision '+dec)
    return new


def main(now=None):
    now=now or dt.datetime.now(UTC)
    if now.tzinfo is None:raise ValueError('UTC-aware current time required')
    now=now.astimezone(UTC)
    FORWARD.mkdir(parents=True,exist_ok=True)
    status=status_base(now)
    old=json.loads(STATE.read_text()) if STATE.is_file() else {'orders':[]}
    try:
        # An incomplete US session must never become the decision_date.
        ny=now.astimezone(NY)
        candidate=ny.date() if (ny.hour,ny.minute)>=(17,30) else ny.date()-dt.timedelta(days=1)
        # SPY completed EOD is authoritative for US calendar closure, not a guessed weekday.
        base,fail=get_adjusted(['SPY'],candidate,max_workers=1)
        if 'SPY' not in base:raise ValueError('US EOD benchmark unavailable: '+str(fail))
        spy=base['SPY'];dates=[d.date() for d in spy.index if d.date()<=candidate]
        if not dates:raise ValueError('No completed benchmark history')
        asof=max(dates)
        if (candidate-asof).days>4:raise ValueError('Stale SPY benchmark')
        vintage=latest_vintages(asof)[-1]
        symbols=[str(x['symbol']).upper() for x in vintage[3]+vintage[4]]
        required=set(symbols)|set(SECTOR.values())|{'SPY','EURUSD=X'}
        required|={'EUR'+str(x['currency']).upper()+'=X' for x in vintage[3]+vintage[4] if str(x['currency']).upper()!='EUR'}
        frames,fail=get_adjusted(required,asof,max_workers=8)
        # Every chosen 2026 constituent is required; silently dropping a leader
        # would change H4 selection and misstate source equivalence.
        if fail:
            first=list(sorted(fail.items()))[:10]
            raise ValueError(f'Adjusted dataset incomplete ({len(fail)}/{len(required)}): '+str(first))
        m,meta=build_model(vintage,asof,frames)
        status.update({'decision_session':str(asof),'source':meta})
        evidence=preview(m,meta,asof,now,old.get('orders',[]))
        status['next_preopen']=evidence
        result,operation=evaluate_model(m,meta)
        status['model_run']=operation
        if result is None:
            status['status']='WAITING_FIRST_ACTUAL_EXECUTION_SESSION'
        else:
            rows=serialize(result)
            fresh=order_timecheck(rows,old.get('orders',[]),m)
            status.update({'status':'PAPER_LEDGER_VERIFIED','orders_cumulative':len(rows),
                           'new_orders':len(fresh),'nav_eur':round(float(result.equity.iloc[-1]),2),
                           'max_drawdown_pct':round(float((result.equity/result.equity.cummax()-1).min()*100),3),
                           'last_valuation_session':str(result.equity.index[-1].date()),
                           'tax_eur_total':round(float(result.total_tax_eur),2),
                           'fees_eur_total':round(float(result.total_fees_eur),2),
                           'estimated_slippage_eur':round(float(result.estimated_slippage_eur),2),
                           'model_state_input_timestamp_validated':True})
            save={'schema_version':1,'engine_id':ENGINE,'portfolio':'EXACT_V4_ORIGINAL_SOURCE_PAPER',
                  'initial_cash_eur':INITIAL,'orders':rows,'latest_nav_eur':status['nav_eur'],
                  'last_valuation_session':status['last_valuation_session'],
                  'cash_and_positions':'computed_by_frozen_simulator_not_manual_broker',
                  'real_money_authorized':False}
            write_json(STATE,save)
            daily=FORWARD/'equity.csv'
            data='date,net_liquidation_nav_eur\n'+'\n'.join(
                f'{day.date()},{val:.4f}' for day,val in result.equity.items())+'\n'
            daily.write_text(data,encoding='utf8')
            cols=result.orders.columns.tolist()
            if len(cols):result.orders.to_csv(FORWARD/'orders.csv',index=False)
    except Exception as exc:
        status.update({'status':'PENDING_EXACT_DATA_OR_PARITY','reason':str(exc)[:2000],
                       'no_new_official_orders':True})
        if os.environ.get('ATLAS_EXACT_DEBUG')=='1':traceback.print_exc()
    write_json(STATUS,status)
    print(json.dumps({k:status.get(k) for k in ('status','reason','orders_cumulative','new_orders','decision_session','nav_eur')},ensure_ascii=False))
    return status

if __name__=='__main__':main()
