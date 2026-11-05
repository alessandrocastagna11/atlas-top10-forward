#!/usr/bin/env python3
"""Atlas V4 Confidence70 live PAPER runner. Fail-closed: no model snapshot, no trade."""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from paper import ENGINE, PaperBlocked, canonical_sha, execute_paper, fresh_state, mark, validate_state
from provider import DataUnavailable, YahooChart

ROOT=Path(__file__).resolve().parent
REPLAY_EQUITY_SHA='188e39bf2196296653c5985ddaed5d9eb548e3bc891972f549dcd4326fdfc43c'
REPLAY_ORDERS_SHA='57859b33f601d93566ea14dee70a69ebf80bdfacd650282f991e78071c2b7168'
CANONICAL_BUNDLE_SHA='fb1321e534d21059fc4f151e8027071106cc1899ef8b830a249ab61d6d8be2d3'
NY=ZoneInfo('America/New_York')

class SignalBlocked(PaperBlocked):pass

def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(obj,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(tmp,path)

def validate_signal(s,now):
    if s.get('engine_id')!=ENGINE or s.get('schema_version')!='3.0' or s.get('mode')!='SHADOW':
        raise SignalBlocked('wrong V4 signal schema/engine/mode')
    proof=s.get('provenance') or {}
    if proof.get('replay_equity_sha256')!=REPLAY_EQUITY_SHA or proof.get('replay_orders_sha256')!=REPLAY_ORDERS_SHA:
        raise SignalBlocked('replay hashes not matched; do not claim original parity')
    if proof.get('canonical_usa_bundle_sha256')!=CANONICAL_BUNDLE_SHA:raise SignalBlocked('canonical bundle mismatch')
    if proof.get('parity_status')!='REPLAY_DIFFERS_FROM_ORIGINAL':raise SignalBlocked('historical parity status must be explicit')
    if proof.get('model_name')!='Confidence70' or abs(float(proof.get('confidence_beta',-1))-0.7)>1e-10:
        raise SignalBlocked('wrong sizing profile')
    if proof.get('lookahead_audit') not in ('RESEARCH_ONLY','PIT_REVIEWED'):
        raise SignalBlocked('lookahead/provenance status absent')
    if s.get('authorization',{}).get('real_money_authorized') or s.get('authorization',{}).get('human_approved'):
        raise SignalBlocked('only shadow/paper allowed')
    dec=dt.date.fromisoformat(s['decision_session']); exe=dt.date.fromisoformat(s['execution_session'])
    today=now.astimezone(NY).date()
    if dec>today or exe<=dec or (today-dec).days>6:raise SignalBlocked('future/stale/out-of-order decision')
    freshness=s.get('data_freshness',{})
    for key in ('prices_as_of_session','scores_as_of_session','membership_as_of_session','fx_as_of_session'):
        if key not in freshness:raise SignalBlocked('missing '+key)
        when=dt.date.fromisoformat(freshness[key])
        if when>dec:raise SignalBlocked(key+' has future data')
        if (dec-when).days>7 and key!='membership_as_of_session':raise SignalBlocked(key+' is stale')
        if key=='membership_as_of_session' and (dec-when).days>45:raise SignalBlocked('stale monthly membership')
    ids=proof.get('live_source_sha256') or {}
    if not isinstance(ids,dict) or not all(isinstance(ids.get(k),str) and len(ids[k])==64 for k in ('prices','fx','universe','scores')):
        raise SignalBlocked('missing hashed live model inputs')
    if not proof.get('produced_by_engine_run',False):raise SignalBlocked('no verified model run recorded')
    from paper import validate_targets
    validate_targets(s)
    for p in s['targets']:
        if not p.get('market_symbol') or p.get('price_eur') is None:
            raise SignalBlocked('symbol map or decision close unavailable')
    return dec,exe

def last_complete_us_session(now,provider,symbol='ACWI'):
    ny=now.astimezone(NY)
    last=ny.date() if (ny.hour,ny.minute)>=(16,45) else ny.date()-dt.timedelta(days=1)
    bars=provider.history(symbol,last-dt.timedelta(days=13),last)
    eligible=[s for s in bars if s<=last]
    if not eligible:raise DataUnavailable('benchmark has no EOD data')
    session=max(eligible)
    if (last-session).days>4:raise DataUnavailable('benchmark quote stale')
    return session,bars[session]

def run(root=ROOT,now=None,fetch=True):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('timezone-aware now required')
    statefile=root/'state'/'portfolio.json'
    st=json.loads(statefile.read_text()) if statefile.exists() else fresh_state()
    validate_state(st)
    status={'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),'strategy':'V4_Confidence70',
            'engine_id':ENGINE,'mode':'PAPER','capital_initial_eur':st['initial_cash_eur'],
            'real_money_orders':0,'official_main_unchanged':True,
            'historical_replay_parity':'REPLAY_DIFFERS_FROM_ORIGINAL',
            'executed_decisions':len(st['decisions']),'total_paper_orders':len(st['orders'])}
    provider=YahooChart()
    last_session=None
    if fetch:
        try:
            last_session,bench=last_complete_us_session(now,provider)
            status['benchmark']={'symbol':'ACWI','session':str(last_session),'close_local':bench.close,
                                 'currency':bench.currency,'source_sha256':bench.source_sha256}
            status['market_data_status']='BENCHMARK_EOD_VERIFIED'
        except DataUnavailable as e:
            status['market_data_status']='PENDING_QUOTES'
            status['market_data_reason']=str(e)
    else:
        status['market_data_status']='NOT_FETCHED'
    signalfile=root/'inbox'/'latest_signal.json'
    status['status']='PENDING_SIGNAL'
    status['reason']='No fresh Confidence70 model-generated and hashed signal snapshot'
    if signalfile.exists():
        try:
            s=json.loads(signalfile.read_text())
            dec,exe=validate_signal(s,now)
            # The producer must attach actual source bytes; self-reported hashes alone are insufficient.
            for label,expected in s['provenance']['live_source_sha256'].items():
                if label not in ('prices','fx','universe','scores'):raise SignalBlocked('unexpected source name: '+label)
                source=root/'inbox'/'sources'/(label+'.csv')
                if not source.is_file():raise SignalBlocked('source evidence missing: '+str(source.name))
                if source.stat().st_size<20:raise SignalBlocked('empty source evidence: '+label)
                if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
                    raise SignalBlocked('source evidence hash mismatch: '+label)
            if last_session is None:raise DataUnavailable('complete benchmark EOD not verified')
            if dec>last_session:raise DataUnavailable('decision after last complete US close')
            if exe>now.astimezone(NY).date():raise DataUnavailable('execution session not yet started')
            if exe==now.astimezone(NY).date() and now.astimezone(NY).hour<10:raise DataUnavailable('wait until 10:00 NY for next-open quote availability')
            if st['last_execution_session'] and exe<=dt.date.fromisoformat(st['last_execution_session']):
                raise PaperBlocked('duplicate/out-of-order execution: state never overwritten')
            names={p['ticker']:p['market_symbol'] for p in s['targets']}
            for t,p in st['holdings'].items():
                if t in names and names[t]!=p['market_symbol']:raise PaperBlocked('ticker-to-issuer mapping changed: '+t)
                names[t]=p['market_symbol']
            quote_metadata={t:provider.eur_quote(sym,exe,'open') for t,sym in names.items()}
            close_meta={p['ticker']:provider.eur_quote(p['market_symbol'],dec,'close') for p in s['targets']}
            for target in s['targets']:
                t=target['ticker']; model=float(target['price_eur']); actual=close_meta[t]['price_eur']
                if model<=0 or abs(actual/model-1)>0.05:raise DataUnavailable('model/provider close divergence >5%: '+t)
            new,execution=execute_paper(st,s,{t:m['price_eur'] for t,m in quote_metadata.items()})
            write_json(root/'market_data'/'execution_receipt.json',{
                'session':str(exe),'data':quote_metadata,'model_close_receipts':close_meta,
                'signal_sha256':canonical_sha(s)})
            write_json(statefile,new)
            st=new
            status.update({'status':'PAPER_EXECUTED','reason':'next-open quotes checked; paper only',
                           'execution':execution,'executed_decisions':len(st['decisions']),
                           'total_paper_orders':len(st['orders'])})
        except (SignalBlocked,PaperBlocked,KeyError,TypeError,ValueError,json.JSONDecodeError,DataUnavailable) as e:
            status.update({'status':'PENDING_DATA' if isinstance(e,DataUnavailable) else 'BLOCKED_SIGNAL',
                           'reason':str(e)})
    # Independent mark-to-market: only identical close session for all held positions
    if st['holdings'] and last_session:
        try:
            marks={t:provider.eur_quote(p['market_symbol'],last_session,'close') for t,p in st['holdings'].items()}
            status['valuation']=mark(st,{t:r['price_eur'] for t,r in marks.items()},last_session)
            write_json(root/'market_data'/'valuation_receipt.json',{'session':str(last_session),'quotes':marks})
        except (DataUnavailable,PaperBlocked) as e:
            status['valuation_status']='PENDING_DATA';status['valuation_reason']=str(e)
    elif not st['holdings']:
        status['valuation']={'nav_eur':round(st['cash_eur'],2),'cash_eur':round(st['cash_eur'],2),
                             'positions':0,'valuation_type':'CASH_ONLY'}
    status['state_sha256']=canonical_sha(st)
    write_json(root/'reports'/'latest_status.json',status)
    write_json(root/'state'/'portfolio.json',st)
    out=root/'reports'/'orders.csv';out.parent.mkdir(parents=True,exist_ok=True)
    fields=['session','action','ticker','market_symbol','shares','reference_open_eur','execution_price_eur',
            'fee_eur','tax_estimate_eur','cash_change_eur','signal_sha256','mode']
    with out.open('w',newline='',encoding='utf-8') as fh:
        writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader();writer.writerows(st['orders'])
    lines=['# Atlas V4 Confidence70 — Live paper','',f"**Stato:** {status['status']}",'',
           f"**Motivo:** {status['reason']}",'',
           f"Capitale iniziale virtuale: €{st['initial_cash_eur']:,.2f}",
           f"Ordini virtuali cumulati: {len(st['orders'])}",
           f"Sedute eseguite: {len(st['decisions'])}",
           f"Dati benchmark: {status['market_data_status']}",
           '', 'Nessun ordine reale; main ufficiale invariato.']
    (root/'reports'/'latest.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(status,ensure_ascii=False,sort_keys=True))
    return status

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');p=parser.parse_args()
    if p.check:
        stfile=ROOT/'state'/'portfolio.json'
        st=json.loads(stfile.read_text()) if stfile.exists() else fresh_state()
        validate_state(st);print('PAPER_LEDGER_OK',len(st['orders']))
    else:run()
