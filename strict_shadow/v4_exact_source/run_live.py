#!/usr/bin/env python3
"""Run only the archived Confidence70 core + original tax simulator for PAPER.

As-of provenance is written before a trade, and the next-run portfolio is
accepted only if all prior orders match the committed ledger exactly.
Never submits broker orders and never writes the Cohort Proxy or strict inbox.
"""
from __future__ import annotations
import datetime as dt, hashlib,json,math,os,sys,traceback
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from scipy import optimize
from live_model import HERE,CACHE,LIVE,START_PAPER,ENGINE,INITIAL,SECTOR,latest_vintages,get_adjusted,build_model,evaluate_model,check_required_us_closes,requires_same_us_close
from live_model import v1,v2,t9,inc,frozen,LIVE,check_required_local_closes,check_fx_freshness,_cache_filename,universe_transition_audit
if str(LIVE) not in sys.path:sys.path.insert(0,str(LIVE))
from paper import earliest_open_utc
from market_sessions import venue
import exchange_calendars as xcals
from paper_reporting import to_portfolio,initial_portfolio,write_report
from shared_inputs import publish as publish_shared_inputs
from multisource import audit as independent_data_audit

UTC=dt.timezone.utc
NY=ZoneInfo('America/New_York')
FORWARD=HERE/'forward'
STATE=FORWARD/'state.json';STATUS=FORWARD/'latest_status.json'
PREOPEN=FORWARD/'preopen'


def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,ensure_ascii=False,sort_keys=True,default=str,allow_nan=False)+'\n',encoding='utf8')
    tmp.replace(path)


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def serialize(result):
    cols=['scenario','strategy','decision_date','date','action','ticker','shares','reference_price_eur',
          'execution_price_eur','reference_value_eur','fee_eur','slippage_bps_per_side',
          'capital_gains_tax_eur','reason']
    rows=[]
    if result is None:return []
    for d in result.orders.to_dict('records'):
        row={}
        for key in cols:
            x=d.get(key)
            if isinstance(x,pd.Timestamp):x=x.isoformat()
            if isinstance(x,float) and not math.isfinite(x):x=None
            row[key]=x
        rows.append(row)
    return rows


def status_base(now):
    # Capture of today's observed market-cap universe is independent from
    # yesterday's completed US benchmark. Never silently promote future ranks
    # into an earlier decision session.
    cap_file=LIVE/'dataset'/'marketcap_observed'/'latest_status.json'
    try: cap=json.loads(cap_file.read_text(encoding='utf8')) if cap_file.is_file() else {}
    except (ValueError,OSError):cap={}
    return {'checked_at_utc':now.isoformat(),'engine':'FROZEN_ATLAS_V4_CONFIDENCE70',
            'universe_observed_capture_day':cap.get('capture_day'),
            'universe_observed_capture_at_utc':cap.get('observed_at_utc'),
            'universe_observed_marketcap_usa_count':cap.get('crosswalk_usa'),
            'universe_observed_marketcap_nonusa_count':cap.get('crosswalk_non_usa'),
            'universe_observed_nonusa_spare_count':cap.get('nonusa_ranked_replacement_buffer'),
            'universe_observed_nonusa_quality':cap.get('nonusa_roster_quality'),
            'universe_observed_nonusa_fragile':cap.get('nonusa_roster_fragile'),
            'universe_observed_match_methods':cap.get('crosswalk_match_methods'),
            'universe_observed_marketcap_ready':bool(cap.get('sufficient_for_100_usa_200_nonusa')),
            'universe_observed_is_historical_pit':False,
            'capital_initial_eur':INITIAL,'paper_only':True,'real_money_orders':0,
            'historical_alpha_code':'VERBATIM_REGION_SLOT_ATLAS_FROM_2012_2025',
            'tax_simulator':'FROZEN_T9_WITH_TERMINAL_LIQUIDATION_DISABLED',
            'universe_market_cap_pit_certified':False,
            'data_universe':'AS_RECORDED_ACWI_ETF_HOLDING_PROXY_2026',
            'frozen_replay_parity':'NOT_YET_PROVEN_FOR_2026_DAILY_INPUTS',
            'proxy_ledger_unchanged':True,'strict_exact_inbox_unchanged':True,
            'status':'PENDING_DATA'}


def positions_asof(ledger,decision):
    positions={}
    for row in ledger:
        if row['ticker']=='SPY' or str(row['date'])[:10]>str(decision):continue
        key=row['ticker']; qty=float(row['shares']);price=float(row['execution_price_eur']);fee=float(row['fee_eur'])
        if row['action']=='BUY':
            amount=qty*price+fee
            if key in positions:
                positions[key].shares+=qty;positions[key].cost_basis_eur+=amount
            else:positions[key]=v2.Position(qty,amount,pd.Timestamp(row['date']))
        else:
            if key not in positions or positions[key].shares+1e-6<qty:
                raise ValueError('Prior paper ledger sells unknown shares: '+key)
            pos=positions[key]
            fraction=qty/pos.shares
            pos.shares-=qty;pos.cost_basis_eur*=1-fraction
            if pos.shares<1e-8:del positions[key]
    return positions


