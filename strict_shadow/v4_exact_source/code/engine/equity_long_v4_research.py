#!/usr/bin/env python3
from __future__ import annotations
import argparse, dataclasses, math, json
from pathlib import Path
from typing import Mapping
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

import equity_long_backtest as v1
import equity_long_backtest_v2 as v2
import equity_long_bear_tax_comparison as t9
import equity_long_overlay_comparison as overlays

TRIAL_COUNT = 33
BASE = "T9A reference"
V4A = "V4A Price-heat penalty"
V4B = "V4B Cost-alpha gate"
V4C = "V4C Combined"
STRATS = (BASE,V4A,V4B,V4C)

@dataclasses.dataclass(frozen=True)
class GateSpec:
    enabled: bool = False
    hard_exit_rank: int = 30
    min_uncertainty_hurdle: float = 0.010
    tax_deferral_weight: float = 0.25
    expected_alpha_cap: float = 0.20


def make_price_heat(features: v1.FeatureBundle) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame]:
    close = features.close_usd
    ret63 = np.log(close).diff(63)
    trend = features.feature_tables["trend_strength"]
    # Percentile heat: persistent distance above long trend + recent 3m acceleration.
    p_trend = trend.rank(axis=1, pct=True)
    p_ret63 = ret63.rank(axis=1, pct=True)
    heat = 0.65*p_trend + 0.35*p_ret63
    excess = ((heat - 0.85)/0.15).clip(lower=0.0, upper=1.0)
    # Convex penalty: mild at p85-p90, material only in the hottest tail.
    penalty = 0.65 * excess.pow(2)
    # Extreme crowding block, deliberately rare.
    block = (heat >= 0.985) & (ret63 >= np.log(1.35)) & (features.feature_tables["extension_5"] > 0.5)
    return heat, penalty, block


def build_expected_alpha(score: pd.DataFrame, close_eur: pd.DataFrame, test_start: str) -> tuple[pd.DataFrame,pd.Series]:
    """Vectorized expanding point-in-time mapping score -> 63-session alpha."""
    idx=score.index; h=63; n=len(idx)
    fwd=close_eur.shift(-h).div(close_eur).sub(1.0)
    fwd_alpha=fwd.sub(fwd.median(axis=1),axis=0).clip(-0.60,0.60)
    x=score.clip(-3,3).to_numpy(float); y=fwd_alpha.to_numpy(float)
    valid=np.isfinite(x)&np.isfinite(y)
    # Use every 21st session to reduce overlap; each row contributes cross-sectional sufficient statistics.
    sample=np.zeros(n,dtype=bool); sample[::21]=True
    xy=np.where(valid,x*y,0.0).sum(axis=1); xx=np.where(valid,x*x,0.0).sum(axis=1)
    xy[~sample]=0.0; xx[~sample]=0.0
    cxy=np.r_[0.0,np.cumsum(xy)]; cxx=np.r_[0.0,np.cumsum(xx)]
    beta=np.full(n,np.nan); start_i=max(h+252,int(idx.searchsorted(pd.Timestamp(test_start))))
    lookback=252*8
    for i in range(start_i,n):
        cutoff=i-h; lo=max(0,cutoff-lookback)
        num=cxy[cutoff+1]-cxy[lo]; den=cxx[cutoff+1]-cxx[lo]
        if den>1e-12: beta[i]=np.clip(num/den,0.0,0.12)
    beta_s=pd.Series(beta,index=idx).ffill(limit=5)
    out=score.clip(-3,3).mul(beta_s,axis=0)
    return out,beta_s


