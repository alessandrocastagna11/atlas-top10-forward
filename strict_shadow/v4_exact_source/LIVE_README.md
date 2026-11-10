# Atlas V4 Confidence70 original-source PAPER forward

This is NOT `V4_CONF70_FORWARD_COHORT_PROXY_V1` and cannot use its historic CAGR
as evidence of live returns. It executes the original 2012–2025
`RegionSlotAtlas` class (verbatim, audited SHA), original H4 two-factor OLS
routines, robust cross-sectional scoring and path/alpha selectors, V2 continuous
SPY regime, T8 overlay and T9 tax-aware simulator. It changes the simulator's
**end-of-sample forced liquidation** to `False`: that closing transaction is an
end-of-backtest accounting operation and must not become a live order.

Capital starts from **EUR 10,000 PAPER on 2026-11-10**, on a standalone ledger.
The legacy Cohort Proxy and Strict inbox are never written. The validated
2012–2025 **35-day backtest variant**, rather than the earlier unlocked baseline,
is the executable policy: every purchase becomes a FIFO lot, and **NO sale is
allowed before 35 calendar days**, even defensive reductions and SPY positions.
The original five-week full-selection cadence, weekly risk monitoring, ten-stock,
15%-issuer/30%-sector caps, €500 core-order minimum, 6% rebalancing band and 1%
cash reserve are retained. No hard limit on monthly order count.
The new 35-day gate is identical in mechanics to the 2012–2025 replay; its past
figures are not certified predictions for the current ACWI-proxy universe.
No cap on monthly order count. Broker API operations are impossible.

## Transparent market-data exception (NOT the historical universe)

The historical 2012–2025 original ranked PIT USA and RoW universes; those exact
2026 market-cap ranks were not supplied. The daily feed retains timestamped iShares ACWI 500 holdings as a fallback
candidate pool. It also captures public issuer capitalizations in USD with
the UTC first-observed time and explicit ticker/issuer/country crosswalks.
When a captured cap table covers at least 100 US and 200 non-US companies,
the chooser switches to **market-cap ordering within that ACWI candidate
pool**, effective only from the observation day and never retrospectively.
During source outages the last qualifying cap snapshot remains eligible until
the 10-day staleness limit. This still is NOT the unrestricted original
historical point-in-time market-cap universe or a certified audit of share counts. No retroactive membership backfill
can create a trade. Prices are Yahoo adjusted daily OHLC with exchange-local
session dates and FX per date; SPY and original sector ETF factors are required.
Incomplete source data blocks trading without creating synthetic quotes.

The original model is refreshed from raw market data before decisions; an
append-only ledger is checked against every previously stored order, and a
pre-opening attestation must already exist before any new simulated fill.
If re-stated data change a prior decision or the next global open has already
begun, no new order is committed. The code runs on GitHub Actions; job success
means the check ran, NOT that the strategy placed any paper orders.

Daily human-readable report: `forward/latest.md`; machine-readable holdings,
cash and FIFO maturity dates: `forward/portfolio.json`; order ledger:
`forward/orders.csv`; dated NAV series: `forward/equity.csv`;
model state: `forward/state.json`; operational/pending status: `forward/latest_status.json`;
source-locked pre-open records: `forward/preopen/`. Empty accounts retain
€10,000 cash and zero orders; missing prices never become invented fills.
Each membership vintage is effective only from its own **recorded date**.
Daily prices, FX, and issuer-cap observations are archived without changing
past rosters or previously registered paper orders. The new cap-ranked
candidates are still constrained by the ACWI eligibility pool; results remain
a research proxy, and 2012-2025 performance cannot be claimed as proven for
this updated universe.

This is a development deployment: **2026 source-code and input-to-signal parity
with the 2012–2025 frozen universe is not certified**. The actual reference
backtest starts 2012-01-03 and ends 2025-12-10.
