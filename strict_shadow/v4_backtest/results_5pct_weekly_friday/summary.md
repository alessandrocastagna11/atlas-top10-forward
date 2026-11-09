# Atlas V4 — historical current-cohort diagnostic (NOT PIT)

**WARNING: this is NOT a verified historical point-in-time backtest.**
It holds a 6 Nov 2026 ETF holdings roster fixed into 2025-2026.
That introduces selection/survivorship and look-ahead bias; CAGR is not evidence of forward alpha.

| Metric | V4 Cohort Proxy | ACWI buy-and-hold EUR (price-only) |
|---|---:|---:|
| Window | 2025-03-03 to 2026-11-06 | Same |
| Start EUR | 10,000 | 10,000 |
| End EUR | 12,524.66 | 11,820.85 |
| Total return | 25.247% | 18.208% |
| CAGR | 14.354% | 10.481% |
| Max drawdown | -22.599% | -17.331% |

Orders: 152 (80 buys, 72 sells), fees EUR 152.0, estimated realized taxes EUR 1619.28, average exposure 68.62%.
Signals generated: 82; processed: 69. Rebalance threshold 5% NAV, decision cadence weekly_friday.

Blocked/skipped: {"missing_execution_open_or_prior_fx": 13, "signal_after_last_session": 1}

**Limitations:** historical membership and first-seen FX vintage are not independently PIT; 2026 holdings selected retrospectively; no historical delisting complete accounting; dividend-unadjusted ACWI benchmark; no inference of real-world implementability. This does not reproduce the frozen historical original Confidence70.
The only genuinely forward observed period begins November 2026.
