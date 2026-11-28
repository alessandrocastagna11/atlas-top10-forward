# Atlas DataHub — independent EOD audit

Independent equity-provider integration remains audit-only on the original V4 paper branch. It never replaces Yahoo adjusted prices, changes orders, or publishes secondary vendor quotes. ECB reference comparisons work without a new key when the existing ECB feed is available. Third-party providers are disabled by default.

## Optional setup
Only use the providers whose plan and data license permit your workflow, including derived audit reporting in this repository. Do not upgrade or incur charges for this feature.
In Settings → Secrets and variables → Actions:
- Add repository variable ATLAS_MULTISOURCE_OPT_IN = 1.
- Add repository secrets MASSIVE_API_KEY, TWELVE_DATA_API_KEY, EODHD_API_KEY, FMP_API_KEY as desired. Never put keys in code, logs, or messages.
- Start with Massive; it supplies a grouped US daily comparison once per run after midnight New York. Twelve Data samples up to five tickers or FX pairs; EODHD samples up to four explicitly mapped non-US listings, now with up to 150 days of adjusted-return overlap diagnostics; FMP samples up to four US symbols. Manual runs share the provider quotas. Provider access and terms vary.
- ECB reference data is compared with the same-date Yahoo FX close at 2% diagnostic tolerance, not used as a replacement fixing.
- Equity tolerance is 1%, diagnostic only. Differences can arise from split, venue, session and rounding conventions. London EODHD mapping is deferred pending GBp/GBP verification.
- Stooq is deferred pending automated access and license validation.

Inspect forward/latest_status.json → multisource_audit. A missing key or no comparisons must not be interpreted as independent validation. Audit disagreement is an advisory warning, not an automatic trade or a price substitution. Optional provider failures are isolated. A missing primary adjusted quote still blocks the original engine. No secondary-provider raw quotes or response bodies are persisted; only the audit outcomes are recorded.

V6.6 Hybrid and Top5 consume the same original V4 input snapshot and now propagate upstream data audits, rejecting explicit freshness failures or audit/session mismatch. Full alternate-source adjusted-history parity tests remain unavailable without provider credentials. Before enabling automatic fallback, prove exact listing, currency, dates, split/dividend adjustments, history length and causal observation provenance. No historical PIT certification is claimed.

## Automatic reciprocal FX recovery
If a required direct Yahoo EUR cross fails, try its inverse only with a verified direct cache, at least 60 overlapping sessions (up to 120), 300 historical observations, maximum relative error 1%, median 0.2%, exact currencies and latest expected date. Append only up to three consecutive missing weekday observations, preserving historical direct prices. This is same-provider recovery, not an independent feed. Provenance is reported in fx_recovery_audit. Equity-provider adjusted histories are compared for diagnostic purposes only. Do not enable automatic equity substitution based solely on a short overlap or a matching last price.
