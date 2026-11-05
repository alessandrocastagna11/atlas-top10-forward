#!/usr/bin/env python3
"""Independent daily V4-style RESEARCH PROXY paper execution, never an exact H4 parity claim."""
from __future__ import annotations
import datetime as dt,json,os
from pathlib import Path
from zoneinfo import ZoneInfo
from paper import PaperBlocked,execute_paper,fresh_state,mark,validate_state,canonical_sha
from provider import YahooChart,DataUnavailable
from dataset_daily import atomic,json_save
from forward_signal import FORWARD,VERSION
NY=ZoneInfo('America/New_York')
MODEL='V4_CONF70_FORWARD_COHORT_PROXY_V1'
STATE=FORWARD/'proxy_paper_state.json'

def execute(now=None,provider=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    provider=provider or YahooChart()
    if now.tzinfo is None:raise ValueError('now must be aware')
    ny=now.astimezone(NY)
    state=json.loads(STATE.read_text()) if STATE.exists() else fresh_state()
    if state.get('research_model_variant',MODEL)!=MODEL:raise PaperBlocked('proxy strategy identity mismatch')
    state['research_model_variant']=MODEL
    validate_state(state)
    result={'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
            'model_variant':MODEL,'historical_engine_equivalent':False,
            'capital_initial_eur':state['initial_cash_eur'],'real_money_orders':0,
            'status':'PENDING_SIGNAL','reason':'No pending forward proxy decision'}
    signalfile=FORWARD/'pending_signal.json'
    if signalfile.is_file():
        try:
            signal=json.loads(signalfile.read_text())
            if signal.get('model_variant')!=VERSION or signal.get('historical_engine_equivalent') is not False:
                raise PaperBlocked('Only explicit research proxy signals are accepted')
            if signal.get('authorization',{}).get('real_money_authorized') is not False:
                raise PaperBlocked('No real money authorization')
            if signal.get('schema_version')!='research-forward-1':raise PaperBlocked('unsupported signal schema')
            dec=dt.date.fromisoformat(signal['decision_session'])
            exe=dt.date.fromisoformat(signal['execution_session'])
            if exe<=dec or dec>ny.date() or (ny.date()-dec).days>7:
                raise PaperBlocked('stale/future proxy signal')
            if exe>ny.date() or ny.time()<dt.time(16,45) and exe==ny.date():
                raise DataUnavailable('next execution session is not complete')
            if state.get('last_execution_session') and exe<=dt.date.fromisoformat(state['last_execution_session']):
                raise PaperBlocked('already executed, refusing duplicate')
            if len(signal.get('targets',[]))>10:raise PaperBlocked('too many targets')
            roster=signal['universe_source']
            if dt.date.fromisoformat(roster['recorded_on'])>dec or dt.date.fromisoformat(roster['asof'])>dec:
                raise PaperBlocked('future roster vintage')
            # Quote the real NEXT trading session. No close equals open, no filled orders from synthetic prices.
            check=provider.bar('ACWI',exe)
            if check.session!=exe:raise DataUnavailable('US execution benchmark session unverified')
            names={s['ticker']:s['market_symbol'] for s in signal['targets']}
            for t,p in state['holdings'].items():
                if t in names and names[t]!=p['market_symbol']:raise PaperBlocked('issuer symbol map collision')
                names[t]=p['market_symbol']
            quotes={}
            metadata={}
            for t,sym in names.items():
                rec=provider.eur_quote(sym,exe,'open')
                quotes[t]=rec['price_eur'];metadata[t]=rec
            new,trade=execute_paper(state,signal,quotes)
            new['research_model_variant']=MODEL
            json_save(STATE,new)
            json_save(FORWARD/'execution_receipt.json',{
                'mode':'PROXY_PAPER_ONLY','model_variant':MODEL,
                'execution_session':str(exe),'source_quotes':metadata,
                'signal_sha256':canonical_sha(signal),'paper_trade_summary':trade})
            signalfile.unlink()
            state=new
            result.update({'status':'PROXY_PAPER_EXECUTED','reason':'All next-session opens verified',
                           'execution_session':str(exe),'execution':trade})
        except DataUnavailable as err:
            result.update({'status':'PENDING_EXECUTION_QUOTES','reason':str(err)[:300]})
        except (PaperBlocked,KeyError,ValueError,TypeError) as err:
            result.update({'status':'BLOCKED_PROXY_EXECUTION','reason':str(err)[:300]})
    result.update({'cash_eur':round(state['cash_eur'],2),'orders_count':len(state['orders']),
                   'executed_decisions':len(state['decisions'])})
    if state['holdings']:
        try:
            # Mark all positions only when a matching exact market session can be established.
            hist=provider.history('ACWI',ny.date()-dt.timedelta(days=12),ny.date())
            eligible=[d for d in hist if d<=ny.date() and (d!=ny.date() or ny.time()>=dt.time(16,45))]
            if not eligible:raise DataUnavailable('benchmark EOD unavailable')
            session=max(eligible)
            quotes={t:provider.eur_quote(p['market_symbol'],session,'close')['price_eur']
                    for t,p in state['holdings'].items()}
            result['valuation']=mark(state,quotes,session)
        except (DataUnavailable,PaperBlocked) as e:
            result['valuation_status']='PENDING_VALUATION';result['valuation_reason']=str(e)[:200]
    else:
        result['valuation']={'nav_eur':round(state['cash_eur'],2),'cash_eur':round(state['cash_eur'],2),'positions':0}
    json_save(FORWARD/'proxy_paper_status.json',result)
    lines=['# Atlas V4 forward — research PROXY paper account','',
           '**Status: '+result['status']+'**','',result['reason'],'',
           'Model: '+MODEL+' — NOT historically identical to the frozen V4 engine.',
           'Virtual NAV: EUR '+str(result.get('valuation',{}).get('nav_eur','PENDING')),
           'Paper orders: '+str(result['orders_count']),
           'Real broker orders: 0',
           'Strict Confidence70 model remains separate and fail-closed.']
    atomic(FORWARD/'proxy_paper_latest.md',('\n'.join(lines)+'\n').encode())
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return result
if __name__=='__main__':execute()
