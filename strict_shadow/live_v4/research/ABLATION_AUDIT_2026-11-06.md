# Atlas V4 Confidence70 — historical ablation audit (2026-11-06)

**Status: completed OFFLINE RESEARCH. No forward strategies or real broker orders modified.** Dataset 2012-01-03 to 2025-12-10. Source archive: `Atlas_V4_Confidence70_Recovered_Backtest_2026-11-05.zip`; SHA256 `2cba7228ba3f7d339c335f7a17a18a55e4d33a41b88c854375d0ad129a1b002a`. The archive is in the user's file Library, not stored in this GitHub repository.

Original Confidence70 frozen reference was CAGR 10.90209%, maxDD -13.579045%, 720 orders; **reference is not independently reproduced exactly**. The reconstructed beta=0.70 replay was independently rerun and reproduced its archived equity to **EUR0.00** maximal difference and all **720 order rows**.

## Full replayed ablations (initial EUR100,000)

| Variant | CAGR net % | MaxDD % | Orders | True average ROW stock exposure % |
|---|---:|---:|---:|---:|
| Reconstructed Confidence70 | 10.87785 | -13.50882 | 720 | 8.191 |
| US regional slots only, ROW fallback almost zero | 9.97515 | -14.01719 | 677 | 0.005 |
| Strict USA stocks, no ROW stocks | 9.97515 | -14.01718 | 677 | 0.000 |
| Fixed 2 ROW stock slots | 8.00661 | -18.90838 | 726 | 13.173 |
| Confidence sizing beta 0 instead of 0.70 | 9.44599 | -13.81156 | 602 | 6.567 |
| No regime/t8 timing, volatility target retained | 12.49825 | -24.82213 | 635 | 10.059 |
| Neutral static issuer priority (seed 240421) | 4.91204 | -15.55074 | 717 | 9.748 |
| Neutral static issuer priority (seed 42) | 7.19907 | -16.70662 | 628 | 9.246 |
| Neutral static issuer priority (seed 777) | 5.30213 | -15.47510 | 693 | 9.727 |

Additional **EUR10,000 initial account**, same Confidence70 algorithm and EUR1 fixed fees: **CAGR 10.67761%, maxDD -13.55792%, EUR41,115.15 ending, 720 orders**. Do NOT equate the EUR100k CAGR with an EUR10k account.

## Interpretations & limitations

* Market-regime and weekly t8 overlays substantially reduce *historical* maximum drawdown (from -24.82% to -13.51%), but lower CAGR from 12.50% to 10.88%. The experiment *retains* target volatility, portfolio limits and taxes, hence it does not remove all risk management.
* Beta 0.70 adds 1.43pp in-sample CAGR vs beta 0 sizing but increases trades by 118; 12-month block-bootstrap difference of annualized **log** returns has an indicative 95% interval **[-0.97,+2.83] pp** (includes zero). This is NOT evidence beta=0.70 will win out of sample.
* Dynamic ROW budget beats an always-fixed two-slot ROW allocation in sample, but benefits vs US-only are not stable: CAGR 2019–2025 is **9.14%** for original vs **9.44%** for ROW slots disabled. Estimated bootstrap interval for baseline–US slots disabled is **[-0.80,+2.68] pp** (includes zero).
* Three neutral-priority seed tests are all worse, but keep the original trend/momentum/liquidity *eligibility* filters, so this is not a fully factor-neutral control. Not causal proof of stock-selection alpha.
* **Found reporting-classification defect:** original archived `avg_row_weight_pct` counted US ETF `SPY` as ROW because it was not in the 197 canonical USA stock columns. Corrected true baseline ROW stock weight is **8.19%**, while SPY defensive sleeve averages **2.87%**. Results and orders are not affected; only the ROW attribution.
* Historical ROW FX was constructed from interpolated annual-average rates with constant fallback for SAR/SEK/TWD, permitting look-ahead. Global membership/market-cap ranks and delistings are not independently PIT-certified. This is not a production-ready historical audit.
* Order rate approximately **4.3 per month**, greater than a 2–3/month target. No optimization or strategy changes were promoted.

**Decision:** Keep live V4 strict and cohort proxy separate. Do not alter forward investment rules based on these ex-post tests. Rebuild authentically daily, timestamped PIT currency and issuer rankings, restore exact original replay parity, then independently validate outside the discovery sample.

Reproduction scripts and full data/results tables are available in the downloadable research bundle produced in this conversation; not committed to GitHub because large historical binary data are private and must be verified separately.
