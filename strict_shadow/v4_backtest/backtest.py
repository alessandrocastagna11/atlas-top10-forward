#!/usr/bin/env python3
"""Atlas V4 current-cohort retrospective diagnostic; NOT PIT, NOT an investable historical strategy.
Uses the actual repository's forward_signal ranking/allocation and paper.execute_paper
with FIFO 35-day lots. It must never touch any official/paper portfolio state.
"""
from __future__ import annotations
import bisect,csv,datetime as dt,json,math,os,statistics,sys
from collections import Counter,defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'live_v4'))
import forward_signal as f
from dataset_daily import DATA,rows_load,symbol_filename
from paper import ENGINE,PaperBlocked,execute_paper,fresh_state

REB_FRAC=float(os.environ.get('ATLAS_REBALANCE_FRACTION','0.01'))
CADENCE=os.environ.get('ATLAS_SIGNAL_CADENCE','daily')
if not (0<=REB_FRAC<=.10):raise ValueError('Invalid rebalance fraction')
if CADENCE not in ('daily','weekly_friday'):raise ValueError('Invalid research cadence')
REPORT=Path(__file__).resolve().parent/('results' if REB_FRAC==.01 and CADENCE=='daily' else
    'results_{}pct_{}'.format(int(round(REB_FRAC*100)),CADENCE))
NY=ZoneInfo('America/New_York')
SNAPSHOT='2026-11-09'
START=dt.date(2025,3,3)
END=dt.date(2026,11,6)

def read_market(symbol):
    path=DATA/'prices'/symbol_filename(symbol)
    if not path.exists():return []
    arr=[]
    for row in rows_load(path):
        try:
            d=dt.date.fromisoformat(row['date'])
            op=float(row['open'])*float(row.get('unit_scale') or 1)
            cl=float(row['close'])*float(row.get('unit_scale') or 1)
            vol=float(row['volume'])
            if d<=END and op>0 and cl>0 and math.isfinite(op+cl):
                arr.append((d,cl,op,vol,row['currency'].upper()))
        except (ValueError,TypeError,KeyError):continue
    return sorted(arr)

def make_quotes(data):
    # Build as-of-only quote readers. Quoted "close" is NOT dividend-adjusted.
    byday={s:{x[0]:x for x in arr} for s,arr in data.items()}
    dated={s:[x[0] for x in arr] for s,arr in data.items()}
    def latest(s,date,field,exact=False,maxage=5):
        v=byday.get(s,{})
        if exact:
            return v.get(date)
        seq=dated.get(s,[])
        i=bisect.bisect_right(seq,date)-1
        if i<0:return None
        bar=v[seq[i]]
        return bar if (date-bar[0]).days<=maxage else None
    return latest,dated

def bootstrap():
    rosterfile=DATA/'universe/snapshots'/SNAPSHOT/'global500.csv'
    roster=rows_load(rosterfile)
    usa=[x for x in roster if x['country']=='United States'][:100]
    row=[x for x in roster if x['country']!='United States'][:200]
    chosen=usa+row
    symbols={x['symbol'] for x in chosen}|{'ACWI'}
    data={s:read_market(s) for s in symbols}
    fx,_=f.fx_cache()
    # Use the same FX panel as the live feature extractor; no FX interpolation.
    for currency in {x['currency'] for x in chosen}|{'USD'}:
        if currency!='EUR' and currency not in fx:raise RuntimeError('FX missing: '+currency)
    latest,dated=make_quotes(data)
    return chosen,usa,row,data,fx,latest,dated

