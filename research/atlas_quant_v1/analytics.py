#!/usr/bin/env python3
"""Descriptive diagnostics for frozen Atlas Quant v1 research; no fitting."""
from __future__ import annotations
from pathlib import Path
import numpy as np,pandas as pd,matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parent
REPORTS=ROOT/'reports'
STRATS=['Top10_Eligible_BH','Top10_Eligible_MA6_12','Quant20_Momentum','Quant20_TrendVol']
COLS={s:s for s in STRATS}
COLS['Global_proxy_60_30_10']='Global_Proxy'

def load():
    d={s:pd.read_csv(REPORTS/(s+'_nav.csv'),parse_dates=['date']).set_index('date').nav_liquidatable_eur for s in STRATS}
    d['Global_proxy_60_30_10']=pd.read_csv(REPORTS/'benchmark_nav.csv',parse_dates=['date']).set_index('date').nav_liquidatable_eur
    return pd.DataFrame(d).sort_index()

def subperiods(nav):
    out=[]
    for s in nav.columns:
        for start,end in [('2016-02-01','2020-12-31'),('2021-01-01','2025-12-10'),('2022-01-01','2025-12-10')]:
            x=nav[s].loc[start:end]
            if x.empty:continue
            # CAGR relative to actual at first date, not fresh initial capital.
            years=(x.index[-1]-x.index[0]).days/365.25
            cagr=(x.iloc[-1]/x.iloc[0])**(1/years)-1
            dd=(x/x.cummax()-1).min()
            out.append(dict(strategy=s,start=str(x.index[0].date()),end=str(x.index[-1].date()),cagr_pct=100*cagr,maxdd_pct=100*dd))
    pd.DataFrame(out).to_csv(REPORTS/'subperiods.csv',index=False)
    return pd.DataFrame(out)

def uncertainty(nav):
    monthly=nav.resample('ME').last();rr=np.log(monthly/monthly.shift(1)).dropna()
    rr.to_csv(REPORTS/'monthly_log_returns.csv')
    rng=np.random.default_rng(12345);n=len(rr);block=12;out=[]
    for alternative,base in [('Quant20_Momentum','Top10_Eligible_BH'),('Quant20_TrendVol','Top10_Eligible_MA6_12'),('Quant20_Momentum','Global_proxy_60_30_10')]:
        paired=(rr[alternative]-rr[base]).to_numpy();mean_pp=paired.mean()*12*100
        draws=np.empty(3000)
        for it in range(len(draws)):
            indexes=[]
            while len(indexes)<n:
                start=int(rng.integers(n));indexes.extend((start+j)%n for j in range(block))
            draws[it]=paired[np.array(indexes[:n])].mean()*12*100
        lo,hi=np.percentile(draws,[2.5,97.5]);out.append(dict(alternative=alternative,reference=base,months=n,
            annualized_log_excess_pp=mean_pp,ci95_low=lo,ci95_high=hi,bootstrap_block_months=block,
            caution='descriptive, no multiple-test correction, no blind holdout'))
    pd.DataFrame(out).to_csv(REPORTS/'uncertainty.csv',index=False)
    return pd.DataFrame(out)

def plot(nav):
    ax=nav.div(10000).plot(figsize=(11,6),lw=1.5)
    ax.set_title('Atlas Quant v1 — growth of €10,000 (hypothetical after-tax liquidation NAV)')
    ax.set_ylabel('Growth multiple of initial capital');ax.grid(alpha=.2)
    ax.figure.tight_layout();ax.figure.savefig(REPORTS/'equity_curves.png',dpi=160);plt.close(ax.figure)
    dd=nav/nav.cummax()-1
    ax=(dd*100).plot(figsize=(11,6),lw=1.35)
    ax.set_title('Historical drawdowns: Atlas Quant v1 (exploratory)')
    ax.set_ylabel('Drawdown (%)');ax.grid(alpha=.2)
    ax.figure.tight_layout();ax.figure.savefig(REPORTS/'drawdown_curves.png',dpi=160);plt.close(ax.figure)

def stress():
    base=pd.read_csv(REPORTS/'comparison.csv')
    alt=pd.read_csv(ROOT/'stress_2eur_0p3pct'/'comparison.csv')
    b=base[['strategy','cagr_pct','maxdd_pct','orders','fees_eur','slippage_eur']].rename(columns={'cagr_pct':'base_cagr_pct','maxdd_pct':'base_dd_pct','fees_eur':'base_fees_eur','slippage_eur':'base_slippage_eur'})
    a=alt[['strategy','cagr_pct','maxdd_pct','fees_eur','slippage_eur']].rename(columns={'cagr_pct':'stress_cagr_pct','maxdd_pct':'stress_dd_pct','fees_eur':'stress_fees_eur','slippage_eur':'stress_slippage_eur'})
    out=b.merge(a,on='strategy');out.to_csv(REPORTS/'stress_sensitivity.csv',index=False)
    return out

if __name__=='__main__':
    nav=load();print(subperiods(nav).to_string(index=False));print('\nUncertainty\n',uncertainty(nav).to_string(index=False))
    print('\nStress\n',stress().to_string(index=False));plot(nav)