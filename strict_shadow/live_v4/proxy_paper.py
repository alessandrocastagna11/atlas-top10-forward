#!/usr/bin/env python3
"""Independent daily V4-style RESEARCH PROXY paper execution, never an exact H4 parity claim."""
from __future__ import annotations
import datetime as dt,json,os
from pathlib import Path
from zoneinfo import ZoneInfo
from paper import PaperBlocked,execute_paper,fresh_state,mark,validate_state,canonical_sha,earliest_open_utc
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
            # A paper "next open" is only legitimate when the signal's
            # timestamp proves it existed BEFORE the simulated opening.
            # Missing timestamps in old signals are not grandfathered in.
            stamped=dt.datetime.fromisoformat(signal['produced_at_utc'])
            if stamped.tzinfo is None or stamped>now:
                raise PaperBlocked('signal production timestamp missing timezone or future')
            stamp_ny=stamped.astimezone(NY)
            if stamp_ny.date()<dec or (stamp_ny.date()==dec and stamp_ny.time()<dt.time(17,30)):
                raise PaperBlocked('signal predates verified decision-session close')
            symbols=[p['market_symbol'] for p in signal.get('targets',[])]
            symbols.extend(p['market_symbol'] for p in state['holdings'].values())
            # OPEN and CLOSE are different execution policies. A same-day
            # precommitted MOC is correctly AFTER the NY opening, but must
            # satisfy the separate 15:45 NY MOC lock deadline below.
            if signal.get('execution_policy')!='US_MOC_LOCKED_PRE_CLOSE':
                global_cutoff=earliest_open_utc(symbols,exe)-dt.timedelta(minutes=30)
                if stamped.astimezone(dt.timezone.utc)>=global_cutoff:
                    raise PaperBlocked('signal was produced after earliest global opening cutoff')
            if exe<=dec or dec>ny.date() or (ny.date()-dec).days>7:
                raise PaperBlocked('stale/future proxy signal')
            # Optional one-off start: US market-on-close intent locked in Git
            # before the NY 15:45 cutoff. Non-US exchanges have already closed
            # and MUST NOT be booked at an earlier close after the decision.
            moc=signal.get('execution_policy')=='US_MOC_LOCKED_PRE_CLOSE'
            if signal.get('execution_policy') not in (None,'US_MOC_LOCKED_PRE_CLOSE'):
                raise PaperBlocked('unrecognized execution policy')
            if moc:
                if dec!=dt.date(2026,11,5) or exe!=dt.date(2026,11,6):
                    raise PaperBlocked('US MOC launch allowed only on Nov 6, 2026')
                def aware_utc(value):
                    instant=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
                    if instant.tzinfo is None:raise PaperBlocked('missing timestamp timezone')
                    return instant.astimezone(dt.timezone.utc)
                produced=aware_utc(signal['produced_at_utc'])
                locked=aware_utc(signal['locked_at_utc'])
                if produced>locked or produced.date()<dt.date(2026,11,5):
                    raise PaperBlocked('invalid MOC intent timestamps')
                cutoff=dt.datetime(2026,11,6,15,45,tzinfo=NY).astimezone(dt.timezone.utc)
                if locked>=cutoff or produced>=cutoff:
                    raise PaperBlocked('MOC intent not committed before cutoff')
                if not signal.get('parent_signal_blob_sha'):
                    raise PaperBlocked('MOC intent missing immutable GitHub parent blob evidence')
                if not signal.get('targets') or any(
                    t.get('region')!='USA' or ('.' in t.get('market_symbol','')
                    and not t.get('market_symbol','').endswith(('.A','.B')))
                    for t in signal['targets']
                ):
                    raise PaperBlocked('US MOC may contain only US-listed assets')
                if exe>ny.date() or (exe==ny.date() and ny.time()<dt.time(17,30)):
                    raise DataUnavailable('locked Nov 6 MOC: waiting for verified US closing prices')
                if (ny.date()-exe).days>4:
                    raise PaperBlocked('MOC launch expired; refusing retrospective fills')
            elif exe>ny.date() or (ny.time()<dt.time(17,30) and exe==ny.date()):
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
            execution_field='close' if moc else 'open'
            for t,sym in names.items():
                rec=provider.eur_quote(sym,exe,execution_field)
                quotes[t]=rec['price_eur'];metadata[t]=rec
            new,trade=execute_paper(state,signal,quotes,rebalance_fraction=0.01,
                                    execution_price_kind=execution_field)
            new['research_model_variant']=MODEL
            json_save(STATE,new)
            json_save(FORWARD/'execution_receipt.json',{
                'mode':'PROXY_PAPER_ONLY','model_variant':MODEL,
                'execution_session':str(exe),'source_quotes':metadata,
                'execution_policy':signal.get('execution_policy','NEXT_VERIFIED_OPEN'),
                'signal_sha256':canonical_sha(signal),'paper_trade_summary':trade})
            signalfile.unlink()
            state=new
            result.update({'status':'MOC_PAPER_EXECUTED' if moc else 'PROXY_PAPER_EXECUTED',
                           'reason':'Verified same-day US close with pre-close intent' if moc else 'All next-session opens verified',
                           'execution_session':str(exe),'execution_price_kind':execution_field,'execution':trade})
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
            eligible=[d for d in hist if d<=ny.date() and (d!=ny.date() or ny.time()>=dt.time(17,30))]
            if not eligible:raise DataUnavailable('benchmark EOD unavailable')
            session=max(eligible)
            valuation_audit={t:provider.latest_published_eur_quote(
                    p['market_symbol'],session,max_age_days=5)
                    for t,p in state['holdings'].items()}
            quotes={t:q['price_eur'] for t,q in valuation_audit.items()}
            result['valuation']=mark(state,quotes,session)
            result['valuation_sources']={t:{k:q[k] for k in
                    ('symbol','valuation_quote_session','valuation_staleness_days','fx_session','market_sha256')
                    if k in q} for t,q in valuation_audit.items()}
            result['valuation_non_exact_markets']=[
                t for t,q in valuation_audit.items() if not q['is_exact_valuation_session']]
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