# Exchange calendars for the actual listing venue, not the issuer's domicile.
# NSE is not supplied by exchange_calendars; XBOM is an explicitly indicative
# proxy for .NS, never a certified exchange calendar.
EXCHANGE_CALENDARS={
    'USA':'XNYS','.T':'XTKS','.HK':'XHKG','.KS':'XKRX','.KQ':'XKRX',
    '.TW':'XTAI','.L':'XLON','.SW':'XSWX','.TO':'XTSE','.V':'XTSX',
    '.AS':'XAMS','.PA':'XPAR','.BR':'XBRU','.DE':'XETR','.F':'XFRA',
    '.AX':'XASX','.BO':'XBOM','.NS':'XBOM','.ST':'XSTO','.CO':'XCSE',
    '.OL':'XOSL','.HE':'XHEL','.MI':'XMIL','.MC':'XMAD','.SI':'XSES',
    '.SR':'XSAU','.SA':'BVMF',
}

def listing_calendar(symbol, vendor_exchange=None):
    name,_=venue(symbol)
    if name not in EXCHANGE_CALENDARS:
        raise ValueError('No equity exchange calendar for '+str(symbol))
    calendar_name=('XNAS' if name=='USA' and vendor_exchange in {'NMS','NGM','NCM','NASDAQ','NAS'} else EXCHANGE_CALENDARS[name])
    return xcals.get_calendar(calendar_name),name!='.NS'

def symbol_next_open(symbol, first_date, not_before=None):
    """Next scheduled exchange session; a schedule never certifies a fill."""
    day=dt.date.fromisoformat(str(first_date)[:10])
    if not_before is not None and not_before.tzinfo is None:
        raise ValueError('A timezone-aware cutoff is required')
    cal,_=listing_calendar(symbol)
    session=cal.date_to_session(pd.Timestamp(day),direction='next')
    for _ in range(15):
        opening=cal.session_open(session).to_pydatetime()
        if not_before is None or opening>not_before:
            return session.date(),opening
        session=cal.next_session(session)
    raise ValueError('No eligible exchange opening: '+str(symbol))