def compute_signal(day,usa,row,fx,cache):
    # Same current-cohort selector, scoring, regional slots and weights as forward_signal.generate.
    b=f.prices('ACWI',day)[0]
    if len(b)<285:return None,'benchmark warmup'
    u=[];rw=[]
    for x in usa:
        y=f.feature_pack(x['symbol'],x,day,fx)
        if y:u.append(y)
    for x in row:
        y=f.feature_pack(x['symbol'],x,day,fx)
        if y:rw.append(y)
    if len(u)<20 or len(rw)<20:
        return None,'breadth USA {} ROW {}'.format(len(u),len(rw))
    scored=f.group_score(u)+f.group_score(rw)
    top_usa=sorted((x for x in scored if x['region']=='USA'),key=lambda x:x['score'],reverse=True)
    top_row=sorted((x for x in scored if x['region']=='ROW'),key=lambda x:x['score'],reverse=True)
    strength=f.cohort_strength(u,rw)
    slots=0 if strength>=0 else 2 if strength>=-.5 else 4 if strength>=-1 else 6
    picks=[];sects={}
    def add(x):
        if len(picks)>=10 or any(z['ticker']==x['ticker'] for z in picks) or sects.get(x['sector'],0)>=3:return False
        picks.append(x);sects[x['sector']]=sects.get(x['sector'],0)+1
        return True
    for pool,number in [(top_usa,10-slots),(top_row,slots)]:
        count=0
        for x in pool:
            if count>=number:break
            if add(x):count+=1
    if len(picks)<10:
        for x in sorted(top_usa+top_row,key=lambda x:x['score'],reverse=True):
            if len(picks)>=10:break
            add(x)
    if len(picks)<5:return None,'selection breadth'
    closes=[x[1] for x in b]
    regime=closes[-1]>sum(closes[-200:])/200 and sum(closes[-50:])/50>sum(closes[-200:])/200
    exposure=.8 if regime else .3
    weights=f.weighted_allocation(picks,exposure)
    targets=[{'ticker':x['ticker'],'market_symbol':x['market_symbol'],
              'region':x['region'],'sector':x['sector'],'weight':weights[x['ticker']]}
             for x in picks if x['ticker'] in weights]
    produced=dt.datetime.combine(day,dt.time(18,15),NY)
    exe=f.executable_session(day,produced,[x['market_symbol'] for x in targets])
    return {'engine_id':ENGINE,'mode':'SHADOW','decision_session':str(day),
            'execution_session':str(exe),'targets':targets,'risk_regime':'RISK_ON' if regime else 'DEFENSIVE_PROXY',
            'target_exposure':sum(x['weight'] for x in targets)},None

def compute_metrics(history,initial=10000):
    if not history:return None
    navs=[x[1] for x in history]
    first,last=history[0],history[-1]
    peak=initial;dd=0
    for v in navs:
        peak=max(peak,v)
        dd=min(dd,v/peak-1)
    days=(last[0]-first[0]).days
    cagr=(last[1]/initial)**(365.25/days)-1 if days>0 else 0.
    annual={}
    prev=initial
    for yr in sorted(set(x[0].year for x in history)):
        vals=[v for d,v in history if d.year==yr]
        annual[str(yr)]=round(100*(vals[-1]/prev-1),3)
        prev=vals[-1]
    return {'initial_eur':initial,'final_eur':round(last[1],2),
            'total_return_pct':round(100*(last[1]/initial-1),3),
            'cagr_pct':round(100*cagr,3),'max_drawdown_pct':round(100*dd,3),
            'annual_price_only_returns_pct':annual,'valuation_sessions':len(history),
            'first_valuation':str(first[0]),'last_valuation':str(last[0])}

