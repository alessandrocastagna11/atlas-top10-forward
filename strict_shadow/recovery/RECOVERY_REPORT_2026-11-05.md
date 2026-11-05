# Atlas V4 Confidence70 — Recovery report (2026-11-05)

**Historical model inputs RECOVERED. Research replay PERFORMED. Exact original parity NOT claimed.**

## Original source archives located in the user's ChatGPT Library
- `H4Atlas_DataHub_1.0.1_FIXED.zip`, SHA-256 `2e32a219ba5e8b3e32d9c25343638c0dc9d9a12e725c449996407696493ff56d`.
- `ATLAS_V4_REBUILT_RECOVERY_2026-10-06.zip`, 166,906,021 bytes, SHA-256 `671bcc0cf48afa294d3de9a710e3e5c70cfb3e66d3ea466c4d0f646ea3fb38c8`.
- `H4Atlas_0.18.0_V4_CONFIDENCE70_CLEAN_2026-10-07.zip` contains the production reference contract.

## Restored original research dataset
- 284 original compressed per-issuer Global price files, covering variable windows in 2010–2025. Existence does not imply continuous price history.
- 3,000 Global200 yearly PIT-labelled memberships (2012–2026), SHA-256 `535f47e79beb484a4cc7c03f0ddb2e87290b0c2956774cb0ead1c554bf59ebb8`.
- 33,600 Global200 monthly membership rows `u300_monthly_membership.csv`.
- 85,070 USA monthly S&P500 membership/rank rows `usa_monthly_rank_rebuilt.csv`; the `rank` column is NOT verified to represent market-cap ranks.
- `prepared_uniform_world.pkl.gz` (36.7 MiB), SHA-256 `0f430238ad4d8e679b7a81fab810c80a37b72a5ad7e9d9c3c1ac3e3ca1a1fb6b`.
- `H4_CANONICAL_R1_INPUT_BUNDLE.pkl.gz` (85.1 MiB), SHA-256 `fb1321e534d21059fc4f151e8027071106cc1899ef8b830a249ab61d6d8be2d3` (matches the historical USA control input expected by the recovered code).
- Recovered native Python execution engine.

## Actual regenerated 2012–2025 Confidence70 backtest (2026-11-05)

| Metric | Frozen target | Re-executed research model |
|---|---:|---:|
| CAGR | 10.90209% | 10.87785242% |
| Max drawdown | −13.579045% | −13.50881516% |
| Sharpe excess cash | 0.881483 | 0.87934882 |
| End value from EUR 100k | EUR 422,926.43 | EUR 421,640.18 |
| Orders | 720 | 720 |

The re-executed output was regenerated from input panels by the recovered V4 engine, with the cross-sectional strength beta changed to 0.70 **after** the baseline USA control validation. Only `UNIFIED_REGION_THRESHOLD_ROTATE` in the comparison was freshly regenerated; other rows are archived references. Small residual RoW feature/ranking differences are documented in the original recovery.

## Scientific limitations
Historical FX used by the RoW prepared panel is annual-mean smoothed/interpolated, with constant fallbacks for certain currencies; this can create look-ahead risk and is NOT validated daily historical FX. Global200 membership files carry a PIT label but historical market-cap ranking provenance is not externally certified. Yahoo adjusted prices, delisted tickers, symbol mapping and actual available trading sessions are not independently certified.

**This is a reproducible historical research backtest, not certified out-of-sample/PIT performance and not a live trading instruction.** The official branch `main` remains untouched.

## Where to find the large dataset
The full recovered dataset is supplied as a separate ~161 MB downloadable ZIP in the ChatGPT conversation and should be backed up locally. It is NOT in this GitHub repository: the connector stores UTF-8 text and is not a large binary asset uploader. A 288-symbol acquisition workflow remains optional; it is not needed to regenerate the recovered historical test if using the complete offline archive.
