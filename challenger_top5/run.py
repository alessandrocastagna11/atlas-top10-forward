#!/usr/bin/env python3
"""Prospective PAPER challenger; original source/roster is read-only.
Replays only previously published, pre-open-attested decisions. Never sends
broker orders. Rejects historical ledger revisions rather than backfilling."""
from __future__ import annotations
import datetime as dt
import json, math, sys, traceback, subprocess
from pathlib import Path
from dataclasses import replace
from types import MethodType
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'confidence_source'/'strict_shadow'/'v4_exact_source'
sys.path.insert(0,str(SOURCE))
import live_model as lm
import run_live as rl
from shared_inputs import load as load_shared_inputs

START=pd.Timestamp('2026-11-18')
INITIAL=10000.
ENGINE='ATLAS_TOP5_ALWAYS_RANK_MOM10_V1'
UTC=dt.timezone.utc
STATE=ROOT/'state.json'
STATUS=ROOT/'latest_status.json'
SIGNALS=ROOT/'signals'

def read(p, default=None):
    return json.loads(p.read_text()) if p.exists() else default
def write(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False,default=str)+'\n')
    tmp.replace(p)

def score_mom10(base,path,close,eligible,calibration):
    # Literal research score: recover original .20 path term, then add
    # percentile of pct_change(63).shift(21), not pct_change(42).
    quality=((path-base)/.20).clip(-1,1)
    mom=close.pct_change(63,fill_method=None).shift(21)
    percent=mom.where(eligible).rank(axis=1,pct=True)
    return (base+.20*quality+.10*(2*percent-1)).clip(-3,3).where(calibration)

def setup(m,vintages):
    calibration=m.membership.copy()
    historical=calibration.index<pd.Timestamp(vintages[0][0])
    first=[str(r['symbol']).upper() for r in vintages[0][3]]
    first+=['G::'+str(r['symbol']).upper() for r in vintages[0][4]]
    calibration.loc[historical,first]=True
    # Before the first observed vintage there is no certified eligibility.
    # First-cohort historical calibration is explicitly a proxy only; no trades.
    rankmask=m.eligible.copy()
    rankmask.loc[historical]=calibration.loc[historical]&m.base_score.loc[historical].notna()
    m.path_score=score_mom10(m.base_score,m.path_score,m.features.close_eur,rankmask,calibration)
    m.expected_alpha,_=m.v4.build_expected_alpha(m.path_score,m.features.close_eur.where(calibration),str(START.date()-dt.timedelta(days=3)))
    # In the historical RANK_MOM10 replay, 10-slot regional schedule was scaled
    # proportionally to 5. Retain original incumbent/correlation logic.
    original=type(m).row_slots_at
    def slots(self,date,max_positions):
        n10,s,_=original(self,date,10)
        n=int(np.clip(round(n10*max_positions/10),0,max_positions))
        return n,s,n/max_positions
    m.row_slots_at=MethodType(slots,m)
    cfg=replace(m.bundle['cfg'],target_volatility=5.0,max_positions=5,
        maximum_position_weight=.20,maximum_sector_weight=.40,
        minimum_holding_calendar_days=35,decision_interval_weeks=5,
        minimum_trade_eur=500,rebalance_band_weight=.06,
        default_slippage_bps_per_side=lm.inc.SLIPPAGE_BPS)
    m.bundle['cfg']=cfg
    return cfg

def load_model():
    source_status=read(SOURCE/'forward'/'latest_status.json',{})
    if source_status.get('signal_engine_status')!='SIGNAL_READY':
        raise ValueError('Confidence70 signal not ready')
    asof=dt.date.fromisoformat(source_status['decision_session'])
    if (dt.datetime.now(UTC).date()-asof).days>4:
        raise ValueError('Confidence70 source too stale')
    source_meta=source_status.get('source',{})
    if source_status.get('shared_input_status')!='PUBLISHED_LOSSLESS':
        raise ValueError('No verified common input snapshot from Confidence70')
    descriptor=source_status.get('shared_input')
    vintages,frames,cash_rate=load_shared_inputs(SOURCE/'forward',descriptor,asof,source_meta.get('roster_sha256'))
    lm.START_PAPER=START
    model,meta=lm.build_model(vintages,asof,frames,shared_cash_rate=cash_rate)
    source_meta=source_status.get('source',{})
    if not source_meta.get('roster_sha256') or meta['roster_sha256']!=source_meta['roster_sha256'] or meta['roster_recorded_date']!=source_meta.get('roster_recorded_date'):
        raise ValueError('Roster hash/date differs from original; fail closed')
    if set(meta['ticker_to_market_symbol'])!=set(source_meta.get('ticker_to_market_symbol',{})):
        raise ValueError('Historical universe symbol set differs from original')
    if meta['active_roster_names']!=300 or int(model.membership.loc[pd.Timestamp(asof)].sum())!=300:
        raise ValueError('Expected exactly 300 active names')
    for key in ('ticker_price_dates','fx_currency_source_sessions','fx_units_per_eur_asof','dated_vintages_applied'):
        if meta.get(key)!=source_meta.get(key):
            raise ValueError('Shared model input provenance mismatch: '+key)
    meta['shared_input']=descriptor
    meta['input_mode']='SAME_LOSSLESS_SNAPSHOT_NO_REFETCH'
    cfg=setup(model,vintages)
    return model,meta,cfg,pd.Timestamp(asof),source_status

