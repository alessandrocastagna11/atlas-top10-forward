# Atlas V4 Confidence70 — isolated GitHub shadow (0.18.0)

**Status: SOURCE_IDENTIFIED / FULL_REPLAY_BLOCKED**. Official main branch and forward automation are unchanged. No simulated performance has been fabricated.

## Source
User-provided archive: `H4Atlas_0.18.0_V4_CONFIDENCE70_CLEAN_2026-10-07.zip`. This branch records the exact declared 0.18.0 engine specification and provides a fail-closed parity checker. The source archive has NOT been imported into GitHub in full.

- Engine ID: `ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07`
- Historical window: 2012-01-03 through 2025-12-10
- Declared historical net CAGR: 10.902089925976854%; MaxDD: -13.579045332178929%; Sharpe excess cash: 0.8814825781070055; orders: 720; ending NAV from €100k: €422,926.4298611628.
- Risk and execution contract in `v4_confidence070_spec.json`.

## Reproduction boundary
The archive contains Android Kotlin execution code, canonical research scripts and frozen equity/orders reference CSVs. However, its `build_from_source.py` references absolute local paths such as `/mnt/data/h4_usa_rebuild`, `/mnt/data/h4_quality_work` and `/mnt/data/h4_exact_recovery` and external Wharton data caches. These missing inputs must be restored and checksummed before claiming independent end-to-end replication. The archived recovery notes also flag a nonzero RoW ranking reconstruction discrepancy.

## Next gate
1. Import original frozen source files and verify their original SHA256 manifest.
2. Run source rebuild against exact point-in-time market, FX, and membership data; no hindsight/oracle.
3. Compare NAV, trades, costs and drawdowns to original 0.18.0 frozen outputs; require exact or explicitly justified tolerances.
4. Only then consider a shadow forward runner. Do not schedule trades or modify official forward engines until parity passes.

**Do not confuse ledger consistency with independent backtest replication.**
