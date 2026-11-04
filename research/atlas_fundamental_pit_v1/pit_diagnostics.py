#!/usr/bin/env python3
"""Audits and robustness for Atlas SEC as-filed research; no live writes."""
from pathlib import Path
import csv,json,math
import numpy as np,pandas as pd
import pit_backtest as P
ROOT=Path(__file__).resolve().parent
REPORTS=ROOT/'reports'
MODES=P.MODELS

def rate(s):
    s=s.astype(float);yrs=(len(s)-1)/12.
    if yrs<=0:return np.nan
    return 100*((float(s.iloc[-1])/float(s.iloc[0]))**(1/yrs)-1)

def subperiods():
    outs=[]; nav={}
    for mode in MODES:
        d=pd.read_csv(REPORTS/(mode+'_nav.csv')).set_index('month')
        nav[mode]=d.NAV_after_hypothetical_liquidation_tax.astype(float)
        for tag,lo,hi in [('2016–2021','2016-02','2021-12'),('2022–2025','2021-12','2025-11')]:
            ss=nav[mode].loc[lo:hi]
            outs.append({'strategy':mode,'interval':tag,'start':lo,'end':hi,
             'cagr_net_pct':round(rate(ss),4),'maxdd_monthly_pct':round(100*(ss/ss.cummax()-1).min(),4),
             'start_nav_eur':round(float(ss.iloc[0]),2),'end_nav_eur':round(float(ss.iloc[-1]),2)})
    pd.DataFrame(outs).to_csv(REPORTS/'subperiods.csv',index=False)
    return nav

def bootstrap(nav):
    rng=np.random.default_rng(20261104)
    comparisons=[('Quant20_QualityValue','Quant20_Price'),('Quant20_QVM','Quant20_Price'),('Quant20_Price','Top10_US_BuyHold'),('Quant20_QVM_MA6_12','Quant20_QVM')]
    outputs=[]
    for a,b in comparisons:
        x=np.log(nav[a].to_numpy()[1:]/nav[a].to_numpy()[:-1])-np.log(nav[b].to_numpy()[1:]/nav[b].to_numpy()[:-1])
        n=len(x);block=12;k=int(np.ceil(n/block));means=[]
        for i in range(2000):
            starts=rng.integers(0,n,size=k)
            series=np.concatenate([x[np.arange(s,s+block)%n] for s in starts])[:n]
            means.append(series.mean()*1200)
        lo,hi=np.quantile(means,[.025,.975]);outputs.append({'strategy':a,'comparator':b,
          'annualized_log_return_diff_pp':round(float(x.mean()*1200),3),'bootstrap_ci_95_low_pp':round(float(lo),3),
          'bootstrap_ci_95_high_pp':round(float(hi),3),'monthly_observations':n,
          'method':'circular block bootstrap 12 months; 2000 replications; exploratory; NOT multiplicity corrected'})
    pd.DataFrame(outputs).to_csv(REPORTS/'relative_uncertainty.csv',index=False)
    return outputs

def stress():
    m,f,p,b=P.input_data()
    oldfee,oldslip,oldout=P.FEE,P.SLIP,P.REPORTS
    folder=REPORTS/'stress_fee2_slippage0p3'
    try:
        P.FEE=2.0;P.SLIP=.003;P.REPORTS=folder
        r,_=P.output(m,f,p,b)
        return r
    finally:P.FEE,P.SLIP,P.REPORTS=oldfee,oldslip,oldout