def gate_choose_factory(alpha_panel: pd.DataFrame, close_eur: pd.DataFrame, tax_enabled: bool, spec: GateSpec):
    def choose(date, score_row, rank_row, eligible_row, positions, cfg):
        ranked = score_row.where(eligible_row).dropna().sort_values(ascending=False).index.tolist()
        alpha_row = alpha_panel.loc[date] if date in alpha_panel.index else pd.Series(dtype=float)
        kept=[]; sector_counts={}
        # Keep valid incumbents up to a wider safety rank; true ineligibility still exits.
        for ticker in positions:
            pos=positions[ticker]
            old=(date-pos.first_entry_date).days>=cfg.minimum_holding_calendar_days
            valid=bool(eligible_row.get(ticker,False)) and pd.notna(rank_row.get(ticker))
            safety=valid and float(rank_row[ticker])<=spec.hard_exit_rank
            if safety or not old:
                sec=v1.TICKER_TO_SECTOR[ticker]
                if sector_counts.get(sec,0)<cfg.max_names_per_sector:
                    kept.append(ticker); sector_counts[sec]=sector_counts.get(sec,0)+1
        kept=kept[:cfg.max_positions]
        for ticker in ranked:
            if len(kept)>=cfg.max_positions: break
            if ticker in kept: continue
            sec=v1.TICKER_TO_SECTOR[ticker]
            if sector_counts.get(sec,0)>=cfg.max_names_per_sector: continue
            kept.append(ticker); sector_counts[sec]=sector_counts.get(sec,0)+1
        if not spec.enabled or len(kept)<cfg.max_positions:
            return kept

        # Consider ranked newcomers against the weakest expected-alpha incumbent.
        for cand in ranked[:max(20,cfg.max_positions*2)]:
            if cand in kept: continue
            cand_a=float(alpha_row.get(cand,np.nan))
            if not np.isfinite(cand_a): continue
            replaceable=[]
            for inc in kept:
                if inc not in positions: continue
                if (date-positions[inc].first_entry_date).days<cfg.minimum_holding_calendar_days: continue
                inc_a=float(alpha_row.get(inc,np.nan))
                if np.isfinite(inc_a): replaceable.append((inc,inc_a))
            if not replaceable: break
            inc,inc_a=min(replaceable,key=lambda z:z[1])
            newsec=v1.TICKER_TO_SECTOR[cand]; oldsec=v1.TICKER_TO_SECTOR[inc]
            if newsec!=oldsec and sector_counts.get(newsec,0)>=cfg.max_names_per_sector: continue
            pos=positions[inc]
            px=float(close_eur.at[date,inc]) if pd.notna(close_eur.at[date,inc]) else np.nan
            value=pos.shares*px if np.isfinite(px) else 0.0
            direct=0.002 # two sides x 10bps
            if value>0: direct += 2*cfg.fixed_fee_eur/value
            tax_pen=0.0
            if tax_enabled and value>0:
                gain=max(0.0,value-pos.cost_basis_eur)
                immediate_tax_ratio=0.26*gain/value
                tax_pen=spec.tax_deferral_weight*immediate_tax_ratio
            hurdle=spec.min_uncertainty_hurdle+direct+tax_pen
            if cand_a-inc_a >= hurdle:
                kept[kept.index(inc)]=cand
                sector_counts[oldsec]-=1; sector_counts[newsec]=sector_counts.get(newsec,0)+1
        return kept
    return choose


def metric_row(res:t9.TaxAwareResult,cash_rate:pd.Series,capital:float)->dict:
    d={"scenario":res.scenario,"strategy":res.name}
    d.update(v2.metrics(res.equity,cash_rate,capital,res,declared_trials=TRIAL_COUNT))
    d.update({"execution_cost_eur":res.total_fees_eur+res.estimated_slippage_eur,
              "total_tax_eur":res.total_tax_eur,"capital_gains_tax_eur":res.capital_gains_tax_eur,
              "total_traded_eur":res.total_traded_eur})
    return d