def main():
    universe,usa,row,data,fx,market,dates=bootstrap()
    # Cache historical features as of each decision, never later.
    import functools
    @functools.lru_cache(maxsize=600)
    def prices_at(s,day):
        arr=data.get(s,[])
        days=dates.get(s,[])
        pos=bisect.bisect_right(days,day)
        return arr[:pos],None
    f.prices=prices_at
    benchmark=data.get('ACWI',[])
    days=[x[0] for x in benchmark if START<=x[0]<=END]
    if len(days)<100:raise RuntimeError('insufficient benchmark sessions')
    state=fresh_state()
    pending=None
    blocked=Counter()
    generated=0
    scheduled=0
    history=[]
    bnh=[]
    raw_signal=None
    exposure=[]
    for day in days:
        # Live signal is queued, one at a time. Use real next-session opening prices,
        # and FX closes from BEFORE the execution trading day.
        if pending and day>=dt.date.fromisoformat(pending['execution_session']):
            actualday=dt.date.fromisoformat(pending['execution_session'])
            names=set(state['holdings'])|{t['ticker'] for t in pending['targets']}
            quotes={}
            missing=[]
            for sym in names:
                exact=market(sym,actualday,'open',exact=True)
                if exact is None:missing.append(sym);continue
                curr=exact[4]
                closefx=f.fx_close(curr,actualday-dt.timedelta(days=1),fx)
                if not math.isfinite(closefx) or closefx<=0:missing.append(sym+' FX');continue
                quotes[sym]=exact[2]/closefx
            if missing:
                blocked['missing_execution_open_or_prior_fx']+=1
                pending=None # fail-closed; no invented fills or extra-day retroactive fills.
            elif day==actualday:
                try:
                    state,receipt=execute_paper(state,pending,quotes,rebalance_fraction=REB_FRAC)
                    scheduled+=1
                except (PaperBlocked,ValueError,KeyError) as exc:
                    blocked['execution_blocked_'+str(exc)[:90]]+=1
                pending=None
        # Mark EOD only when all currencies and prices have a prior or same-day close.
        if state['holdings']:
            total=state['cash_eur']
            missing_mark=False
            exposure_today=0
            for sym,pos in state['holdings'].items():
                bar=market(sym,day,'close',exact=False)
                if bar is None:missing_mark=True;break
                rate=f.fx_close(bar[4],bar[0],fx)
                if not math.isfinite(rate) or rate<=0:missing_mark=True;break
                total+=pos['shares']*bar[1]/rate
                exposure_today+=pos['shares']*bar[1]/rate
            if not missing_mark:
                history.append((day,total))
                exposure.append(exposure_today/total)
            else:
                blocked['missing_mark']+=1
        else:
            history.append((day,state['cash_eur']))
            exposure.append(0)
        acwi=market('ACWI',day,'close',exact=True)
        usd=f.fx_close('USD',day,fx)
        if acwi is not None and math.isfinite(usd) and usd>0:
            bnh.append((day,acwi[1]/usd))
        if pending:continue
        if CADENCE=='weekly_friday' and day.weekday()!=4:continue
        try:
            signal,why=compute_signal(day,usa,row,fx,data)
            if not signal:
                blocked['signal_'+str(why)[:70]]+=1
            elif dt.date.fromisoformat(signal['execution_session'])<=END:
                pending=signal
                generated+=1
            else:blocked['signal_after_last_session']+=1
        except (ValueError,KeyError,TypeError,OverflowError,ZeroDivisionError) as exc:
            blocked['signal_error_'+str(exc)[:90]]+=1

    if not history or not bnh:raise RuntimeError('empty valuations')
    # Align dates for benchmark and V4. Cash account starts at 10000 at first benchmark valuation.
    firstday=history[0][0];lastday=history[-1][0]
    bnh=[(d,x) for d,x in bnh if firstday<=d<=lastday]
    b0=bnh[0][1]
    benchmark_history=[(d,10000*x/b0) for d,x in bnh]
    portfolio=compute_metrics(history)
    bench=compute_metrics(benchmark_history)
    orders=state['orders']
    buys=sum(x['action']=='BUY' for x in orders)
    sells=len(orders)-buys
    result={
      'test_label':'EX_POST_CURRENT_2026_COHORT_DIAGNOSTIC_NOT_PIT_BACKTEST',
      'rebalance_fraction':REB_FRAC,'signal_cadence':CADENCE,
      'strategy':'V4_CONF70_FORWARD_COHORT_PROXY_V1 + updated paper.py FIFO lots',
      'date_window':{'start':str(firstday),'end':str(lastday)},
      'source_universe':'ACWI holdings frozen at 2026-11-06 vintage, recorded on 2026-11-09',
      'historical_membership_pit':False,'historical_fx_first_seen_pit':False,
      'survivorship_and_lookahead_risk':'HIGH: 2026 roster reused for earlier years',
      'execution_policy':'next first globally executable business opening; 30min pre-open cut',
      'trade_policy':f'35-day individual lots; 10 names; 15% issuer; 30% sector; 1% cash; EUR1/order; 10bp/side; 26% realized-gain tax estimate; {REB_FRAC*100:g}% NAV rebalance threshold; no monthly order cap',
      'portfolio':portfolio,'benchmark_ACWI_EUR_price_only':bench,
      'orders_total':len(orders),'buys':buys,'sells':sells,
      'fees_eur':round(sum(x['fee_eur'] for x in orders),2),
      'estimated_tax_eur':round(sum(x['tax_estimate_eur'] for x in orders),2),
      'approx_slippage_eur':round(sum(x['shares']*x.get('reference_open_eur',0)*.001 for x in orders),2),
      'final_cash_eur':round(state['cash_eur'],2),
      'final_holdings':len(state['holdings']),
      'avg_equity_exposure_pct':round(100*statistics.mean(exposure),2),
      'signals_generated':generated,'decisions_processed':scheduled,
      'blocked_and_skipped':dict(blocked),
      'num_roster_candidates':len(universe),
      'num_cached_candidate_symbols':sum(bool(data.get(x['symbol'])) for x in universe),
      'note':'Retrospective selection is NOT causal/out-of-sample. Price-only ACWI excludes dividends and portfolio prices use Yahoo close with no independently certified historical corporate-action PIT. No forward ledger changed.',
    }
    REPORT.mkdir(parents=True,exist_ok=True)
    (REPORT/'summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    with (REPORT/'daily_nav.csv').open('w',newline='',encoding='utf8') as h:
        w=csv.writer(h);w.writerow(['date','atlas_nav_eur','acwi_eur_price_only_nav'])
        benchmap=dict(benchmark_history)
        for d,v in history:w.writerow([d,round(v,4),round(benchmap[d],4) if d in benchmap else ''])
    with (REPORT/'orders.csv').open('w',newline='',encoding='utf8') as h:
        keys=['session','action','ticker','shares','execution_price_eur','fee_eur','tax_estimate_eur','cash_change_eur']
        w=csv.DictWriter(h,fieldnames=keys,extrasaction='ignore');w.writeheader();w.writerows(orders)
    p=portfolio;b=bench
    md=[
      '# Atlas V4 — historical current-cohort diagnostic (NOT PIT)',
      '',
      '**WARNING: this is NOT a verified historical point-in-time backtest.**',
      'It holds a 6 Nov 2026 ETF holdings roster fixed into 2025-2026.',
      'That introduces selection/survivorship and look-ahead bias; CAGR is not evidence of forward alpha.',
      '',
      '| Metric | V4 Cohort Proxy | ACWI buy-and-hold EUR (price-only) |',
      '|---|---:|---:|',
      f"| Window | {firstday} to {lastday} | Same |",
      f"| Start EUR | 10,000 | 10,000 |",
      f"| End EUR | {p['final_eur']:,.2f} | {b['final_eur']:,.2f} |",
      f"| Total return | {p['total_return_pct']}% | {b['total_return_pct']}% |",
      f"| CAGR | {p['cagr_pct']}% | {b['cagr_pct']}% |",
      f"| Max drawdown | {p['max_drawdown_pct']}% | {b['max_drawdown_pct']}% |",
      '',
      f"Orders: {len(orders)} ({buys} buys, {sells} sells), fees EUR {result['fees_eur']}, estimated realized taxes EUR {result['estimated_tax_eur']}, average exposure {result['avg_equity_exposure_pct']}%.",
      f"Signals generated: {generated}; processed: {scheduled}. Rebalance threshold {REB_FRAC*100:g}% NAV, decision cadence {CADENCE}.",
      '',
      'Blocked/skipped: '+json.dumps(dict(blocked),sort_keys=True),
      '',
      '**Limitations:** historical membership and first-seen FX vintage are not independently PIT; 2026 holdings selected retrospectively; no historical delisting complete accounting; dividend-unadjusted ACWI benchmark; no inference of real-world implementability. This does not reproduce the frozen historical original Confidence70.',
      'The only genuinely forward observed period begins November 2026.',
    ]
    (REPORT/'summary.md').write_text('\n'.join(md)+'\n',encoding='utf8')
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
