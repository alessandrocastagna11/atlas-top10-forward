# Live / scheduled updates: deployment gate

The workflow `.github/workflows/atlas-v4-shadow.yml` is committed **only to a non-default branch**. GitHub Actions scheduled triggers generally run from the default branch, so **the daily cron is NOT active** merely because this branch contains the workflow. A manual workflow_dispatch may be available depending on GitHub Actions UI configuration.

To activate scheduled monitoring, explicitly approve a tiny dispatcher workflow on `main` that checks out `atlas-strict-v12-shadow` and writes ONLY to that branch, or deploy the isolated monitor to an independent repository. Until that approval, no autonomous live updates are claimed.

The monitor does not download fresh market data or generate strategy targets. It accepts only a matching independently attested external exact-engine snapshot; without it the status is PENDING_DATA. No real-money orders are possible. A valid snapshot must be produced from exact PIT market/FX/ranking inputs and the full V4 engine, which are not completely present in the uploaded archive.

To run locally: `python strict_shadow/monitor.py`. No third-party dependencies.