def spy_buy_hold(raw:t9.RawMarketData, cash_rate:pd.Series, tax:t9.TaxConfig, start:str, capital:float, fee=1.0, slip=10.0):
    idx=raw.close_eur.index[raw.close_eur.index>=pd.Timestamp(start)]
    ticker=v1.BENCHMARK
    # first valid open
    entry=next(d for d in idx if pd.notna(raw.open_eur.at[d,ticker]))
    px=float(raw.open_eur.at[entry,ticker]); execpx=px*(1+slip/10000)
    shares=(capital-fee)/execpx; basis=capital
    cash=0.0; foreign=italy=stamp=0.0
    curve=[]; dates=[]
    prev=None
    for d in idx:
        if d<entry: continue
        if prev is not None:
            days=max(0,(d-prev).days)
            invested=shares*float(raw.close_eur.at[prev,ticker]) if pd.notna(raw.close_eur.at[prev,ticker]) else 0.0
            if tax.enabled:
                st=invested*t9.historical_stamp_rate(prev)*days/365.25
                cash-=st; stamp+=st
        per=float(raw.distribution_eur_per_share.at[d,ticker])
        if np.isfinite(per) and per>0:
            gross=shares*per
            taxable=bool(raw.taxable_distribution.at[d,ticker])
            fr=tax.us_dividend_withholding_rate if tax.enabled and taxable else 0.0
            ir=t9.italian_financial_rate(d) if tax.enabled and taxable else 0.0
            ft=gross*fr; it=(gross-ft)*ir
            cash+=gross-ft-it; foreign+=ft; italy+=it
        val=cash+shares*float(raw.close_eur.at[d,ticker])
        curve.append(val); dates.append(d); prev=d
    # liquidate at last close, replacing final mark with after-tax proceeds
    last=dates[-1]; px=float(raw.close_eur.at[last,ticker]); ex=px*(1-slip/10000)
    proceeds=shares*ex-fee
    gain=max(0.0,proceeds-basis)
    cap_tax=0.26*gain if tax.enabled else 0.0
    final=cash+proceeds-cap_tax
    curve[-1]=final
    s=pd.Series(curve,index=pd.DatetimeIndex(dates),name="SPY Buy & Hold")
    return s,{"orders":2,"fees_eur":2.0,"slippage_eur":shares*px*2*slip/10000,
             "capital_gains_tax_eur":cap_tax,"dividend_tax_eur":foreign+italy,"stamp_duty_eur":stamp,
             "total_tax_eur":cap_tax+foreign+italy+stamp}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--data-start',default='2007-01-01'); ap.add_argument('--test-start',default='2012-01-03'); ap.add_argument('--end',default='2026-09-10'); ap.add_argument('--capital',type=float,default=10000.0); ap.add_argument('--cache-dir',default='data_cache'); ap.add_argument('--input-dir',default='inputs'); ap.add_argument('--output-dir',default='output_v4'); ap.add_argument('--workers',type=int,default=5)
    args=ap.parse_args(); out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True); cache=Path(args.cache_dir); inp=Path(args.input_dir)
    base_cfg=v2.V2Config()
    symbols=sorted(set(v1.TICKER_TO_SECTOR)|set(v1.SECTOR_UNIVERSE)|{v1.BENCHMARK,v1.FX_TICKER})
    data,fail=v1.download_universe(symbols,args.data_start,args.end,cache,False,args.workers)
    stocks=[t for t in v1.TICKER_TO_SECTOR if t in data]
    features=v1.build_features(data,v1.StrategyConfig()); idx=features.close_eur.index
    membership,memstat=v2.load_membership_mask(inp/'universe_membership.csv',idx,stocks)
    pit,pitstat=v2.load_pit_signals(inp/'pit_signals.csv',idx,stocks)
    costs,coststat=v2.load_execution_costs(inp/'execution_costs.csv',idx,stocks)
    cash_rate,cashstat=v2.fetch_ecb_cash_rate(idx,cache,False,base_cfg.cash_rate_haircut_bps)
    base_score,weights=v2.combine_rule_score(features.score,pit)
    eligible=features.eligible & membership
    base_regime=v2.continuous_regime(features,data)
    t8,_=overlays.t8_volatility_overlay(data,idx)
    signals=t9.bear_exposure_signals(data,features,t8)
    raw=t9.reconstruct_raw_market_data(data,idx,[*stocks,v1.BENCHMARK])

    heat,penalty,block=make_price_heat(features)
    score_a=base_score-penalty.reindex_like(base_score).fillna(0.0)
    elig_a=eligible & ~block.reindex_like(eligible).fillna(False)
    print('Building walk-forward expected-alpha mapping...',flush=True)
    alpha_base,beta_base=build_expected_alpha(base_score,features.close_eur,args.test_start)
    alpha_a,beta_a=build_expected_alpha(score_a,features.close_eur,args.test_start)

    taxcfgs=(t9.TaxConfig('italian_tax',True),)
    results={}; rows=[]; bhrows=[]
    original_choose=v2.choose_names
    policy=t9.ExecutionPolicy()
    for taxcfg in taxcfgs:
        net_cash=t9.net_cash_rate(cash_rate,taxcfg)
        bh,btax=spy_buy_hold(raw,net_cash,taxcfg,args.test_start,args.capital)
        bm=v2.metrics(bh,net_cash,args.capital,None,declared_trials=1); bm.update({'scenario':taxcfg.name,'strategy':'SPY Buy & Hold',**btax}); bhrows.append(bm)
        specs=[
            (V4A,score_a,elig_a,GateSpec(False),alpha_a),
            (V4B,base_score,eligible,GateSpec(True),alpha_base),
            (V4C,score_a,elig_a,GateSpec(True),alpha_a),
        ]
        for name,score,elig,gspec,alpha in specs:
            print(f'Running {taxcfg.name}: {name}',flush=True)
            if gspec.enabled:
                v2.choose_names=gate_choose_factory(alpha,features.close_eur,taxcfg.enabled,gspec)
            else:
                v2.choose_names=original_choose
            res=t9.simulate_tax_aware(features,score,elig,membership,base_regime,cash_rate,costs,base_cfg,raw,args.test_start,args.capital,name,taxcfg,weekly_risk_overlay=t8,bear_floor=signals['t9a_exposure_floor'],execution_policy=policy)
            results[(taxcfg.name,name)]=res; rows.append(metric_row(res,net_cash,args.capital))
    v2.choose_names=original_choose
    metrics=pd.DataFrame(rows+bhrows)
    metrics.to_csv(out/'metrics_v4.csv',index=False)
    # order reason breakdown
    reason=[]
    for (sc,n),r in results.items():
        if len(r.orders):
            g=r.orders.groupby('reason').agg(orders=('ticker','size'),traded_eur=('reference_value_eur','sum')).reset_index(); g.insert(0,'strategy',n); g.insert(0,'scenario',sc); reason.append(g)
    pd.concat(reason,ignore_index=True).to_csv(out/'reason_breakdown_v4.csv',index=False)
    eq={f'{sc}__{n}':r.equity for (sc,n),r in results.items()}
    pd.concat(eq,axis=1).to_csv(out/'equity_curves_v4.csv',index_label='Date')
    pd.DataFrame({'beta_base':beta_base,'beta_heat':beta_a}).to_csv(out/'alpha_mapping_beta.csv',index_label='Date')
    # compact net chart
    fig,ax=plt.subplots(figsize=(10,6))
    # Existing certified T9A curve from the prior batch, plus new V4 curves.
    ref_path=Path('output_buffer/equity_curves_buffer.csv')
    if ref_path.exists():
        ref=pd.read_csv(ref_path,index_col=0,parse_dates=True)
        refcol=next((c for c in ref.columns if c.startswith('italian_tax__T9A')),None)
        if refcol: ax.plot(ref.index,ref[refcol].values,label=BASE)
    for n in (V4A,V4B,V4C):
        ax.plot(results[('italian_tax',n)].equity.index,results[('italian_tax',n)].equity.values,label=n)
    bh,_=spy_buy_hold(raw,t9.net_cash_rate(cash_rate,t9.TaxConfig('italian_tax',True)),t9.TaxConfig('italian_tax',True),args.test_start,args.capital)
    ax.plot(bh.index,bh.values,label='SPY B&H tax-aware',linestyle='--'); ax.set_yscale('log'); ax.grid(True,alpha=.25); ax.legend(); ax.set_title('Equity Long V4 research — Italian-tax scenario'); fig.tight_layout(); fig.savefig(out/'equity_v4_tax.png',dpi=160); plt.close(fig)
    manifest={'pit_signal_status':pitstat,'membership_status':memstat,'execution_cost_status':coststat,'cash_rate_status':cashstat,'failures':fail,'trial_count':TRIAL_COUNT,'price_heat_note':'technical crowding/overextension proxy; not fundamental valuation','gate':dataclasses.asdict(GateSpec(True)),'score_weights':weights}
    (out/'manifest_v4.json').write_text(json.dumps(manifest,indent=2,default=str),encoding='utf-8')
    print('\nNET RESULTS')
    cols=['strategy','ending_value_eur','cagr_pct','sharpe_excess_cash','max_drawdown_pct','calmar','orders','annual_one_way_turnover','execution_cost_eur','total_tax_eur']
    net=metrics[metrics.scenario=='italian_tax'].copy()
    print(net[[c for c in cols if c in net.columns]].to_string(index=False))

if __name__=='__main__': main()
