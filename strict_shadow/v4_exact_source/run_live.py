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
from live_model import HERE,CACHE,LIVE,START_PAPER,ENGINE,INITIAL,SECTOR,latest_vintages,get_adjusted,build_model,evaluate_model
from live_model import v1,v2,t9,inc,frozen,LIVE
if str(LIVE) not in sys.path:sys.path.insert(0,str(LIVE))
from paper import earliest_open_utc
from paper_reporting import to_portfolio,initial_portfolio,write_report

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
    return {'checked_at_utc':now.isoformat(),'engine':'FROZEN_ATLAS_V4_CONFIDENCE70',
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


def symbol_next_open(symbol, first_date, not_before=None):
    """Exchange-local indicative open. Only an actual provider bar certifies a fill.

    Use the exchange-specific trading week: Saudi Riyadh Sunday-Thursday;
    all other currently supported exchanges Monday-Friday. Unlisted exchange
    holidays remain unverified and must be checked against the actual quote.
    """
    day=first_date if isinstance(first_date,dt.date) else dt.date.fromisoformat(str(first_date))
    for _ in range(15):
        trading_weekday=(day.weekday() in (6,0,1,2,3)
                         if str(symbol).upper().endswith('.SR') else day.weekday()<5)
        if trading_weekday:
            opening=earliest_open_utc([symbol],day)
            if not_before is None or opening>not_before:
                return day,opening
        day+=dt.timedelta(days=1)
    raise ValueError('No exchange opening in 15 calendar days: '+str(symbol))


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
        per_symbol[symbol]={
            'first_candidate_session':str(first),
            'first_candidate_open_utc':opening.isoformat(),
            'first_candidate_cutoff_utc':deadline.isoformat(),
            'attested_before_first_candidate_open':on_time,
            'status':'PENDING_FIRST_LOCAL_OPEN' if on_time else 'DEFERRED_PAST_FIRST_LOCAL_OPEN',
            'next_eligible_local_session':str(next_date),
            'next_eligible_open_utc':next_open.isoformat(),
            'execution_quote_required':True,
            'exchange_holiday_calendar_verified':False}
    tomorrow=asof+dt.timedelta(days=1)
    while tomorrow.weekday()>=5:tomorrow+=dt.timedelta(days=1)
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
                    item['attested_before_first_candidate_open']=published
                    item['original_proof_utc']=prior.get('produced_at_utc')
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
        base,fail=get_adjusted(['SPY'],candidate,max_workers=1)
        if 'SPY' not in base:raise ValueError('US EOD benchmark unavailable: '+str(fail))
        spy=base['SPY'];dates=[d.date() for d in spy.index if d.date()<=candidate]
        if not dates:raise ValueError('No completed benchmark history')
        asof=max(dates)
        if (candidate-asof).days>4:raise ValueError('Stale SPY benchmark')
        vintages=latest_vintages(asof)
        # Each immutable recorded vintage becomes active only from its own
        # capture date. Replaying after downtime uses that same dated history.
        # A newly captured 2026 roster never changes yesterday's membership.
        vintage=vintages[-1]
        locked_roster_date=str(vintages[0][0])  # legacy state only; NOT selection policy
        status['universe_lock_recorded_date']=locked_roster_date
        status['latest_available_roster_recorded_date']=str(vintage[0])
        status['universe_policy']='DAILY_CAUSAL_AS_RECORDED_ROSTERS_WITH_GAP_REPLAY'
        status['historical_universe_vintages']=len(vintages)
        all_constituents=[row for v in vintages for row in v[3]+v[4]]
        symbols=[str(x['symbol']).upper() for x in all_constituents]
        required=set(symbols)|set(SECTOR.values())|{'SPY','EURUSD=X'}
        required|={'EUR'+str(x['currency']).upper()+'=X' for x in all_constituents if str(x['currency']).upper()!='EUR'}
        frames,fail=get_adjusted(required,asof,max_workers=8)
        # Every chosen 2026 constituent is required; silently dropping a leader
        # would change H4 selection and misstate source equivalence.
        if fail:
            first=list(sorted(fail.items()))[:10]
            raise ValueError(f'Adjusted dataset incomplete ({len(fail)}/{len(required)}): '+str(first))
        m,meta=build_model(vintages,asof,frames)
        status.update({'decision_session':str(asof),'source':meta})
        evidence=preview(m,meta,asof,now,old.get('orders',[]))
        status['next_preopen']=evidence
        status['signal_status']=evidence.get('status')
        status['signal_published']=bool(evidence.get('signal_published',True))
        status['per_symbol_execution_plan']=evidence.get('per_symbol_exchange_plan',{})
        result,operation=evaluate_model(m,meta)
        status['model_run']=operation
        if result is None:
            status['status']='WAITING_FIRST_ACTUAL_EXECUTION_SESSION'
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

if __name__=='__main__':main()
