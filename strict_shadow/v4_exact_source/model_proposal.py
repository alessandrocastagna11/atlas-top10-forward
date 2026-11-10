"""Explain what the ORIGINAL frozen engine would propose at last verified close.

This is a NO-FILL, READ-ONLY indicative order planner. Does not modify paper
state, never substitutes yesterday's close for tomorrow's actual open.
"""
from __future__ import annotations
import datetime as dt,math
import numpy as np,pandas as pd
from dataclasses import replace
from live_model import v1,v2,inc,INITIAL

def proposal(m,meta,asof):
    date=pd.Timestamp(asof)
    cfg=replace(m.bundle["cfg"],target_volatility=inc.TARGET_VOL,
                max_positions=10,default_slippage_bps_per_side=inc.SLIPPAGE_BPS)
    choose,build,_,_=m.callbacks(None)
    score=m.path_score.loc[date]
    eligible=(m.eligible.loc[date]&m.membership.loc[date]).fillna(False)
    rank=score.where(eligible).rank(ascending=False,method="min")
    names=choose(date,score,rank,eligible,{},cfg)
    risk=float(m.pr_regime.loc[date]*m.pr_t8.loc[date])
    if names and risk>0:
        risky,relative,cov=v2.estimate_risky_volatility(date,names,m.features.close_eur,cfg.covariance_lookback)
        scale=min(1.0,cfg.target_volatility/risky) if risky>0 else 0.0
        exposure=risk*scale
        targets=build(relative,exposure,cfg,decision_date=date)
        realized=float(np.sqrt(max(0.,targets.to_numpy()@cov@targets.to_numpy())))
        if realized>cfg.target_volatility:
            targets*=cfg.target_volatility/realized
    else:
        targets=pd.Series(dtype=float)
        risky=float("nan")
        scale=0.
    threshold=max(float(cfg.minimum_trade_eur),float(cfg.rebalance_band_weight)*INITIAL)
    indicative=[]
    for ticker,weight in targets.sort_values(ascending=False).items():
        weight=float(weight)
        nominal=INITIAL*weight
        market_ticker=meta["ticker_to_market_symbol"].get(ticker,ticker)
        close=float(m.raw.close_eur.at[date,ticker])
        if not math.isfinite(close) or close<=0:continue
        indicative.append({
            "market_symbol":market_ticker,"ticker":ticker,"weight_pct":round(weight*100,5),
            "indicative_allocation_eur":round(nominal,2),
            "reference_previous_close_eur":round(close,8),
            "indicative_shares_at_previous_close":round(nominal/close,8),
            "trade_threshold_eur":round(threshold,2),
            "meets_minimum_order_threshold":nominal>=threshold,
            "proposed_action":"POTENTIAL_BUY_ONLY_NOT_ORDER"})
    return {
       "status":"INDICATIVE_ONLY_NO_ORDER_NO_FILL",
       "source_original_engine":True,
       "decision_session":str(date.date()),
       "computed_from_last_complete_close":True,
       "execution_price_is_not_yesterdays_close":True,
       "capital_eur":INITIAL,
       "engine_source":"frozen RegionSlotAtlas chooser and original volatility constrained target builder",
       "regime_multiplier":round(risk,7),
       "unscaled_risky_volatility":None if not math.isfinite(risky) else round(risky,7),
       "min_order_eur":round(threshold,2),
       "possible_buys_meeting_threshold":sum(p["meets_minimum_order_threshold"] for p in indicative),
       "possible_candidates":indicative,
       "real_paper_orders_created":0,
       "note":"Final buys require actual next-session open quotes and full original simulator execution."}
