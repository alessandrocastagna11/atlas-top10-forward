# Atlas Quant Fundamental PIT v1 — experimental, offline, no live changes

This is **separate research**, not the official Atlas live forward, not a portfolio recommendation.

- Source 1: public SEC EDGAR XBRL companyfacts. Every financial observation retains **filing_date**, accession, fiscal period end, and value; reporting-date look-ahead is blocked. Old-period restatements filed later are not granted to earlier decisions. Current SEC ticker→CIK mapping is *not* historically certified.
- Source 2: existing user-provided historical Top 200 reconstructed membership and 2014–2025 USD stock price archive. **NOT certified PIT membership**; delistings, ticker map, historical sector membership and adjusted vendor price survivorship remain significant limitations.
- Source 3: Yahoo corporate split events to reconcile retroactively split-adjusted OHLC with **as-filed per-share** denominators. If split record missing the ticker is excluded from Value scores. Vendor splits are not a substitute for issuer-level split audits.
- Source 4: existing EUR/USD FX and synthetic global 60/30/10 benchmark. **NOT historical IUSQ**.
- Since independent PIT analyst consensus snapshots were not available, **earnings revision factors are NOT tested**. Earnings growth is not mislabeled as revisions.

## Research design

2016-02 through 2025-11, monthly mark-to-market and **quarterly selection** (February/May/August/November) using financial filings **known by the prior month-end**. Trades at first available next-month historical OPEN, FX from preceding day (preprocessed), split-adjusted share units. No leverage. Same US SEC 10-K / historical USD stock / complete SEC facts eligible universe for all tested strategies to avoid data-coverage mismatch.

Predeclared variants: Top 10 US eligible buy/hold, Top 10 + MA6/12, Quant20 Price, Quant20 Quality+Value, Quant20 Quality+Value+Momentum (QVM), QVM + MA6/12. Factors: quality (ROA, CFO/assets, margin, equity/assets), value (as-filed earnings yield and book-to-price after splits), price momentum (12–1, 6–1, volatility). Maximum six names/sector and 20 names, no repeated equal weight rebalance of winners. No order-count limit. Includes 26% realized gain tax after 4-year loss carry, 26% proxy dividend tax, 0.2% annual bollo, 1€/order and 0.1% slippage. NAV is **hypothetical liquidation value net of taxes**.

**Not production-grade**: SEC only US 10-K companies; 20-F foreign filers absent; quality/valuation comparability is limited across accounting sectors; outstanding-share denominator can be fragile for dual-class structures and ADRs; dividends approximated by retrospective adj-close ratio at ex-date; monthly drawdowns understate intramonth tails; annual rank is reconstructed, not certified PIT; future revisions in other data sources not certified. No transaction-cost market impact model.

Run in a controlled network-connected environment:

```
python -m pip install -r requirements.txt
python -m unittest test_pit -v
python sec_fetch.py
python pit_backtest.py
```

The scripts are isolated under `research/atlas_fundamental_pit_v1` on **research/atlas-quant-v1**. The workflow only commits into that same research branch, never updates `main`, and never changes existing Atlas positions, ledger, forward scripts or recurring schedules.