def preview(m,meta,asof,now,old_orders):
    """Publish the original model signal even when exchange opens differ.

    One delayed Asian open must not block a US/European signal. A late signal
    is still a valid observable *signal*, but never certifies a past fill.
    """
    date=pd.Timestamp(asof)
    cfg=m.bundle['cfg']
    selector,_,_,_=m.callbacks(None)
    scores=m.path_score.loc[date]
    eligible=m.eligible.loc[date]&m.membership.loc[date]
    ranks=scores.where(eligible).rank(ascending=False,method='min')
    selected=selector(date,scores,ranks,eligible,positions_asof(old_orders,date),cfg)
    selected_symbols=[meta['ticker_to_market_symbol'][x] for x in selected]
    if m.bear_floor.get(date,0)>0:selected_symbols.append('SPY')
    if not selected_symbols:selected_symbols=['SPY']
    selected_symbols=list(dict.fromkeys(selected_symbols))
    first_date=asof+dt.timedelta(days=1)
    per_symbol={}
    for symbol in selected_symbols:
        first,opening=symbol_next_open(symbol,first_date)
        deadline=opening-dt.timedelta(minutes=30)
        on_time=now<deadline
        next_date,next_open=(first,opening) if on_time else symbol_next_open(
            symbol,first,not_before=now+dt.timedelta(minutes=30))
        vendor_exchange=meta.get('listing_exchange_by_symbol',{}).get(symbol,'UNKNOWN')
        calendar,calendar_verified=listing_calendar(symbol,vendor_exchange)
        requested_calendar=('XNAS' if venue(symbol)[0]=='USA' and vendor_exchange in
                            {'NMS','NGM','NCM','NASDAQ','NAS'} else EXCHANGE_CALENDARS[venue(symbol)[0]])
        per_symbol[symbol]={
            'listing_exchange_vendor':vendor_exchange,
            'exchange_calendar_requested':requested_calendar,
            'exchange_calendar_alias':requested_calendar!=calendar.name,
            'exchange_calendar':calendar.name,
            'exchange_calendar_source':'exchange_calendars_'+xcals.__version__,
            'first_candidate_close_utc':calendar.session_close(pd.Timestamp(first)).isoformat(),
            'next_eligible_close_utc':calendar.session_close(pd.Timestamp(next_date)).isoformat(),
            'first_candidate_session':str(first),
            'first_candidate_open_utc':opening.isoformat(),
            'first_candidate_cutoff_utc':deadline.isoformat(),
            'attested_before_first_candidate_open':on_time,
            'status':'PENDING_FIRST_LOCAL_OPEN' if on_time else 'DEFERRED_PAST_FIRST_LOCAL_OPEN',
            'next_eligible_local_session':str(next_date),
            'next_eligible_open_utc':next_open.isoformat(),
            'execution_quote_required':True,
            'exchange_holiday_calendar_verified':calendar_verified}
    # The model's common date index is SPY; per-listing plans remain independent.
    tomorrow,_=symbol_next_open('SPY',first_date)
    earliest=min(dt.datetime.fromisoformat(item['first_candidate_open_utc']) for item in per_symbol.values())
    cutoff=earliest-dt.timedelta(minutes=30)
    source_files=list(CACHE.glob('*.csv.gz'))
    source_digest=hashlib.sha256(''.join(sorted(digest(p) for p in source_files)).encode()).hexdigest()
    fingerprint=[(key,round(float(scores.get(key,0)),8)) for key in selected]
    decision_digest=hashlib.sha256(json.dumps({'selected':fingerprint,
          'risk_state':round(float(m.pr_regime.loc[date])*float(m.pr_t8.loc[date]),8)},
          sort_keys=True).encode()).hexdigest()
    on_time_all=all(x['attested_before_first_candidate_open'] for x in per_symbol.values())
    on_time_any=any(x['attested_before_first_candidate_open'] for x in per_symbol.values())
    record={'decision_session':str(asof),'execution_session':str(tomorrow),
            'decision_model_sha256':decision_digest,
            'produced_at_utc':now.isoformat(),'earliest_open_utc':earliest.isoformat(),
            'cutoff_utc':cutoff.isoformat(),'model_source_sha256':source_digest,
            'roster_sha256':meta['roster_sha256'],'engine_id':ENGINE,
            'market_data_first_observed_before_open':on_time_all,
            'any_market_preopen_eligible':on_time_any,
            'exchange_execution_policy':'PER_SYMBOL_NEXT_UNOBSERVED_OPEN',
            'per_symbol_exchange_plan':per_symbol,
            'candidate_count':meta['active_roster_names'],'preopen_selected_symbols':selected_symbols,
            'risk_state':float(m.pr_regime.loc[date])*float(m.pr_t8.loc[date])}
    path=PREOPEN/(str(asof)+'.json')
    if path.is_file():
        prior=json.loads(path.read_text())
        if prior.get('decision_model_sha256')==record['decision_model_sha256']:
            # Never rewrite time-of-publication evidence. Update the display
            # plan with present next eligible opens, not the immutable proof.
            frozen_plan=prior.get('per_symbol_exchange_plan',{})
            if frozen_plan:
                for symbol,item in per_symbol.items():
                    old_item=frozen_plan.get(symbol,{})
                    published=bool(old_item.get('attested_before_first_candidate_open'))
                    proof_time=prior.get('produced_at_utc')
                    # The immutable prior proof may have used a fictitious
                    # holiday opening. Revalidate only its recorded timestamp,
                    # never rewrite it or backdate a new observation.
                    if (old_item and proof_time and item['exchange_holiday_calendar_verified']
                            and old_item.get('first_candidate_open_utc')!=item['first_candidate_open_utc']):
                        proof_dt=dt.datetime.fromisoformat(proof_time)
                        published=proof_dt<dt.datetime.fromisoformat(item['first_candidate_cutoff_utc'])
                        item['original_calendar_plan_revalidated']=True
                    item['attested_before_first_candidate_open']=published
                    item['original_proof_utc']=proof_time
                    if published and now<dt.datetime.fromisoformat(item['first_candidate_open_utc']):
                        item['status']='PENDING_FIRST_LOCAL_OPEN'
                        item['next_eligible_local_session']=item['first_candidate_session']
                        item['next_eligible_open_utc']=item['first_candidate_open_utc']
                        item['next_eligible_close_utc']=item['first_candidate_close_utc']
            record['market_data_first_observed_before_open']=all(x['attested_before_first_candidate_open'] for x in per_symbol.values())
            record['any_market_preopen_eligible']=any(x['status']=='PENDING_FIRST_LOCAL_OPEN' for x in per_symbol.values())
            signal={'status':'ATTESTED_PREVIOUS_RUN','signal_published':True,
                    **record,'per_symbol_exchange_plan':per_symbol,
                    'original_proof_sha256':prior.get('decision_model_sha256'),
                    'original_proof_utc':prior.get('produced_at_utc'),
                    'first_publication_attested':prior.get('market_data_first_observed_before_open')}
            write_json(FORWARD/'latest_signal.json',signal)
            return signal
        # Preserve the prior decision; late corrections are visibly distinct
        # and cannot rewrite historical orders.
        if now<cutoff:
            superseded=PREOPEN/'superseded'/(str(asof)+'_'+str(prior.get('decision_model_sha256','unknown'))[:16]+'.json')
            if not superseded.is_file():write_json(superseded,prior)
            record.update({'supersedes_sha256':prior.get('decision_model_sha256'),
                           'superseded_archive':str(superseded.relative_to(PREOPEN)),
                           'revision_reason':'CORRECTED_CAUSAL_ROSTER_AND_INITIAL_ALPHA',
                           'original_proof_archived':True})
            write_json(path,record)
            signal={'status':'ATTESTED_CORRECTION_BEFORE_OPEN','signal_published':True,**record}
            write_json(FORWARD/'latest_signal.json',signal)
            return signal
        signal={'status':'SIGNAL_REVISION_AFTER_FIRST_OPEN_REQUIRES_FRESH_PROOF',
                'signal_published':True,'not_authorized_for_original_past_open':True,
                'prior_sha256':prior.get('decision_model_sha256'),
                'recomputed_sha256':record['decision_model_sha256'],**record}
        write_json(FORWARD/'latest_signal.json',signal)
        return signal
    # Publish score and exchange-local plan even when the first local opening
    # was already missed. Do not fabricate pre-open certification.
    if not on_time_any:
        signal={'status':'SIGNAL_PUBLISHED_ALL_EXECUTIONS_DEFERRED',
                'signal_published':True,**record}
    else:
        signal={'status':'SIGNAL_PUBLISHED_PER_EXCHANGE',
                'signal_published':True,**record}
    write_json(path,record)
    write_json(FORWARD/'latest_signal.json',signal)
    return signal


