# Atlas V4 Confidence70 — exact original historical engine source

This folder preserves the **seven original Python source files byte-for-byte** from
`Atlas_V4_Confidence70_Recovered_Backtest_2026-11-05.zip`.
`readiness.py` SHA-256 checks each original, and the offline replay (2012–2025)
produced 710 operational trades plus 10 synthetic final liquidation orders
for a €10,000 virtual account, with ~10.71% CAGR / ~-13.63% max DD.

**NOT YET LIVE.** Source parity alone is not signal parity. The original source
reads frozen USA canonical bundle and prepared global panels ending **2025-12-10**.
The current online dataset tracks 2024–2026 prices, but it does **not** provide
audited 2026 point-in-time global200 and USA100 member/rank panels equivalent to
the historical engine's inputs, nor the recomputed H4 residual/factor, T8 and
panic-rebound panels. The 2026 ACWI current-cohort ETF snapshot is **not**
a compatible substitute. No extrapolated picks or trades are permitted.

`python strict_shadow/v4_exact_source/readiness.py` emits a machine-readable
report in `reports/latest_status.json`; it fails if frozen source changes. It
reports `PENDING_EXACT_2026_PIT_ENGINE_ADAPTER` until a separate full exact
source-driven forward producer is built and validated. The existing `live_v4`
strict EUR10k cash portfolio remains isolated. Cohort Proxy is research only,
not the backtested original. Neither can submit real-money orders.

A complete exact live launch needs:
1. Date-vintaged USA Top100 and ROW Global200 ranks/memberships, free of future
   hindsight; market price/FX/corporate-action feed with as-of audit.
2. Rebuilt 2026 features using this checked-in frozen engine, including original
   OLS factor residuals, expected-alpha gate, sector/correlation veto, risk overlay,
   regional slot logic and confidence beta 0.70, minimum holding, tax lots.
3. Numerical replay validation on a common historical range and matching trade
   list, then a strict one-step forward execution test with immutable provenance.
4. Only then publish original-engine decisions to `live_v4/inbox/latest_signal.json`
   and let the existing strict paper runner consume them. Do not seed with proxy.

All original source is kept as-is; do not rewrite its rules in the name of
performance optimization without a separate, validated experiment.
