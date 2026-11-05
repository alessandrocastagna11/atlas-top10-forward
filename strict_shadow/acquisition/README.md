# Atlas V4 historical data acquisition — SHADOW ONLY

This branch is experimental. The official forward on main is unchanged.

The workflow is **not a completed backtest**. It downloads historical Yahoo chart price series and FX for a historical Global200 issuer list, and saves a research artifact. A valid `strict_shadow/acquisition/input/global_top200_annual_pit.csv` is required before execution. **Never substitute today's Global500.**

Original source: `Atlas_V4_Acquisition_Kit_2026-11-05.zip` generated from the recovered Global200 PIT table (3,000 membership rows; 288 distinct symbols). The full original CSV is not yet committed to this repository; the workflow intentionally fails closed until provided.

Known limitations: historical USA100 monthly PIT ranking absent; Yahoo historical adjusted prices are retrospective; delisted issuers and symbol reuse require checks; FX quote conventions need verification; downloading prices does not establish independent V4 Confidence70 replication. GitHub Actions artifacts expire after 30 days; archive data separately for permanence. This workflow has no schedule on this non-default branch. Run via workflow_dispatch when available or push changes to its acquisition paths.