def order_timecheck(rows,last_orders,model,now,asof):
    """Append-only audit; after downtime reconstruct PAPER dates as marked backfills.

    No delayed run claims to have placed an order in the past.
    No new fill is accepted before its execution session has completed.
    The original as-recorded universe and original engine are still used.
    """
    if len(rows)<len(last_orders) or rows[:len(last_orders)]!=last_orders:
        raise ValueError('Append-only paper ledger is not a prefix: prior fills changed after source revision')
    fresh=rows[len(last_orders):]
    recovered_dates=set()
    verified_dates=set()
    model_last=pd.Timestamp(asof)
    for row in fresh:
        if str(row.get('reason'))=='end_of_test':
            raise ValueError('Historical terminal liquidation appeared in PAPER')
        dec=str(row.get('decision_date'))[:10]
        execution=str(row.get('date'))[:10]
        if pd.Timestamp(execution)>model_last:
            raise ValueError('Requested PAPER fill is after last verified complete session: '+execution)
        if dec in verified_dates:continue
        date=pd.Timestamp(dec)
        cfg=model.bundle['cfg']
        choose,_,_,_=model.callbacks(None)
        score=model.path_score.loc[date]
        eligible=model.eligible.loc[date]&model.membership.loc[date]
        ranks=score.where(eligible).rank(ascending=False,method='min')
        # Include earlier already reconstructed fills; do not evaluate
        # each missed decision using only yesterday's saved positions.
        chosen=choose(date,score,ranks,eligible,positions_asof(rows,date),cfg)
        fingerprint=[(key,round(float(score.get(key,0)),8)) for key in chosen]
        observed_sha=hashlib.sha256(json.dumps({'selected':fingerprint,
            'risk_state':round(float(model.pr_regime.loc[date])*float(model.pr_t8.loc[date]),8)},
            sort_keys=True).encode()).hexdigest()
        p=PREOPEN/(dec+'.json')
        proof=json.loads(p.read_text()) if p.is_file() else None
        is_live_attested=(bool(proof) and proof.get('market_data_first_observed_before_open') is True
                          and proof.get('execution_session')==execution
                          and proof.get('decision_model_sha256')==observed_sha)
        if not is_live_attested:
            # This is explicitly retrospective research PAPER, NEVER a
            # contemporaneous market fill and never a broker order.
            rec={
                'mode':'RECOVERED_HISTORICAL_PAPER_SIMULATION_NOT_LIVE_FILL',
                'decision_session':dec,'execution_session':execution,
                'recovered_at_utc':now.isoformat(),
                'original_attestation_present':bool(proof),
                'original_attestation_matches':False,
                'original_attestation_sha256':proof.get('decision_model_sha256') if proof else None,
                'replayed_signal_sha256':observed_sha,
                'original_source_verified':True,
                'execution_data_session_complete':True,
                'simulated_execution_not_real_order':True,
            }
            write_json(PREOPEN/'recoveries'/(dec+'.json'),rec)
            recovered_dates.add(dec)
        verified_dates.add(dec)
    return fresh,sorted(recovered_dates)


