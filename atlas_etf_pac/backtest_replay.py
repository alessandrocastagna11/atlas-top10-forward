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
import os
from pathlib import Path
from collections import Counter
import engine as e

ROOT=Path(__file__).resolve().parent
START_HISTORY=dt.date(2014,1,1)
FIRST_DATE=dt.date.fromisoformat(os.environ.get('ATLAS_BACKTEST_FIRST_DATE','2016-01-04'))
END_DATE=dt.date(2026,11,11)
USE_GOLD_PROXY=os.environ.get('ATLAS_BACKTEST_GOLD_PROXY','1')=='1'
OUT=ROOT/'backtests'/('replay_2016_2026.json' if USE_GOLD_PROXY else 'replay_2022_2026_actual4.json')

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
    # PPFB only starts in the current data source in the early 2020s.
    # Extend the historical GOLD sleeve with a live-traded, EUR-denominated
    # physical gold ETC: Invesco Physical Gold 8PSG.DE, formed in 2009.
    # Anchor the earlier proxy to PPFB's first shared CLOSE; use proxy
    # RETURNS, not raw unrelated unit prices. From switch day forward
    # all GOLD prices use the actual PPFB instrument.
    proxy_sym=None
    proxy=None
    switch=None
    factor=None
    if USE_GOLD_PROXY:
        proxy_sym='8PSG.DE'
        proxy=e.yahoo(proxy_sym,START_HISTORY,END_DATE)
        gold=data[cfg['tickers']['GOLD']]
        overlap=sorted(set(proxy['bars']) & set(gold['bars']))
        if not overlap:raise RuntimeError('GOLD proxy overlap missing')
        switch=overlap[0]
        factor=(gold['bars'][switch]['close'] / proxy['bars'][switch]['close'])
        for day,b in proxy['bars'].items():
            if day<switch and day not in gold['bars']:
                gold['bars'][day]={'open':b['open']*factor,'close':b['close']*factor}
        print('GOLD pre-PPFB proxy',proxy_sym,'bars',len(proxy['bars']),
              'splice',switch,'factor',round(factor,8),flush=True)
    intersection=set.intersection(*(set(d['bars']) for d in data.values()))
    sessions=sorted(day for day in intersection if FIRST_DATE.isoformat()<=day<=END_DATE.isoformat())
    if not sessions:raise RuntimeError('No common Xetra sessions')
    if len(sessions)<(1800 if USE_GOLD_PROXY else 850):raise RuntimeError('Insufficient common EUR session coverage')
    if (END_DATE-dt.date.fromisoformat(sessions[-1])).days>5:
        raise RuntimeError('Last common ETF session stale')
    pre=[d for d in intersection if d<sessions[0]]
    if not pre:raise RuntimeError('Missing pre-seed session')
    seed=sorted(pre)[-1]
    cfg=copy.deepcopy(cfg)
    cfg['first_possible_decision_session']=sessions[0]
    first=dt.date.fromisoformat(sessions[0])
    following=dt.date(first.year+1,1,1) if first.month==12 else dt.date(first.year,first.month+1,1)
    cfg['monthly_contribution_first_month']=following.isoformat()[:7]
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
        'period':str(FIRST_DATE)[:7]+' to 2026-11 (observed common exchange sessions)',
        'model_method':'historical reconstructed next-session Xetra open replay',
        'data_feed':'Yahoo daily OHLC, unofficial, as retrieved in a 2026 snapshot',
        'gold_history_extension':({'historical_proxy':proxy_sym,
            'proxy_until_exclusive':switch,'original_gold_ticker':cfg['tickers']['GOLD'],
            'splice_factor':factor,'raw_proxy_sha256':proxy['sha256'],
            'warning':'Pre-switch backtest proxy ETC has different fees, issuer and possible tracking difference'}
            if USE_GOLD_PROXY else None),
        'bias_note':'Backtest uses currently accessible OHLC histories and is not point-in-time validation of original vintage provider data',
        'param_mutations':['First decision session set to '+str(FIRST_DATE)+' for research',
                           'Monthly contribution begins '+cfg['monthly_contribution_first_month']],
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
