"""Reproducible sensitivity tests, chronological diagnostics and chart.
No data downloader, all used inputs archived in this release.
"""
from pathlib import Path
import numpy as np,pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import backtest_v08_proxy as b

OUT=Path(__file__).resolve().parent
r,m,e,bench,cash,div=b.v5.load_same_universe()
base_slip=b.SLIPPAGE;base_fee=b.FEE
rows=[]
for scenario,slip,fee in [('base',.001,1.),('slippage_3x',.003,1.),('fee_2eur',.001,2.)]:
    b.SLIPPAGE=slip;b.FEE=fee
    for variant in ('BuyHold_Top10','MA10','MA6_MA12','Adaptive_Risk'):
        nav,o,_,_,stats=b.run_variant(variant,r,m,e,bench,div)
        stats0=b.performance(nav)
        rows.append(dict(scenario=scenario,strategy=variant,slippage_side_pct=slip*100,fee_eur=fee,
                        **stats0,orders=stats['orders'],realized_tax_eur=stats['tax_realized_eur']))
b.SLIPPAGE=base_slip;b.FEE=base_fee
pd.DataFrame(rows).to_csv(OUT/'SENSITIVITY.csv',index=False)

curve=pd.read_csv(OUT/'EQUITY.csv',parse_dates=['date']).set_index('date')
sub=[]
for name in curve.columns:
    for a,z in [('2016-02-01','2021-12-31'),('2022-01-01','2025-12-10'),('2020-01-01','2020-12-31'),('2022-01-01','2022-12-31')]:
        s=curve.loc[a:z,name]
        years=(s.index[-1]-s.index[0]).days/365.25
        sub.append(dict(strategy=name,start=a,end=z,cagr_pct=100*((s.iloc[-1]/s.iloc[0])**(1/years)-1),
                        max_dd_pct=100*(s/s.cummax()-1).min(),start_nav_eur=s.iloc[0],end_nav_eur=s.iloc[-1]))
pd.DataFrame(sub).to_csv(OUT/'SUBPERIODS.csv',index=False)
monthly=curve.resample('ME').last().pct_change().dropna()
vs=[]
for name in curve.columns:
 if name=='Global_proxy_60_30_10':continue
 n=len(monthly)
 wins=int((monthly[name]>monthly['Global_proxy_60_30_10']).sum())
 vs.append(dict(strategy=name,months=n,win_vs_bench=wins,win_pct=100*wins/n))
pd.DataFrame(vs).to_csv(OUT/'MONTHLY_WINS.csv',index=False)

# Moving-block bootstrap CI of *paired* relative log return: deliberately a
# broad exploratory uncertainty assessment, not a proof of causal alpha.
logrel=np.log1p(monthly['MA6_MA12'])-np.log1p(monthly['Adaptive_Risk'])
rng=np.random.default_rng(12345);a=logrel.to_numpy();L=12;n=len(a);res=[]
for i in range(2500):
 idx=[]
 while len(idx)<n:
  start=int(rng.integers(0,n-L+1));idx.extend(range(start,start+L))
 idx=idx[:n];res.append(np.mean(a[idx])*12*100)
ci=np.quantile(res,[.025,.975]);
(OUT/'UNCERTAINTY.md').write_text(f'''# Moving-block bootstrap (esplorativo)\n\nPeriodo: {n} rendimenti mensili accoppiati. Differenza di log-rendimento annualizzato MA6/MA12 − Adaptive Risk: {np.mean(a)*1200:.2f} punti percentuali; IC percentile 95% da 2500 campioni di blocchi consecutivi da {L} mesi: **[{ci[0]:.2f}, {ci[1]:.2f}]** pp. Seed RNG 12345. Non adeguato a conclusioni causali, non corregge scelta ripetuta di modelli né i problemi PIT/OHLC.\n''',encoding='utf-8')

fig,ax=plt.subplots(figsize=(11,6))
for label in ('Global_proxy_60_30_10','BuyHold_Top10','MA10','MA6_MA12','Adaptive_Risk'):
    ax.plot(curve.index,curve[label],label=label,linewidth=1.4)
ax.set_title('Atlas Top10 — Backtest v0.8 di ricerca (esecuzione CLOSE proxy; non OPEN)')
ax.set_ylabel('Valore liquidabile dopo tasse stimate (€)')
ax.grid(alpha=.3);ax.legend(fontsize=9);fig.tight_layout();fig.savefig(OUT/'EQUITY_COMPARISON.png',dpi=155);plt.close(fig)

fig,ax=plt.subplots(figsize=(11,5))
for label in ('BuyHold_Top10','MA10','MA6_MA12','Adaptive_Risk'):
    ax.plot(curve.index,(curve[label]/curve[label].cummax()-1)*100,label=label,linewidth=1.3)
ax.set_title('Drawdown storico delle strategie (CLOSE proxy)')
ax.set_ylabel('Drawdown %');ax.grid(alpha=.3);ax.legend(fontsize=9);fig.tight_layout();fig.savefig(OUT/'DRAWDOWN_COMPARISON.png',dpi=155);plt.close(fig)

print(pd.DataFrame(rows)[['scenario','strategy','cagr_pct','max_dd_pct','orders']].to_string(index=False));print('CI',ci)