def main(now=None):
    now=now or dt.datetime.now(UTC)
    if now.tzinfo is None:raise ValueError('UTC-aware current time required')
    now=now.astimezone(UTC)
    FORWARD.mkdir(parents=True,exist_ok=True)
    status=status_base(now)
    old=json.loads(STATE.read_text()) if STATE.is_file() else {'orders':[]}
    prior_portfolio_path=FORWARD/'portfolio.json'
    portfolio=(json.loads(prior_portfolio_path.read_text()) if prior_portfolio_path.is_file()
               else initial_portfolio())
    try:
        # An incomplete US session must never become the decision_date.
        ny=now.astimezone(NY)
        candidate=ny.date() if (ny.hour,ny.minute)>=(17,30) else ny.date()-dt.timedelta(days=1)
        # SPY completed EOD is authoritative for US calendar closure, not a guessed weekday.
        base,fail=get_adjusted(['SPY'],candidate,max_workers=1,require_current_us={'SPY'})
        if 'SPY' not in base:raise ValueError('US EOD benchmark unavailable: '+str(fail))
        spy=base['SPY'];dates=[d.date() for d in spy.index if d.date()<=candidate]
        if not dates:raise ValueError('No completed benchmark history')
        asof=max(dates)
        if (candidate-asof).days>4:raise ValueError('Stale SPY benchmark')
        # Use the latest dated ranking, but never let one newly listed issuer
        # without sufficient calibration history veto the entire 300-name model.
        # Deterministically replace it with the next ranked eligible issuer.
        # Do not suppress missing benchmark/FX data or transient vendor errors.
        excluded_short_history=set()
        excluded_quote_currency=set()
        frames={'SPY':spy}
        active_local={'SPY'};fx_symbols=set()
        for attempt in range(40):
            vintages=latest_vintages(asof, excluded_symbols=excluded_short_history|excluded_quote_currency)
            all_constituents=[row for v in vintages for row in v[3]+v[4]]
            symbols={str(x['symbol']).upper() for x in all_constituents}
            required=set(symbols)|set(SECTOR.values())|{'SPY','EURUSD=X'}
            required|={'EUR'+str(x['currency']).upper()+'=X' for x in all_constituents
                       if str(x['currency']).upper()!='EUR'}
            # The benchmark SPY confirms the last US session. All current
            # US roster constituents, sector ETFs and held US positions must
            # have an actual close for that exact session.
            active_us={str(row['symbol']).upper() for row in vintages[-1][3]}
            active_us.update(SECTOR.values())
            active_us.update(str(row['ticker']).upper().removeprefix('G::')
                             for row in portfolio.get('holdings',[])
                             if isinstance(row,dict) and row.get('ticker'))
            strict_us={ticker for ticker in active_us
                       if requires_same_us_close(ticker)}
            active_local=active_us|{str(row['symbol']).upper() for row in vintages[-1][4]}|{'SPY'}
            required.update(active_local)
            fx_symbols={s for s in required if s.endswith('=X')}
            # Retry a previously cached frame if it now belongs to the active roster.
            previous_audit=check_required_local_closes(frames,active_local,asof,now)
            previous_fx=check_fx_freshness(frames,fx_symbols,asof,now)
            refresh=(required-set(frames))|set(previous_audit['stale_or_missing'])|set(previous_fx['blocked'])
            fetched,failed=get_adjusted(refresh,asof,max_workers=8,
                                        require_current_us=active_local|fx_symbols)
            frames.update(fetched)
            status['local_eod_quote_audit']=check_required_local_closes(frames,active_local,asof,now)
            status['local_eod_quote_audit']['provider_errors']={k:v for k,v in failed.items() if k in active_local}
            status['fx_freshness_audit']=check_fx_freshness(frames,fx_symbols,asof,now)
            status['us_eod_quote_audit']={
                'expected_session':str(asof),
                'required_us_count':len(strict_us),
                'missing_or_stale':{ticker:failed[ticker] for ticker in sorted(strict_us)
                                    if ticker in failed},
                'status':'BLOCKED' if any(ticker in failed for ticker in strict_us)
                         else 'CHECKING'}
            # ACWI reports the listing denomination, but Yahoo may return a
            # different one (e.g., London pence vs pounds, dual listings).
            # A wrong denomination silently multiplies EUR prices by 100 or
            # applies the wrong FX pair. Quarantine mismatched issuers and
            # deterministically refill the 100-USA/200-nonUSA ranked cohort.
            mismatched={}
            for row in all_constituents:
                ticker=str(row['symbol']).upper()
                frame=frames.get(ticker)
                if frame is None or 'quote_currency' not in frame:continue
                quoted=frame['quote_currency'].dropna()
                if quoted.empty:continue
                actual=str(quoted.iloc[-1]).upper()
                expected=str(row['currency']).upper()
                if actual!=expected:mismatched[ticker]=(expected,actual)
            if mismatched:
                excluded_quote_currency.update(mismatched)
                for ticker in mismatched:frames.pop(ticker,None)
                status['excluded_wrong_quote_currency']={k:{'roster_currency':v[0],
                    'vendor_currency':v[1]} for k,v in sorted(mismatched.items())}
            if not failed and not mismatched:break
            if mismatched and not failed:
                continue
            removable={ticker for ticker,reason in failed.items()
                       if ticker in symbols and
                       ('insufficient history' in reason.lower() or
                        'few clean adjusted closes' in reason.lower())}
            other={ticker:reason for ticker,reason in failed.items()
                   if ticker not in removable}
            if other:
                # Stale historical cache may validate independent returns for
                # research, but it never becomes the model's missing input.
                diagnostic_frames=dict(frames)
                expected_map=status['local_eod_quote_audit']['expected_session_by_symbol']
                for symbol in sorted(other):
                    if symbol in diagnostic_frames:continue
                    target=expected_map.get(symbol,str(asof))
                    try:
                        history=pd.read_csv(CACHE/_cache_filename(symbol),parse_dates=['Date'],index_col='Date')
                        history=history.loc[history.index<pd.Timestamp(target)].sort_index()
                        if not history.empty:diagnostic_frames[symbol]=history
                    except (OSError,ValueError,KeyError):pass
                status['multisource_audit']=independent_data_audit(diagnostic_frames,active_local,fx_symbols,asof,
                    expected_map,now,missing=other)
                raise ValueError('Required market/FX data unavailable; no fake substitute: '
                                 +str(sorted(other.items())[:10]))
            if not removable-excluded_short_history:
                raise ValueError('No progress while replacing short-history issuers: '+str(sorted(failed)))
            excluded_short_history.update(removable)
        else:
            raise ValueError('Too many short-history candidate replacements (>40)')
        status['short_history_excluded_from_daily_universe']=sorted(excluded_short_history)
        status['wrong_currency_excluded_from_daily_universe']=sorted(excluded_quote_currency)
        status['daily_universe_replacement_rounds']=attempt
        # The feed must be complete before we publish a snapshot or signal.
        status['us_eod_quote_audit']=check_required_us_closes(frames,strict_us,asof)
        status['local_eod_quote_audit']=check_required_local_closes(frames,active_local,asof,now)
        status['multisource_audit']=independent_data_audit(frames,active_local,fx_symbols,asof,
            status['local_eod_quote_audit']['expected_session_by_symbol'],now,
            missing=status['local_eod_quote_audit']['stale_or_missing'])
        if status['local_eod_quote_audit']['stale_or_missing']:
            raise ValueError('STALE_REQUIRED_LOCAL_EOD '+str(status['local_eod_quote_audit']['stale_or_missing']))
        status['fx_freshness_audit']=check_fx_freshness(frames,fx_symbols,asof,now)
        status['fx_recovery_audit']={}
        for symbol in sorted(fx_symbols):
            frame=frames.get(symbol)
            if frame is None or frame.empty or 'source_symbol' not in frame:continue
            last=frame.iloc[-1]
            if last.get('adjustment_source')!='FX_RECIPROCAL_VERIFIED_OVERLAP':continue
            status['fx_recovery_audit'][symbol]={'source_symbol':str(last['source_symbol']),
                'source_session':str(frame.index[-1].date()),
                'overlap_sessions':int(last['inverse_overlap_sessions']),
                'max_overlap_error':float(last['inverse_max_overlap_error']),
                'independent_provider':False}
        if status['fx_freshness_audit']['blocked']:
            raise ValueError('STALE_REQUIRED_FX '+str(status['fx_freshness_audit']['blocked']))
        # Each immutable recorded vintage becomes active only from its own
        # capture date. Replaying after downtime uses that same dated history.
        # A newly captured 2026 roster never changes yesterday's membership.
        vintage=vintages[-1]
        status['universe_transition_audit']=universe_transition_audit(vintages,asof)
        if asof>=dt.date(2026,11,30) and status['universe_transition_audit']['status']!='EXPANDED_ACTIVE':
            raise ValueError('Expanded universe not verified; no new model signal')
        locked_roster_date=str(vintages[0][0])  # legacy state only; NOT selection policy
        status['universe_lock_recorded_date']=locked_roster_date
        status['latest_available_roster_recorded_date']=str(vintage[0])
        status['universe_policy']='DAILY_CAUSAL_AS_RECORDED_ROSTERS_WITH_GAP_REPLAY'
        status['historical_universe_vintages']=len(vintages)
        m,meta=build_model(vintages,asof,frames)
        meta['listing_exchange_by_symbol']={symbol:str(frame['listing_exchange_vendor'].iloc[-1])
            for symbol,frame in frames.items() if not frame.empty and 'listing_exchange_vendor' in frame}
        status.update({'decision_session':str(asof),'source':meta,'cash_rate_audit':meta.get('cash_rate_audit')})
        # Export only actual model inputs; publication failure must not
        # alter original model decisions. The challenger blocks without it.
        try:
            status['shared_input']=publish_shared_inputs(
                FORWARD,asof,vintages,frames,m.cash_rate,meta,now.isoformat())
            status['shared_input_status']='PUBLISHED_LOSSLESS'
        except Exception as shared_error:
            status['shared_input_status']='FAILED'
            status['shared_input_error']=str(shared_error)[:1000]
        # Daily EUR mark-to-market quotes for the *persisted* PAPER ledger.
        # As-of equals the latest complete US benchmark session; for local
        # exchange holidays a prior close may be carried explicitly.
        raw_close=m.raw.close_eur.loc[pd.Timestamp(asof)]
        good_quotes={ticker:round(float(value),8) for ticker,value in raw_close.items()
                     if pd.notna(value) and math.isfinite(float(value)) and float(value)>0}
        write_json(FORWARD/'latest_quotes.json',{
            'decision_session':str(asof),'recorded_at_utc':now.isoformat(),
            'price_model':'RAW_UNADJUSTED_EUR_LAST_COMPLETED_PER_EXCHANGE_CLOSE',
            'close_eur':good_quotes,
            'ticker_price_dates':{key:date for key,date in
                meta.get('ticker_price_dates',{}).items() if key in good_quotes},
            'fx_currency_source_sessions':meta.get('fx_currency_source_sessions',{}),
            'fx_units_per_eur_asof':meta.get('fx_units_per_eur_asof',{}),
            'fx_rate_source':meta.get('fx_rate_source'),
            'exchange_local_eod_enforced':meta.get('exchange_local_eod_enforced',False),
            'local_exchange_session_may_be_prior_date':True,
            'not_executable_intraday_quotes':True})
        status['data_universe']=meta.get('universe_kind')
        status['fx_audit']={
           'quote_currency_source':'RECORDED_VENDOR_LISTING_DENOMINATION',
           'fx_rates':meta.get('fx_units_per_eur_asof',{}),
           'fx_quote_sessions':meta.get('fx_currency_source_sessions',{}),
           'independent_ecb_is_audit_only':True,
           'retrospective_historical_pit_fx_claimed':False}
        status['exchange_local_eod_enforced']=meta.get('exchange_local_eod_enforced',False)
        status['signal_engine_status']='SIGNAL_READY'
        status['signal_engine_error']=None
        evidence=preview(m,meta,asof,now,old.get('orders',[]))
        status['next_preopen']=evidence
        status['signal_status']=evidence.get('status')
        status['signal_published']=bool(evidence.get('signal_published',True))
        status['per_symbol_execution_plan']=evidence.get('per_symbol_exchange_plan',{})
        # Safe immediate end-to-end ORDER-PLANNER probe at the verified close:
        # no fictitious fill, no write to the cumulative paper order ledger.
        try:
            from model_proposal import proposal
            # Freeze the actual ledger holdings at this decision close. A
            # target weight is NOT an order for the whole target amount.
            live_positions=positions_asof(old.get('orders',[]),asof)
            close_prices=m.raw.close_eur.loc[pd.Timestamp(asof)]
            ledger_cash=float(old.get('cash_eur',INITIAL))
            current_nav=ledger_cash
            for ticker,pos in live_positions.items():
                if ticker not in close_prices or pd.isna(close_prices[ticker]):
                    raise ValueError('Existing holding missing latest valuation: '+ticker)
                current_nav+=pos.shares*float(close_prices[ticker])
            indicative=proposal(m,meta,asof,positions=live_positions,nav_eur=current_nav)
            write_json(FORWARD/'proposal_last_close.json',indicative)
            # Immutable as-recorded decisions: a Monday cannot disappear when
            # Tuesday's daily proposal replaces latest_status/proposal files.
            # Future weekly reviews consume these preserved source decisions.
            dated_path=FORWARD/'proposals_by_session'/(str(asof)+'.json')
            if not dated_path.is_file():
                write_json(dated_path,{**indicative,
                    'first_recorded_at_utc':now.isoformat(),
                    'first_signal_sha256':evidence.get('decision_model_sha256'),
                    'universe_snapshot_recorded_date':meta.get('roster_recorded_date'),
                    'as_recorded_no_retroactive_revisions':True})
            else:
                prior_proposal=json.loads(dated_path.read_text())
                if prior_proposal.get('possible_candidates')!=indicative.get('possible_candidates'):
                    status['proposal_revised_after_recording']=True
                    status['immutable_historical_proposal_preserved']=str(asof)
            status['indicative_order_test']={
                'status':indicative['status'],
                'possible_buys_meeting_threshold':indicative['possible_buys_meeting_threshold'],
                'decision_session':indicative['decision_session'],
                'actual_fills':0}
        except Exception as propose_error:
            status['indicative_order_test']={'status':'PROPOSAL_CALCULATION_PENDING',
                'reason':str(propose_error)[:300],'actual_fills':0}

        # After launch the frozen-signal approximation owns the immutable
        # ledger. The exact historical replay remains a diagnostics-only path
        # and must never overwrite already-booked proxy fills.
        if old.get('execution_adapter')=='FROZEN_ORIGINAL_SIGNAL_PREVIOUS_COMPLETED_CLOSE_PROXY':
            result=None
            operation={'status':'ORIGINAL_MODEL_UPDATED_PAPER_ACCOUNT_SEPARATE',
                       'reason':'Paper ledger advanced by independent proxy account step'}
        else:
            result,operation=evaluate_model(m,meta)
        status['model_run']=operation
        if result is None:
            status['status']=('PAPER_APPROX_LEDGER_HANDOFF' if
                old.get('execution_adapter')=='FROZEN_ORIGINAL_SIGNAL_PREVIOUS_COMPLETED_CLOSE_PROXY'
                else 'WAITING_FIRST_ACTUAL_EXECUTION_SESSION')
        else:
            rows=serialize(result)
            fresh,recovered_dates=order_timecheck(rows,old.get('orders',[]),m,now,asof)
            # Validate *every* historical sale including SPY risk-sleeve sells.
            if len(result.realized_lots):
                min_sold_days=int(result.realized_lots['calendar_days'].min())
                if min_sold_days<35:
                    raise ValueError(f'ALERT: realized a lot younger than 35 calendar days: {min_sold_days}')
            else:min_sold_days=None
            portfolio=to_portfolio(result,m,meta)
            if len(rows)!=portfolio['orders_total']:
                raise ValueError('Order ledger and paper holdings snapshot mismatch')
            status.update({'status':'PAPER_LEDGER_VERIFIED','orders_cumulative':len(rows),
                           'new_orders':len(fresh),'recovered_paper_decision_dates':recovered_dates,
                           'recovered_paper_decision_count':len(recovered_dates),
                           'new_orders_mode':'HISTORICAL_PAPER_BACKFILL' if recovered_dates else 'ATTESTED_PAPER',
                           'nav_eur':round(float(result.equity.iloc[-1]),2),
                           'max_drawdown_pct':round(float((result.equity/result.equity.cummax()-1).min()*100),3),
                           'last_valuation_session':str(result.equity.index[-1].date()),
                           'tax_eur_total':round(float(result.total_tax_eur),2),
                           'fees_eur_total':round(float(result.total_fees_eur),2),
                           'estimated_slippage_eur':round(float(result.estimated_slippage_eur),2),
                           'model_state_input_timestamp_validated':True,
                           'minimum_realized_lot_age_days':min_sold_days,
                           'min_hold_calendar_days':35,
                           'blocked_or_partial_sell_requests':int(result.blocked_sell_count),
                           'cash_eur':portfolio['cash_eur'],
                           'invested_eur':portfolio['invested_eur'],
                           'positions_count':portfolio['positions_count'],
                           'holdings_summary':[{'ticker':p['ticker'],'market_value_eur':p['market_value_eur']}
                                               for p in portfolio['holdings']]})
            save={'schema_version':1,'engine_id':ENGINE,'portfolio':'EXACT_V4_ORIGINAL_SOURCE_PAPER',
                  'initial_cash_eur':INITIAL,'orders':rows,'latest_nav_eur':status['nav_eur'],
                  'last_valuation_session':status['last_valuation_session'],
                  'universe_lock_recorded_date':locked_roster_date,
                  'universe_latest_recorded_date':str(vintage[0]),
                  'universe_policy':'DAILY_CAUSAL_AS_RECORDED_ROSTERS_WITH_GAP_REPLAY',
                  'cash_and_positions':'computed_by_frozen_simulator_not_manual_broker',
                  'real_money_authorized':False}
            write_json(STATE,save)
            write_json(prior_portfolio_path,portfolio)
            daily=FORWARD/'equity.csv'
            data='date,net_liquidation_nav_eur\n'+'\n'.join(
                f'{day.date()},{val:.4f}' for day,val in result.equity.items())+'\n'
            daily.write_text(data,encoding='utf8')
            cols=result.orders.columns.tolist()
            if len(cols):result.orders.to_csv(FORWARD/'orders.csv',index=False)
    except Exception as exc:
        status.update({'status':'PENDING_EXACT_DATA_OR_PARITY','reason':str(exc)[:2000],
                       'signal_engine_status':'FAILED',
                       'signal_engine_error':str(exc)[:2000],
                       'no_new_official_orders':True})
        if os.environ.get('ATLAS_EXACT_DEBUG')=='1':traceback.print_exc()
    # Persist a readable status even before the first actual trade/valuation.
    # An unsuccessful run never changes the last verified position ledger.
    status.setdefault('orders_cumulative',len(old.get('orders',[])))
    status.setdefault('new_orders',0)
    status.setdefault('min_hold_calendar_days',35)
    status.setdefault('nav_eur',portfolio.get('nav_net_liquidation_eur',10000.))
    status['portfolio_report_session']=portfolio.get('valuation_session')
    if not STATE.is_file():
        write_json(STATE,{'schema_version':1,'engine_id':ENGINE,'portfolio':'EXACT_V4_ORIGINAL_35DAY_PAPER',
                          'initial_cash_eur':INITIAL,'orders':[],'latest_nav_eur':10000.,
                          'last_valuation_session':None,'real_money_authorized':False,
                          'universe_lock_recorded_date':status.get('universe_lock_recorded_date')})
    # Persist the chosen source vintage even in an initial cash-only run.
    if status.get('universe_lock_recorded_date') and not old.get('universe_lock_recorded_date'):
        locked_state=json.loads(STATE.read_text())
        locked_state['universe_lock_recorded_date']=status['universe_lock_recorded_date']
        write_json(STATE,locked_state)
    if not prior_portfolio_path.is_file():write_json(prior_portfolio_path,portfolio)
    write_json(STATUS,status)
    write_report(FORWARD,status,portfolio,
                 json.loads(STATE.read_text()).get('orders',[]))
    print(json.dumps({k:status.get(k) for k in ('status','reason','orders_cumulative','new_orders','decision_session','nav_eur')},ensure_ascii=False))
    return status

if __name__=='__main__':
    report=main()
    if report.get('signal_engine_status')!='SIGNAL_READY':
        raise SystemExit(1)
