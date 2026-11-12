#!/usr/bin/env python3
"""Historical 4-sleeve PAC replay, isolated from forward state. Not PIT macro V2.3.

Real EUR OHLC from the same Yahoo daily provider as the paper engine.
Reuses actual production process() and its cost/risk/trade rules, but time
travels only in this explicitly separate in-sample research simulation.
"""
import copy
import datetime as dt
import json
import math
from pathlib import Path
from collections import Counter
import engine as e

ROOT=Path(__file__).resolve().parent
START_HISTORY=dt.date(2014,1,1)
FIRST_DATE=dt.date(2016,1,4)
END_DATE=dt.date(2026,11,11)
OUT=ROOT/'backtests'/'replay_2016_2026.json'

def xirr(flows):
    origin=flows[0][0]
    def npv(rate):
        return sum(v / ((1.+rate)**((t-origin).days/365.25)) for t,v in flows)
    lo=-0.999
    hi=3.
    try:
        while npv(lo)*npv(hi)>0 and hi<10000:
            hi=2*hi+1
        if npv(lo)*npv(hi)>0:return None
        for _ in range(140):
            mid=(lo+hi)/2
            if npv(lo)*npv(mid)>0: lo=mid
            else: hi=mid
        return (lo+hi)/2
    except (OverflowError,ZeroDivisionError):return None

def metric(series, contributions, initial, orders, trades=None):
    start=dt.date.fromisoformat(series[0][0])
    last=dt.date.fromisoformat(series[-1][0])
    periods=(last-start).days/365.25
    twr=1.
    peak=1.
    mdd=0.
    for i in range(1,len(series)):
        today=float(series[i][1])
        previous=float(series[i-1][1])
        deposit=float(contributions.get(series[i][0],0))
        twr*=1+(today-deposit)/previous-1
        peak=max(peak,twr)
        mdd=min(mdd,twr/peak-1)
    capital=initial+sum(contributions.values())
    flows=[(start,-initial)]
    flows.extend((dt.date.fromisoformat(d),-val) for d,val in sorted(contributions.items()))
    flows.append((last,float(series[-1][1])))
    irr=xirr(flows)
    return {'start_session':series[0][0],'end_session':series[-1][0],
            'initial_eur':initial,'contributions_eur':round(capital-initial,2),
            'total_paid_eur':round(capital,2),'final_nav_eur':round(float(series[-1][1]),2),
            'profit_eur':round(float(series[-1][1])-capital,2),
            'twr_total_pct':round((twr-1)*100,4),
            'twr_cagr_pct':round(((twr**(1/periods))-1)*100,4),
            'max_drawdown_pct':round(mdd*100,4),
            'xirr_pct':round(100*irr,4) if irr is not None else None,
            'orders':orders,
            'trades':trades or {}}

