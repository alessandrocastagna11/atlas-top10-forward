#!/usr/bin/env python3
"""Exact-engine baseline vs weekly-buy variant on REAL Yahoo bars, 2025-26.

WARNING: The only complete ranked universe available in this repository was
observed in November 2026. Freezing it for 2025 creates severe lookahead and
survivorship biases. This research result MUST NOT be described as verified
point-in-time historical alpha, nor replace the official live PAPER account.

Both variants use the SAME frozen RegionSlotAtlas selector, market inputs, cash
interest, dividends, Italian taxes, EUR1 fills, 10bp slippage and strict FIFO
35 calendar-day minimum hold. The sole flag difference is weekly_core_entries.
"""
from __future__ import annotations
import datetime as dt, json, os, sys
from dataclasses import replace
from pathlib import Path
import numpy as np
import pandas as pd

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
import live_model as lm
import live_simulator as sim

OUT=HERE/'runs'/'weekly_buy_study'
OUT.mkdir(parents=True,exist_ok=True)
ASOF=dt.date.fromisoformat(os.environ.get('ATLAS_STUDY_ASOF','2026-11-10'))
START=pd.Timestamp(os.environ.get('ATLAS_STUDY_START','2025-03-03'))

def build():
    excluded=set()
    cache={}
    for round_no in range(50):
        vintages=lm.latest_vintages(ASOF,excluded_symbols=excluded)
        # RESEARCH ONLY: use the as-recorded latest roster for a past window.
        actual=vintages[-1]
        symbols={str(row['symbol']).upper() for row in actual[3]+actual[4]}
        factors=set(lm.SECTOR.values())|{'SPY','EURUSD=X'}
        fx={'EUR'+str(row['currency']).upper()+'=X' for row in actual[3]+actual[4]
            if str(row['currency']).upper()!='EUR'}
        required=symbols|factors|fx
        fetched,fail=lm.get_adjusted(required-set(cache),ASOF,max_workers=12)
        cache.update(fetched)
        if not fail:break
        short={ticker for ticker,error in fail.items() if ticker in symbols and
               ('insufficient history' in error.lower() or
                'few clean adjusted closes' in error.lower())}
        other={ticker:error for ticker,error in fail.items() if ticker not in short}
        if other:
            raise RuntimeError('Cannot research with missing FX/factor/vendor bars: '+str(other)[:500])
        if not short-excluded:raise RuntimeError('No progress replacing insufficient-history tickers')
        excluded.update(short)
    else:raise RuntimeError('Too many short-history exclusions')
    actual=vintages[-1]
    research=(dt.date(2024,1,2),actual[1],actual[2],actual[3],actual[4],
              'RESEARCH_HINDSIGHT_2026_UNIVERSE_BACKDATED_NOT_PIT')
    m,meta=lm.build_model(research,ASOF,cache)
    # Recompute the same forward-only alpha calibration for the 2025 research
    # start. This DOES NOT remove lookahead from 2026 issuer selection.
    calibration=m.membership
    m.expected_alpha,_=lm.v4.build_expected_alpha(
        m.path_score.where(calibration),
        m.features.close_eur.where(calibration),
        str((START-pd.Timedelta(days=3)).date()))
    return m,meta,sorted(excluded),actual

def metrics(result,tag):
    equity=result.equity.dropna()
    if len(equity)<30:raise RuntimeError('Too few comparable sessions')
    years=(equity.index[-1]-equity.index[0]).days/365.25
    cagr=(float(equity.iloc[-1])/float(equity.iloc[0]))**(1/years)-1
    dd=(equity/equity.cummax()-1).min()
    orders=result.orders
    buys=int((orders.action=='BUY').sum()) if len(orders) else 0
    sells=int((orders.action=='SELL').sum()) if len(orders) else 0
    weekly_buys=int((orders.reason=='weekly_entry_from_cash').sum()) if len(orders) else 0
    total_traded=float(result.total_traded_eur)
    div=float(result.gross_dividends_eur)
    return dict(name=tag,start=str(equity.index[0].date()),
         end=str(equity.index[-1].date()),sessions=len(equity),
         start_eur=round(float(equity.iloc[0]),2),
         ending_net_liquidation_eur=round(float(equity.iloc[-1]),2),
         net_cagr_pct=round(100*cagr,4),max_drawdown_pct=round(100*dd,4),
         orders_total=len(orders),buys=buys,sells=sells,
         buys_from_weekly_entry=weekly_buys,
         turnover_traded_eur=round(total_traded,2),
         turnover_vs_initial_capital=round(total_traded/10000,4),
         total_fees_eur=round(float(result.total_fees_eur),2),
         estimated_slippage_eur=round(float(result.estimated_slippage_eur),2),
         capital_gains_tax_eur=round(float(result.capital_gains_tax_eur),2),
         dividends_gross_eur=round(div,2),
         cash_interest_net_eur=round(float(result.cash_interest_eur),2),
         locked_sell_count=int(result.blocked_sell_count),
         min_realized_lot_days=(
             None if result.realized_lots.empty else int(result.realized_lots.calendar_days.min())))

