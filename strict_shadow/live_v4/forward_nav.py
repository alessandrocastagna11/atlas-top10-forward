#!/usr/bin/env python3
"""Idempotent, revision-audited EOD NAV history for PAPER research proxy.

The value recorded is a post-cost *virtual* valuation, not a broker fill.
Only record completed benchmark sessions; never invent a forward return.
"""
from __future__ import annotations
import datetime as dt,hashlib,json,math
from pathlib import Path
from dataset_daily import json_save

def _digest(value):
    raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()

def summarize(observations, initial_capital=10000.):
    # Latest revision per *real* completed benchmark day; do not count
    # multiple retries or source revisions as additional trading sessions.
    latest={}
    for row in observations:latest[row['session']]=row
    ordered=[latest[d] for d in sorted(latest)]
    if not ordered:return {'unique_sessions':0,'max_drawdown_pct':0.,'day_return_pct':None,
                            'latest_session':None,'latest_nav_eur':initial_capital}
    peak=float(initial_capital);worst=0.
    for row in ordered:
        nav=float(row['nav_eur'])
        peak=max(peak,nav)
        worst=min(worst,100*(nav/peak-1))
    last=ordered[-1]
    prev=float(ordered[-2]['nav_eur']) if len(ordered)>1 else initial_capital
    return {'unique_sessions':len(ordered),'latest_session':last['session'],
        'latest_nav_eur':last['nav_eur'],'day_return_pct':round(100*(float(last['nav_eur'])/prev-1),5),
        'cumulative_return_pct':round(100*(float(last['nav_eur'])/initial_capital-1),5),
        'max_drawdown_pct':round(worst,5),
        'revision_count':len(observations)-len(ordered)}

def record(path,valuation,source_quotes,now,initial_capital=10000.):
    if now.tzinfo is None:raise ValueError('timezone required')
    session=dt.date.fromisoformat(valuation['session']).isoformat()
    if dt.date.fromisoformat(session)>now.date():raise ValueError('future valuation session')
    nav=float(valuation['nav_eur']);cash=float(valuation['cash_eur'])
    positions=int(valuation['positions'])
    if not all(math.isfinite(z) and z>=0 for z in (nav,cash)) or nav<=0:
        raise ValueError('invalid virtual NAV')
    if positions<0:raise ValueError('negative positions')
    raw=json.loads(path.read_text(encoding='utf8')) if path.exists() else {
        'schema_version':1,'mode':'RESEARCH_PROXY_PAPER_ONLY','observations':[]}
    if raw.get('schema_version')!=1 or raw.get('mode')!='RESEARCH_PROXY_PAPER_ONLY':
        raise ValueError('Unexpected journal format')
    obs=raw['observations']
    same=[row for row in obs if row['session']==session]
    row={'session':session,'nav_eur':round(nav,2),'cash_eur':round(cash,2),
         'positions':positions,'first_reported_utc':now.astimezone(dt.timezone.utc).isoformat(),
         'quote_audit_sha256':_digest(source_quotes)}
    if same:
        last=same[-1]
        if all(last.get(x)==row[x] for x in ('nav_eur','cash_eur','positions')):
            return raw,summarize(obs,initial_capital)
        row['revises_previous_sha256']=_digest(last)
    obs.append(row)
    json_save(path,raw)
    return raw,summarize(obs,initial_capital)