def choose_targets(m,cfg,date,positions):
    choose,alloc,_,_=m.callbacks(None)
    score=m.path_score.loc[date]
    eligible=(m.eligible.loc[date]&m.membership.loc[date]).fillna(False)
    ranks=score.where(eligible).rank(ascending=False,method='min')
    names=choose(date,score,ranks,eligible,positions,cfg)
    weights=pd.Series(dtype=float)
    if names:
        risk,relative,cov=lm.v2.estimate_risky_volatility(date,names,m.features.close_eur,cfg.covariance_lookback)
        if np.isfinite(risk) and risk>0:
            weights=alloc(relative,min(1.,cfg.target_volatility/risk),cfg,decision_date=date)
    return [{'ticker':t,'score':round(float(score[t]),8) if pd.notna(score[t]) else None,
             'rank':float(ranks[t]) if pd.notna(ranks[t]) else None,
             'target_weight':round(float(weights.get(t,0.)),8),
             'sector':lm.v1.TICKER_TO_SECTOR[t]} for t in names]

def same_targets(left,right):
    if [x['ticker'] for x in left]!=[x['ticker'] for x in right]:return False
    return all(abs(x['target_weight']-y['target_weight'])<=1e-5 for x,y in zip(left,right))

def replay(m,cfg,asof,old,signals):
    if asof<START:return None,[],[]
    original_map=lm.v1._weekly_execution_map
    allowed={}
    missed=[]
    # Never invent historical orders. A missed decision is skipped and the
    # five-week cadence counts only prospectively attested opportunities.
    for execution,decision in original_map(m.features.close_eur.index,START).items():
        key=str(decision.date()); proof=signals.get(key)
        if not proof or not proof.get('preopen_eligible'):
            missed.append({'decision':key,'execution':str(execution.date()),'reason':'NO_PREOPEN_PROOF'})
            continue
        if str(execution.date())!=proof['execution_session']:
            missed.append({'decision':key,'execution':str(execution.date()),'reason':'EXCHANGE_SESSION_MISMATCH'})
            continue
        allowed[execution]=decision
    lm.v1._weekly_execution_map=lambda index,start:allowed.copy()
    choose,alloc,_,_=m.callbacks(None)
    def verified_choose(date,score,rank,eligible,positions,config):
        proof=signals[str(date.date())]
        names=choose(date,score,rank,eligible,positions,config)
        if names!=[x['ticker'] for x in proof['targets']]:
            raise ValueError('Revised selection at previously attested decision '+str(date.date()))
        execution=pd.Timestamp(proof['execution_session'])
        for t in set(names)|set(positions):
            if pd.isna(m.raw.open_eur.at[execution,t]) or float(m.raw.open_eur.at[execution,t])<=0:
                raise ValueError('Missing actual execution-session open: '+t)
        return names
    def verified_alloc(relative,exposure,config,decision_date=None):
        weights=alloc(relative,exposure,config,decision_date=decision_date)
        proof=signals[str(decision_date.date())]['targets']
        targets=[{'ticker':t,'target_weight':float(weights.get(t,0.))} for t in weights.index]
        expected={x['ticker']:x['target_weight'] for x in proof}
        if set(expected)!=set(weights.index) or any(abs(float(weights[t])-expected[t])>1e-5 for t in weights.index):
            raise ValueError('Revised attested target weights '+str(decision_date.date()))
        return weights
    try:
        result=lm.live_sim.simulate_tax_aware(m.features,m.path_score,m.eligible,m.membership,
            pd.Series(1.,index=m.features.close_eur.index),m.cash_rate,None,cfg,m.raw,
            str(START.date()),INITIAL,ENGINE,m.tax,weekly_risk_overlay=None,bear_floor=None,
            execution_policy=lm.t9.ExecutionPolicy(name='coreband'+str(lm.inc.CORE_BAND),core_risk_band_weight=lm.inc.CORE_BAND),
            cash_reserve_fraction=.01,name_selector=verified_choose,target_weight_builder=verified_alloc,
            liquidate_on_final_session=False)
    finally:lm.v1._weekly_execution_map=original_map
    rows=rl.serialize(result)
    previous=old.get('orders',[])
    if len(rows)<len(previous):raise ValueError('Replay removed committed orders')
    for a,b in zip(rows,previous):
        if set(a)!=set(b):raise ValueError('Order schema revised')
        for k in a:
            if isinstance(a[k],(int,float)) and isinstance(b[k],(int,float)):
                if not math.isclose(a[k],b[k],rel_tol=1e-8,abs_tol=1e-5):raise ValueError('Prior paper order revised: '+k)
            elif a[k]!=b[k]:raise ValueError('Prior paper order revised: '+k)
    return result,previous+rows[len(previous):],missed