def run():
    m,meta,excluded,source=build()
    cfg=replace(m.bundle['cfg'],target_volatility=lm.inc.TARGET_VOL,
                max_positions=10,default_slippage_bps_per_side=lm.inc.SLIPPAGE_BPS)
    pol=lm.t9.ExecutionPolicy(name=f'corrected_coreband{lm.inc.CORE_BAND}',
                              core_risk_band_weight=lm.inc.CORE_BAND)
    results={}
    for label,weekly in [('ORIGINAL_5W_BASELINE',False),
                         ('CHALLENGER_WEEKLY_NEW_BUYS',True)]:
        selector,weights,_,_=m.callbacks(None)
        res=sim.simulate_tax_aware(
            m.features,m.path_score,m.eligible,m.membership,
            m.pr_regime,m.cash_rate,None,cfg,m.raw,str(START.date()),10000.,
            label,m.tax,weekly_risk_overlay=m.pr_t8,bear_floor=m.bear_floor,
            execution_policy=pol,cash_reserve_fraction=lm.inc.CASH_RESERVE,
            name_selector=selector,target_weight_builder=weights,
            liquidate_on_final_session=False,weekly_core_entries=weekly)
        summary=metrics(res,label)
        res.orders.to_csv(OUT/(label+'_orders.csv'),index=False)
        res.equity.to_csv(OUT/(label+'_equity.csv'),header=['net_liquidation_nav_eur'])
        results[label]=summary
        print('BACKTEST',json.dumps(summary,sort_keys=True),flush=True)
    old=results['ORIGINAL_5W_BASELINE']
    new=results['CHALLENGER_WEEKLY_NEW_BUYS']
    compare=dict(
        evidence='REAL_PROVIDER_PRICE_SERIES_RETROSPECTIVE_CURRENT_2026_COHORT_NONPIT',
        historical_point_in_time_universe=False,
        warning='2026 roster reused retrospectively into 2025: lookahead and survivorship; NOT evidence of historical deployable alpha',
        selection_engine='FROZEN_REGION_SLOT_ATLAS_BOTH',
        same_fees_taxes_dividends_and_fifo=True,
        actual_vendor_roster_recorded=str(source[0]),
        research_hypothetical_cohort_effective='2024-01-02',
        exclude_short_history=excluded,universe_names=300,
        study_start=str(START.date()),study_end=str(ASOF),
        baseline=old,weekly_challenger=new,
        delta_net_cagr_pp=round(new['net_cagr_pct']-old['net_cagr_pct'],4),
        delta_max_dd_pp=round(new['max_drawdown_pct']-old['max_drawdown_pct'],4),
        additional_orders=new['orders_total']-old['orders_total'],
        additional_turnover_eur=round(new['turnover_traded_eur']-old['turnover_traded_eur'],2),
        protect_official_ledger='OFFICIAL PAPER STATE AND ORDERS UNCHANGED BY THIS RESEARCH')
    (OUT/'summary.json').write_text(json.dumps(compare,indent=2,ensure_ascii=False)+'\n')
    lines=[
       '# Atlas V4 Confidence70 weekly entries — NON-PIT diagnostic',
       '', '**This is a retrospective historical-price study, NOT a verified PIT backtest.**',
       'The issuer roster was observed in November 2026 and is reused into 2025.',
       'Every other model component is held equal, including costs, taxes and 35-day FIFO.',
       '', '| Metric | Original five-week | Weekly buys |',
       '|---|---:|---:|',
    ]
    for key in ('net_cagr_pct','max_drawdown_pct','ending_net_liquidation_eur',
                'orders_total','buys','sells','turnover_traded_eur',
                'total_fees_eur','capital_gains_tax_eur','cash_interest_net_eur'):
        lines.append('| '+key+' | '+str(old[key])+' | '+str(new[key])+' |')
    lines.extend(['','Incremental weekly purchases: '+str(new['buys_from_weekly_entry']),
                  'Historical constituency PIT: NO. Do not compare with 2012-2025 original PARITY.',
                  'Broker fills: none. Official virtual forward orders unchanged.'])
    (OUT/'comparison.md').write_text('\n'.join(lines)+'\n')
    print('STUDY_COMPLETE',json.dumps(compare,sort_keys=True),flush=True)

if __name__=='__main__':run()
