# Atlas Quant SEC Fundamental PIT v1.1 — Historical Research

**THIS STUDY DOES NOT CHANGE OR RUN THE LIVE FORWARD.**

## Provenance and point-in-time controls

- SEC EDGAR XBRL `companyfacts` annual 10-K/10-K/A values, filtered by exact `filed` date and accession before each historical decision. Later-filed restatements are not backfilled into past decisions.
- SEC + Yahoo historical corporate split events used to reconcile as-filed outstanding shares with retroactively split-adjusted prices. Companies without verified split history are excluded.
- Dataset is a US-dollar/SEC 10-K subset drawn from reconstructed annual Top200. **The roster is NOT historically certified PIT and delisted/missing stocks may introduce survivorship bias.**
- Price archive uses adjusted Yahoo historic OHLC; FX uses archive rates; dividends are vendor-derived estimates; benchmark is synthetic global 60/30/10 EUR, not an IUSQ history.
- No historical analyst EPS estimates were licensed; **analyst revisions have NOT been modeled**. Financial reports and EPS surprises are not interchangeable with analysts' forecast revisions.
- Results: FEB 2016–NOV 2025, monthly NAV/MaxDD (intramonth drawdowns can be worse); trading decisions based solely on previous month-end, execution estimated at first monthly OPEN in archived data.
- Factors: ROA, operating margin, CFO/assets, equity/assets, earnings yield and book-to-market from SEC as-filed; momentum from lagged adjusted prices. All models use identical SEC-complete eligible universe.
- Audit: 40 frozen quarterly selection months; min/median SEC-complete universe 43/74.0; all filing dates <= decision date: **True**.

## Equal-input comparison — taxed estimated CAGR and monthly drawdown

| Strategy | CAGR net | Monthly MaxDD | Orders | Liquidatable final € |
|---|---:|---:|---:|---:|
| Top10_US_BuyHold | 17.96% | -29.41% | 48 | 50,046 |
| Top10_US_MA6_12 | 13.74% | -20.09% | 84 | 35,085 |
| Quant20_Price | 13.23% | -13.04% | 684 | 33,587 |
| Quant20_QualityValue | 11.63% | -15.66% | 294 | 29,242 |
| Quant20_QVM | 10.28% | -14.46% | 530 | 25,952 |
| Quant20_QVM_MA6_12 | 8.34% | -11.54% | 512 | 21,843 |
| Synthetic_Global_Benchmark | 10.05% | -16.06% | 1 | 25,434 |

## Transaction-cost sensitivity

Same SEC snapshots and selections, but **€2 per order and 0.30% slippage each side**, rather than €1 and 0.10%.

| Strategy | Base CAGR | Higher-cost CAGR | Difference (pp) |
|---|---:|---:|---:|
| Top10_US_BuyHold | 17.96% | 17.82% | -0.14 |
| Top10_US_MA6_12 | 13.74% | 13.51% | -0.23 |
| Quant20_Price | 13.23% | 12.30% | -0.93 |
| Quant20_QualityValue | 11.63% | 11.17% | -0.46 |
| Quant20_QVM | 10.28% | 9.53% | -0.75 |
| Quant20_QVM_MA6_12 | 8.34% | 7.59% | -0.76 |
| Synthetic_Global_Benchmark | 10.05% | 10.01% | -0.04 |

## Block-bootstrap comparison (NOT multiple-testing corrected)

| Contrast | Log-return difference pp annualized | 95% circular block-bootstrap interval (pp) |
|---|---:|---:|
| Quant20_QualityValue vs Quant20_Price | -1.66 | [-5.73, +2.51] |
| Quant20_QVM vs Quant20_Price | -2.80 | [-5.74, +0.21] |
| Quant20_Price vs Top10_US_BuyHold | -4.03 | [-11.33, +3.25] |
| Quant20_QVM_MA6_12 vs Quant20_QVM | -1.90 | [-4.20, +0.41] |

## Critical limitations and conclusion

The latest SEC filings stored by SEC are not an independent immutable archive of all historic issuer mappings. These names were identified using today's SEC ticker table, so **fully certified issuer-level PIT identity and historical investability is not established**. The historic Top200 membership itself was reconstructed from a separate provider; some companies lack filings. A positive historical CAGR is not proof of alpha.
The nominal price/accounting assumptions differ from real broker fills, Italian withholding on foreign dividends, settlement, and monthly vs daily drawdowns. Tax modeling is an estimate.
The 2022–2025 subperiod has been studied repeatedly in Atlas; **it is not blind out of sample**. Additional optimizing on these returns would increase overfitting risk.
**Do not promote the Quality/Value or QVM variants to live from these results. The two existing forward engines continue unchanged.**

Raw metrics: `comparison.csv`, `subperiods.csv`, `relative_uncertainty.csv`, `coverage.csv`, `audit.json`, and raw orders and security-level factor audit.
