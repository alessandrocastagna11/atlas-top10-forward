# Atlas V4 — daily PAPER forward and historical PIT gap audit (6 Nov 2026)

**Model scope:** two distinct experimental ledgers. The original frozen H4
Confidence70 is fail-closed and has zero original-model trades until exact
feature and ranking parity can be attested. The independent
`V4_CONF70_FORWARD_COHORT_PROXY_V1` generates forward research signals using
the current dated iShares ACWI holdings cohort, not true PIT market-cap ranks.
The proxy is **not** a performance replication of the 2012–2025 original.

## Working live schedule

The default-branch dispatcher `.github/workflows/atlas-v4-confidence70-dispatcher.yml`
runs at UTC 00:15 and 02:45 Monday–Saturday (winter in Italy: 01:15 and
03:45, daylight saving shifts them by one local hour), and additionally
supports manual dispatch. Checkout, commits and paper results are restricted to
the branch `atlas-v4-confidence70-live-paper`. A second push-triggered
workflow tests and updates the branch after code changes.

Daily:
1. Run offline guards and validate both ledgers.
2. Refresh dated/hashed Yahoo OHLC, FX and contemporaneously recorded ETF
   holdings cohort; *same-day FX candles are not treated as EOD until 17:30 NY*.
   New bars record their first observed UTC timestamp; older cache rows lacking
   timestamps are not grandfathered as originally PIT.
3. Independently fetch **ECB EXR** daily EUR reference rates from 2012 onward.
   Original rates are stored in `dataset/fx_ecb/<CUR>.csv`; any revision is
   logged in `dataset/fx_ecb/revisions.csv`; the audit is
   `dataset/fx_ecb/audit/latest.json`. A retroactively retrieved official
   series is **not** a certified historical *as-published* vintage. No annual
   interpolations are allowed in any PIT-claiming pipeline. Rates are for
   independent validation; the trading quote adapter is kept separate.
4. Settle previously precommitted proxy signals only at genuinely verified
   session prices (6 Nov USA launch uses a pre-close locked MOC).
   Mark held securities using the last **published** exchange close, with
   individual market-session dates and freshness limits. Do not confuse marking
   a closed venue with buying/selling there.
5. Publish the next forward signal using dated information and retain an
   immutable signal history. No brokerage credentials and no live broker routes.
6. Publish `reports/pit_readiness.json` with explicit evidence gaps, current
   proxy orders and NAV. CI passing **does not** imply an order took place.

The ChatGPT task *Atlas V4 Confidence70 live* remains enabled for a separate
daily report at 09:30 Europe/Rome. It inspects GitHub's committed audit and
ledger; GitHub Actions, not the reminder itself, performs the simulations.

## Important methodological limitations

- 2012–2025 returns reconstructed with annual-average/interpolated FX and
  retrospective universe rankings **must not** be rebranded as PIT, even after
  the new ECB data is downloaded. This update establishes an independent
  daily FX reference panel, not a corrected full backtest.
- ECB TWD daily reference coverage may end before 2026; the audit explicitly
  flags stale currencies. SAR (and currencies absent from ECB) need separate
  historical quote provenance and triangulation checks.
- Global top-100 capitalization snapshots, RoW scores, delistings, original
  H4 feature parity and independent trade-by-trade reconciliation are still
  required to make the frozen engine operable. Such data must not be
  invented or substituted with today's ETF constituents.
- Neither synthetic tests nor successful workflow runs establish financial
  performance or guarantee availability, fills or a maximum drawdown.

Useful files:
- `forward/proxy_paper_status.json` — proxy paper NAV/orders.
- `forward/execution_receipt.json` — latest actual simulated fill receipt.
- `forward/proxy_paper_state.json` — append-only decisions and orders.
- `reports/latest_status.json` — strict original model status (separate).
- `dataset/fx_ecb/audit/latest.json` — ECB independent reference coverage.
- `reports/pit_readiness.json` — issues blocking exact historical parity.