def main():
    cfg=json.loads(e.CONFIG.read_text())
    data={}
    for asset,symbol in cfg['tickers'].items():
        data[symbol]=e.yahoo(symbol,START_HISTORY,END_DATE)
        print('Source',symbol,len(data[symbol]['bars']),'bars',max(data[symbol]['bars']),flush=True)
    intersection=set.intersection(*(set(d['bars']) for d in data.values()))
    sessions=sorted(day for day in intersection if FIRST_DATE.isoformat()<=day<=END_DATE.isoformat())
    if not sessions:raise RuntimeError('No common Xetra sessions')
    if len(sessions)<1800:raise RuntimeError('Insufficient common EUR session coverage')
    if (END_DATE-dt.date.fromisoformat(sessions[-1])).days>5:
        raise RuntimeError('Last common ETF session stale')
    pre=[d for d in intersection if d<sessions[0]]
    if not pre:raise RuntimeError('Missing pre-seed session')
    seed=sorted(pre)[-1]
    cfg=copy.deepcopy(cfg)
    cfg['first_possible_decision_session']=sessions[0]
    cfg['monthly_contribution_first_month']='2016-02'
    st={'schema':1,'model':cfg['model'],'mode':cfg['mode'],'started_at':seed,
        'last_processed_session':seed,'last_risk_on':None,
        'last_deposit_month':sessions[0][:7],
        'off_trough':None,'cash_eur':1000.,'positions':{},
        'orders':[],'blocked_actions':[],'contributions':[],
        'signal_history':[],'pending':None,'stamp_accrued_eur':0.,
        'nav':[{'date':seed,'cash_eur':1000.,'holdings_eur':0.,
                'nav_eur':1000.,'twr_index':1.}]}
    # The passive benchmark has permanent 65/15/10/10 fractional units,
    # initial allocation at the first actual observed OPEN, then each
    # month's deposit at that month's first common Xetra open.
    weights=cfg['weights'];units={k:0. for k in weights}
    passive_deposit_month=sessions[0][:7]
    passive_stamp=0.;passive_prev_holding=0.
    passive_nav=[]
    passive_deposits={}
    active_nav=[(seed,1000.)]
    holdings_period=0.
    for i,session in enumerate(sessions):
        now=dt.datetime.combine(dt.date.fromisoformat(session),
                                dt.time(20,30),tzinfo=e.ROME)
        previous_day=dt.date.fromisoformat(sessions[i-1] if i else seed)
        elapsed=(dt.date.fromisoformat(session)-previous_day).days
        passive_stamp+=passive_prev_holding*cfg['annual_stamp_proxy']*elapsed/365.25
        contrib=0.
        if i==0:contrib=1000.
        elif session[:7]>passive_deposit_month:
            contrib=250.
            passive_deposit_month=session[:7]
            passive_deposits[session]=contrib
        for k,sym in cfg['tickers'].items():
            price=e.num(data[sym]['bars'][session]['open'])
            units[k]+=contrib*weights[k]/price
        passive_prev_holding=sum(units[k]*e.num(data[sym]['bars'][session]['close'])
                                 for k,sym in cfg['tickers'].items())
        passive_nav.append((session,passive_prev_holding-passive_stamp))
        st,filled=e.process(cfg,st,session,data,now)
        holdings_period+=sum(p['shares']*data[cfg['tickers'][k]]['bars'][session]['close']
                             for k,p in st['positions'].items())
        active_nav.append((session,st['nav'][-1]['nav_eur']))
        if (i+1)%450==0:
            print('replayed',i+1,'/',len(sessions),session,flush=True)
    active_deposits={q['date']:q['amount_eur'] for q in st['contributions']}
    active=metric(active_nav,active_deposits,1000.,len(st['orders']),
                  dict(Counter(o['side'] for o in st['orders'])))
    passive=metric([(seed,1000.)]+passive_nav,passive_deposits,1000.,0)
    # First session benchmark initial transaction applied during day 1, so
    # benchmark's initial 1000 is recorded as seed cash not a new deposit.
    all_day=dt.date.fromisoformat(sessions[-1])
    out={
        'strategy':'ATLAS_ETF_PAC_4SLEEVE_RESTART_V1',
        'period':'2016-01 to 2026-11 (observed common exchange sessions)',
        'model_method':'historical reconstructed next-session Xetra open replay',
        'data_feed':'Yahoo daily OHLC, unofficial, as retrieved in a 2026 snapshot',
        'bias_note':'Backtest uses today's accessible OHLC histories and is not point-in-time validation of original vintage provider data',
        'param_mutations':['First decision date set to Jan 2016 for research', 'Monthly contribution begins Feb 2016'],
        'costs_note':'Engine fees/slippage/taxes/annual stamp estimates; passive baseline also annual stamp but no fees or slippage',
        'active':active,'passive_4etf_pac':passive,
        'sessions':len(sessions),'last_available_session':sessions[-1],
        'quotes':{k:{'last_day':max(v['bars']), 'raw_source_sha256':v['sha256'],
                      'first_day':min(v['bars']), 'count':len(v['bars'])}
                  for k,v in data.items()},
        'risk_limit_note':'35-day min hold is enforced by current engine using latest buy date per sleeve, not exact per-lot sales',
        'max_total_orders_per_month':cfg['max_total_orders_per_month'],
        'blocked_actions_count':len(st['blocked_actions']),
        'final_positions':{k:round(v['shares'],8) for k,v in st['positions'].items()},
        'tax_proxy_paid_eur':round(sum(o['tax_proxy_eur'] for o in st['orders']),2),
        'fees_eur':round(sum(o['fee_eur'] for o in st['orders']),2),
        'stamp_accrued_eur':round(st['stamp_accrued_eur'],2),
        'average_invested_eur':round(holdings_period/len(sessions),2)
    }
    e.save(OUT,out)
    print('BACKTEST_RESULT_JSON '+json.dumps(out,sort_keys=True),flush=True)

if __name__=='__main__':
    main()
