#!/usr/bin/env python3
"""Publish independent V4 audit gates; never promote current-cohort proxy to original H4 parity."""
from __future__ import annotations
import datetime as dt,json
from pathlib import Path
from dataset_daily import DATA,json_save,rows_load
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'reports/pit_readiness.json'
PROXY=ROOT/'forward/proxy_paper_status.json'
STRICT=ROOT/'reports/latest_status.json'
REQUIREMENTS=(
    'POINT_IN_TIME_USA100_MARKET_CAP_RANKING',
    'POINT_IN_TIME_ROW_MARKET_CAP_AND_ISSUER_SCORING',
    'TIME_VERSIONED_FX_WITH_HISTORICAL_RELEASE_AND_REVISIONS',
    'FULL_ORIGINAL_CONFIDENCE70_FEATURE_AND_RULE_PARITY',
    'INDEPENDENT_HISTORICAL_ORDER_BY_ORDER_RECONCILIATION',
)

def read_json(path):
    try:return json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
    except (OSError,ValueError):return {}

def status(now=None,root=ROOT,require_model=False):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('timezone required')
    dataset=read_json(root/'dataset/audit/latest.json')
    ecb=read_json(root/'dataset/fx_ecb/audit/latest.json')
    universe=read_json(root/'dataset/universe/snapshots'/now.date().isoformat()/'source.json')
    if not universe:universe=dataset.get('forward_universe',{})
    strict=read_json(root/'reports/latest_status.json')
    proxy=read_json(root/'forward/proxy_paper_status.json')
    ecb_data=(ecb.get('status')=='ECB_REFERENCE_UPDATED')
    refcurr=int(ecb.get('currencies_with_history',0))
    ecb_stale=[]
    for currency,coverage in ecb.get('currency_coverage',{}).items():
        try:
            if (now.date()-dt.date.fromisoformat(coverage['last'])).days>14:
                ecb_stale.append(currency)
        except (ValueError,TypeError,KeyError):ecb_stale.append(currency)
    signal=read_json(root/'forward/latest_status.json')
    rotype=universe.get('type','UNKNOWN')
    # Current iShares holdings are a useful prospective cohort but NOT
    # contemporaneous company market-cap ranks for historical Confidence70.
    pit_roster=False
    historic_fx_pit=False # Time-versioned historic revisions not proven by retrospective data.
    original_parity=False
    pending=list(REQUIREMENTS)
    return {'checked_at_utc':now.isoformat(),'workflow':'PAPER_ONLY',
            'real_money_authorized':False,'original_confidence70_model_ready':False,
            'research_proxy_model_ready':signal.get('status')=='RESEARCH_SIGNAL_READY',
            'research_proxy_execution_status':proxy.get('status','UNKNOWN'),
            'research_proxy_orders':int(proxy.get('orders_count',0)),
            'research_proxy_nav_eur':proxy.get('valuation',{}).get('nav_eur'),
            'original_model_status':strict.get('status','UNKNOWN'),
            'historical_pit_issues_remaining':pending,
            'historical_pit_verified':pit_roster and historic_fx_pit and original_parity,
            'universe_ranking_type':rotype,
            'FX':{'ecb_daily_reference_available':ecb_data,
                  'ecb_currencies_with_history':refcurr,
                  'ecb_currencies_stale_over_14_days':sorted(ecb_stale),
                  'ecb_retroactive_archive_not_original_vintage':True,
                  'annual_interpolated_fx_allowed_in_pit_engine':False,
                  'yahoo_execution_fx_separately_sourced':True},
            'notes':'ECB is a daily independent reference; historical extraction does not by itself prove historical PIT availability. Original V4 stays fail-closed.'}

def main():
    result=status()
    json_save(OUT,result)
    print(json.dumps({k:result[k] for k in
          ('original_confidence70_model_ready','research_proxy_execution_status','research_proxy_orders','FX')},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