def main():
    now=dt.datetime.now(UTC)
    old=read(STATE,{'engine':ENGINE,'capital_initial_eur':INITIAL,'orders':[],'positions':{},'cash_eur':INITIAL,'nav_net_liquidation_eur':INITIAL})
    status={'engine':ENGINE,'checked_at_utc':now.isoformat(),'paper_only':True,'real_money_orders':0,'status':'PENDING','historical_pit_certified':False,'forward_parity_certified':False,'execution_model':'completed actual local-session bar open with indicative previous-day EUR FX; simulated, not broker fills'}
    try:
        m,meta,cfg,asof,original=load_model()
        status.update({'decision_session':str(asof.date()),'source':meta,'universe_same_hash':True,'shared_input_sha256':meta['shared_input']['sha256'],'shared_input_verified':True,'same_price_dates_and_fx':True,'universe_kind':meta['universe_kind'],'original_nav_eur':original.get('nav_eur')})
        signals={p.stem:read(p) for p in SIGNALS.glob('*.json')}
        result,orders,missed=replay(m,cfg,asof,old,signals)
        if result is None:
            state=old.copy()
        else:
            pos=result.paper_positions
            mark=m.raw.close_eur.loc[asof]
            if any(t not in mark or not np.isfinite(mark[t]) or mark[t]<=0 for t in pos):raise ValueError('Owned title missing mark')
            for t,p in pos.items():
                p['market_value_eur']=float(p['shares']*mark[t])
            gross=float(result.paper_cash_eur+sum(p['market_value_eur'] for p in pos.values()))
            state={'engine':ENGINE,'capital_initial_eur':INITIAL,'orders':orders,'positions':pos,'cash_eur':float(result.paper_cash_eur),
                   'nav_eur':gross,'nav_net_liquidation_eur':float(result.equity.iloc[-1]),'exposure':float(result.exposure.iloc[-1]),
                   'valuation_session':str(asof.date()),'fees_eur':float(result.total_fees_eur),'slippage_eur':float(result.estimated_slippage_eur),
                   'capital_gains_tax_eur':float(result.capital_gains_tax_eur),'tax_eur':float(result.capital_gains_tax_eur+result.foreign_dividend_tax_eur+result.italian_dividend_tax_eur+result.interest_tax_eur+result.stamp_duty_eur),
                   'blocked_sales':int(result.blocked_sell_count),
                   'equity_history':{str(d.date()):float(x) for d,x in result.equity.items()}}
        positions=rl.positions_asof(state['orders'],asof.date())
        targets=choose_targets(m,cfg,asof,positions)
        # Publish only for the next US weekday. Every selected/owned exchange
        # must have an opening on the execution day and not yet have opened.
        execution=asof.date()+dt.timedelta(days=1)
        while execution.weekday()>=5:execution+=dt.timedelta(days=1)
        opens={}
        for t in set(positions)|{x['ticker'] for x in targets}:
            market=meta['ticker_to_market_symbol'].get(t,t)
            day,opening=rl.symbol_next_open(market,execution)
            opens[t]={'date':str(day),'utc':opening.isoformat()}
        preopen=bool(targets) and all(x['date']==str(execution) and now<dt.datetime.fromisoformat(x['utc']) for x in opens.values())
        signal={'engine':ENGINE,'decision_session':str(asof.date()),'first_recorded_at_utc':now.isoformat(),
                'execution_session':str(execution),'preopen_eligible':preopen,'targets':targets,'candidate_exchange_opens':opens,
                'roster_sha256':meta['roster_sha256'],'shared_input_sha256':meta['shared_input']['sha256'],'source_commit':subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD'],text=True).strip()}
        path=SIGNALS/(str(asof.date())+'.json')
        if not path.exists():write(path,signal)
        else:signal=read(path)
        status['archived_signal_input_sha256']=signal.get('shared_input_sha256')
        status['archived_signal_matches_current_input']=signal.get('shared_input_sha256')==meta['shared_input']['sha256']
        newly=state['orders'][len(old.get('orders',[])):]
        status.update({'status':'PAPER_MODEL_READY' if signal['preopen_eligible'] else 'SIGNAL_READY_NOT_PREOPEN_ELIGIBLE',
                       'signal':signal,'new_orders':len(newly),'paper_orders_new':newly,'missed_decisions':missed,
                       'nav_eur':state.get('nav_eur',INITIAL),'nav_net_liquidation_eur':state.get('nav_net_liquidation_eur',INITIAL),
                       'cash_eur':state['cash_eur'],'positions_count':len(state['positions']),
                       'limitation':'Prospective replay with shared as-recorded ACWI cap proxy, preopen proof and conservative all-exchange gate; first-cohort calibration proxy, no certified 2012-2025 parity.'})
        write(STATE,state)
    except Exception as e:
        status.update({'status':'BLOCKED_NO_NEW_FILL','reason':str(e),'new_orders':0})
        traceback.print_exc()
    write(STATUS,status)
    print(json.dumps({k:status.get(k) for k in ['status','reason','decision_session','new_orders','nav_eur']},indent=2))
    return 1 if status['status']=='BLOCKED_NO_NEW_FILL' else 0

if __name__=='__main__':sys.exit(main())
