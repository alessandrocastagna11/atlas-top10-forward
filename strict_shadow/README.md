# Atlas Strict v1.2 — isolated replication gate

**Status: BLOCKED_SOURCE_DATA**. This branch does not claim to reproduce the historical result. The official forward engine on `main` is unchanged.

Historical anchors (previously reported, not independently revalidated):
- Net CAGR: 11.304069694%
- Max drawdown: -14.304488103%
- Sharpe: 0.9229
- Global Top200 PIT; 2012–2025 research window (exact start/end dates still require original manifest)
- 10 holdings max; issuer 15% max; sector 30% max; <=3 issuers/sector; 99% exposure max; 1% cash minimum
- Canonical selection was described as USA Top100 core + Global Top200 satellite; LADDER60/B5. These descriptors are insufficient to reimplement exact rules.

Required original artifacts before an exact backtest can run:
1. Canonical v1.2 source and full parameter manifest, including LADDER60/B5 semantics and costs/taxes.
2. Frozen PIT membership and issuer-level price/corporate action/FX panels with SHA256 hashes.
3. Original NAV/equity ledger, trade log and benchmark; precise calendar and starting capital.
4. Documented methodology for CAGR, max DD and Sharpe.

Use `python strict_shadow/verify.py --nav PATH --manifest PATH` to check the *provided* original ledger. It intentionally does **not** generate returns from missing data or adjust parameters to force a match.

Known dataset master manifest identifier: GLOBAL_TOP200_PIT_MCR_YF_535f47e79beb_20d34bc8428e; PIT membership SHA256 535f47e79beb484a4cc7c03f0ddb2e87290b0c2956774cb0ead1c554bf59ebb8. Dataset research-grade, not institutionally certified.

No GitHub Actions workflows, trading orders or scheduled processes are enabled on this branch.