def write_report(stress_vals, uncertainty):
    standard=pd.read_csv(REPORTS/'comparison.csv')
    aud=json.loads((REPORTS/'audit.json').read_text())
    coverage=pd.read_csv(REPORTS/'coverage.csv')
    stress_file=pd.read_csv(REPORTS/'stress_fee2_slippage0p3'/'comparison.csv')
    out=['# Atlas Quant SEC Fundamental PIT v1.1 — Historical Research','',
       '**THIS STUDY DOES NOT CHANGE OR RUN THE LIVE FORWARD.**','',
       '## Provenance and point-in-time controls','',
       '- SEC EDGAR XBRL `companyfacts` annual 10-K/10-K/A values, filtered by exact `filed` date and accession before each historical decision. Later-filed restatements are not backfilled into past decisions.',
       '- SEC + Yahoo historical corporate split events used to reconcile as-filed outstanding shares with retroactively split-adjusted prices. Companies without verified split history are excluded.',
       '- Dataset is a US-dollar/SEC 10-K subset drawn from reconstructed annual Top200. **The roster is NOT historically certified PIT and delisted/missing stocks may introduce survivorship bias.**',
       '- Price archive uses adjusted Yahoo historic OHLC; FX uses archive rates; dividends are vendor-derived estimates; benchmark is synthetic global 60/30/10 EUR, not an IUSQ history.',
       '- No historical analyst EPS estimates were licensed; **analyst revisions have NOT been modeled**. Financial reports and EPS surprises are not interchangeable with analysts\' forecast revisions.',
       '- Results: FEB 2016–NOV 2025, monthly NAV/MaxDD (intramonth drawdowns can be worse); trading decisions based solely on previous month-end, execution estimated at first monthly OPEN in archived data.',
       '- Factors: ROA, operating margin, CFO/assets, equity/assets, earnings yield and book-to-market from SEC as-filed; momentum from lagged adjusted prices. All models use identical SEC-complete eligible universe.',
       f'- Audit: {aud["n_decision_snapshots"]} frozen quarterly selection months; min/median SEC-complete universe {aud["min_sec_coverage"]}/{aud["median_sec_coverage"]}; all filing dates <= decision date: **{aud["max_sec_pub_date_le_decision"]}**.',
       '', '## Equal-input comparison — taxed estimated CAGR and monthly drawdown','',
       '| Strategy | CAGR net | Monthly MaxDD | Orders | Liquidatable final € |','|---|---:|---:|---:|---:|']
    for _,r in standard.iterrows():
        out.append(f'| {r["model"]} | {r["cagr_pct"]:.2f}% | {r["maxdd_monthly_pct"]:.2f}% | {int(r["orders"])} | {r["final_liquidatable_eur"]:,.0f} |')
    out+=['','## Transaction-cost sensitivity','',
        'Same SEC snapshots and selections, but **€2 per order and 0.30% slippage each side**, rather than €1 and 0.10%.','',
        '| Strategy | Base CAGR | Higher-cost CAGR | Difference (pp) |','|---|---:|---:|---:|']
    for _,r in standard.iterrows():
        st=stress_file[stress_file.model==r['model']]
        if len(st)==0:continue
        new=float(st.iloc[0].cagr_pct)
        out.append(f'| {r["model"]} | {r["cagr_pct"]:.2f}% | {new:.2f}% | {new-r["cagr_pct"]:+.2f} |')
    out+=['','## Block-bootstrap comparison (NOT multiple-testing corrected)','',
        '| Contrast | Log-return difference pp annualized | 95% circular block-bootstrap interval (pp) |',
        '|---|---:|---:|']
    for x in uncertainty:
        out.append(f'| {x["strategy"]} vs {x["comparator"]} | {x["annualized_log_return_diff_pp"]:+.2f} | [{x["bootstrap_ci_95_low_pp"]:+.2f}, {x["bootstrap_ci_95_high_pp"]:+.2f}] |')
    out+=['','## Critical limitations and conclusion','',
       'The latest SEC filings stored by SEC are not an independent immutable archive of all historic issuer mappings. These names were identified using today\'s SEC ticker table, so **fully certified issuer-level PIT identity and historical investability is not established**. The historic Top200 membership itself was reconstructed from a separate provider; some companies lack filings. A positive historical CAGR is not proof of alpha.',
       'The nominal price/accounting assumptions differ from real broker fills, Italian withholding on foreign dividends, settlement, and monthly vs daily drawdowns. Tax modeling is an estimate.',
       'The 2022–2025 subperiod has been studied repeatedly in Atlas; **it is not blind out of sample**. Additional optimizing on these returns would increase overfitting risk.',
       '**Do not promote the Quality/Value or QVM variants to live from these results. The two existing forward engines continue unchanged.**',
       '', 'Raw metrics: `comparison.csv`, `subperiods.csv`, `relative_uncertainty.csv`, `coverage.csv`, `audit.json`, and raw orders and security-level factor audit.']
    (REPORTS/'FINAL_REPORT.md').write_text('\n'.join(out)+'\n')

def main():
    nav=subperiods()
    u=bootstrap(nav)
    stress_vals=stress()
    write_report(stress_vals,u)
    print('DIAGNOSTICS_STRESS',json.dumps({m:{'cagr':v['cagr_pct'],'orders':v['orders']} for m,v in stress_vals.items()}))
    print('DIAGNOSTICS_UNCERTAINTY',json.dumps(u))
if __name__=='__main__':main()