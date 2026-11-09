#!/usr/bin/env python3
"""Atlas V4 Confidence70: frozen source audit and fail-closed 2026 feed readiness.

Does not build signals, trade, infer missing PIT membership or certify replay parity.
Never imports user archive's pickle; no untrusted deserialization occurs here.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
LIVE = HERE.parent / 'live_v4'
EXPECTED_SHA256 = {
    'scripts/replay_confidence70.py': '8d81949ade468bc37949fe2c284176c5518ff821198a118b2240888fb8859ee5',
    'code/incumbent_engine.py': '752876f47e0e4df3bd2e73954a482936ff8a9adae59835ff1de1a6cfb6dbccc8',
    'code/engine/equity_long_backtest.py': 'c236e566845de79fe18a0e7993e314065ac88e2f3c71998b94f4363ccf9ea9aa',
    'code/engine/equity_long_backtest_v2.py': '716c525d983131de8b1f593099c8f68609dd5926fe0748a8bf0ae70e3de40c9c',
    'code/engine/equity_long_bear_tax_comparison.py': '7d6071dd7bf4c46647f92f84a9cd743b5fe4025c646b7770d7134cd351df410b',
    'code/engine/equity_long_v4_research.py': '20e779c2a7322be50b14666d6f7086f0b03da0527fcb225e679de8e974464e37',
    'code/engine/equity_long_overlay_comparison.py': '05b30c8a8ed83b92e1293b6b0dd928c925392a244919c7f7844de3bfe7f0010c',
}
REQUIRED = [
    'authentic_2026_USA100_monthly_membership_and_rank_vintages',
    'authentic_2026_GLOBAL200_minus_USA_membership_vintages',
    'regenerated_2026_daily_USA_and_ROW_factors_with_OLS_residuals',
    'matching_2026_SPY_regime_T8_panic_and_rebound_panels',
    'independent_daily_point_in_time_FX_and_corporate_actions',
    '2026_replay_to_signal_source_code_parity_validation',
    'engine_produced_target_snapshot_for_next_trading_session',
]


def source_integrity(root=HERE):
    checks = {}
    for rel, required in EXPECTED_SHA256.items():
        path = root / rel
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        checks[rel] = {'expected_sha256': required, 'observed_sha256': actual,
                       'exact_match': actual == required}
    return checks


def assess(root=HERE, live=LIVE, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone-aware current timestamp required')
    source = source_integrity(root)
    source_ok = all(x['exact_match'] for x in source.values())
    audit_path = live / 'dataset' / 'audit' / 'latest.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.is_file() else {}
    exact_signal_path = live / 'inbox' / 'latest_signal.json'
    # Even when an inbox file appears, a manually constructed snapshot does
    # NOT establish that the unmodified frozen model generated it.
    input_ready = bool(audit.get('data_ready_for_confidence70_model') is True)
    return {
        'checked_at_utc': now.astimezone(dt.timezone.utc).isoformat(),
        'name': 'Atlas V4 Confidence70 — FROZEN ORIGINAL ENGINE / strict live',
        'original_engine_source_present': source_ok,
        'frozen_source_files_verified': sum(x['exact_match'] for x in source.values()),
        'source_files_expected': len(EXPECTED_SHA256),
        'source_files': source,
        'historical_reference_period': ['2012-01-03', '2025-12-10'],
        'historical_reference_cagr_10k_pct': 10.711838,
        'historical_reference_operational_orders_10k': 710,
        'input_data_audit_status': audit.get('dataset_use', 'MISSING'),
        'input_data_model_ready': input_ready,
        'verified_frozen_engine_2026_execution': False,
        'frozen_live_signal_file_present': exact_signal_path.is_file(),
        'status': ('FROZEN_SOURCE_INTEGRITY_ERROR' if not source_ok else
                   'PENDING_EXACT_2026_PIT_ENGINE_ADAPTER'),
        'required_before_any_exact_engine_paper_orders': REQUIRED,
        'allowed_to_use_cohort_proxy_signals': False,
        'automatic_broker_orders': 0,
        'disposition': 'NO EXACT MODEL SIGNAL; DO NOT SUBSTITUTE PROXY OR HISTORICAL WEIGHTS',
        'explanation': ('Immutable frozen source is installed, but the original replay takes 2012-2025 frozen panel inputs. '
                        'Regenerating 2026 PIT USA/ROW panels and proving source-level signal parity '
                        'is required before activating exact live. Historical results cannot be extrapolated.'),
    }


def main():
    report = assess()
    out = HERE / 'reports' / 'latest_status.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n', encoding='utf-8')
    print('ATLAS_V4_FROZEN_SOURCE_AUDIT', report['status'],
          report['frozen_source_files_verified'], '/', report['source_files_expected'],
          'DATA_READY', report['input_data_model_ready'])
    if report['status'] == 'FROZEN_SOURCE_INTEGRITY_ERROR':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
