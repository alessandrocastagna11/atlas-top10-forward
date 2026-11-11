"""Explain what the ORIGINAL frozen engine would propose at last verified close.

This is a NO-FILL, READ-ONLY indicative order planner. Does not modify paper
state, never substitutes yesterday's close for tomorrow's actual open.
"""
from __future__ import annotations
import datetime as dt,math
import numpy as np,pandas as pd
from dataclasses import replace
from live_model import v1,v2,inc,INITIAL
from paper_account import RESERVE as PAPER_CASH_RESERVE

def proposal(m,meta,asof,positions=None,nav_eur=None):
    """Original target builder with *incremental* orders against real PAPER holdings.

    Does not execute. positions is a mapping of ticker to v2.Position, using
    only lots recorded by the decision close. Empty is valid for initial entry.
    """
    positions={} if positions is None else positions
    date=pd.Timestamp(asof)
    cfg=replace(m.bundle["cfg"],target_volatility=inc.TARGET_VOL,
                max_positions=10,default_slippage_bps_per_side=inc.SLIPPAGE_BPS)
    choose,build,_,_=m.callbacks(None)
    score=m.path_score.loc[date]
    eligible=(m.eligible.loc[date]&m.membership.loc[date]).fillna(False)
    rank=score.where(eligible).rank(ascending=False,method="min")
    names=choose(date,score,rank,eligible,positions,cfg)
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
    portfolio_value=float(nav_eur) if nav_eur is not None else float(INITIAL)
    if not math.isfinite(portfolio_value) or portfolio_value<=0:raise ValueError('Invalid portfolio NAV')
    threshold=max(float(cfg.minimum_trade_eur),float(cfg.rebalance_band_weight)*portfolio_value)
    existing_value=0.0
    close_row=m.raw.close_eur.loc[date]
    for ticker,pos in positions.items():
        price=float(close_row.get(ticker,float('nan')))
        if not math.isfinite(price) or price<=0:
            raise ValueError('Missing owned holding valuation: '+str(ticker))
        existing_value+=float(pos.shares)*price
    # No phantom cash and no exposure above the original strategy target.
    target_total=portfolio_value*float(targets.sum())
    exposure_room=max(0.0,target_total-existing_value)
    cash_room=max(0.0,portfolio_value-existing_value-PAPER_CASH_RESERVE)
    remaining=min(exposure_room,cash_room)
    indicative=[]
    for ticker,weight in targets.sort_values(ascending=False).items():
        weight=float(weight)
        nominal=portfolio_value*weight
        market_ticker=meta["ticker_to_market_symbol"].get(ticker,ticker)
        close=float(m.raw.close_eur.at[date,ticker])
        if not math.isfinite(close) or close<=0:continue
        owned=close*float(positions[ticker].shares) if ticker in positions else 0.0
        desired_addition=max(0.0,nominal-owned)
        feasible_addition=min(desired_addition,remaining)
        qualifies=feasible_addition>=threshold-1e-8
        addition=feasible_addition if qualifies else 0.0
        if qualifies:remaining=max(0.0,remaining-addition)
        indicative.append({
            "market_symbol":market_ticker,"ticker":ticker,"weight_pct":round(weight*100,5),
            "indicative_allocation_eur":round(nominal,2),
            "existing_position_value_eur":round(owned,2),
            "increment_required_to_reach_target_eur":round(desired_addition,2),
            "indicative_new_buy_eur":round(addition,2),
            "reference_previous_close_eur":round(close,8),
            "indicative_shares_at_previous_close":round(nominal/close,8),
            "indicative_new_shares_at_previous_close":round(addition/close,8),
            "trade_threshold_eur":round(threshold,2),
            "meets_minimum_order_threshold":qualifies,
            "proposed_action":"POTENTIAL_INCREMENTAL_BUY_ONLY_NOT_ORDER" if qualifies else "HOLD_OR_TOO_SMALL_NO_ORDER"})
    return {
       "status":"INDICATIVE_ONLY_NO_ORDER_NO_FILL",
       "source_original_engine":True,
       "decision_session":str(date.date()),
       "computed_from_last_complete_close":True,
       "execution_price_is_not_yesterdays_close":True,
       "capital_eur":round(portfolio_value,4),
       "existing_position_count":len(positions),
       "existing_equity_value_eur":round(existing_value,4),
       "target_total_equity_value_eur":round(target_total,4),
       "exposure_room_before_buys_eur":round(exposure_room,4),
       "cash_room_before_buys_eur":round(cash_room,4),
       "unused_budget_after_planned_buys_eur":round(remaining,4),
       "selection_considers_existing_lots":True,
       "proposal_budget_is_incremental":True,
       "engine_source":"frozen RegionSlotAtlas chooser and original volatility constrained target builder",
       "regime_multiplier":round(risk,7),
       "unscaled_risky_volatility":None if not math.isfinite(risky) else round(risky,7),
       "min_order_eur":round(threshold,2),
       "possible_buys_meeting_threshold":sum(p["meets_minimum_order_threshold"] for p in indicative),
       "possible_candidates":indicative,
       "real_paper_orders_created":0,
       "note":"Only INCREMENTAL buy budgets, reflecting existing positions. Final fills require completed local-exchange session evidence."}
