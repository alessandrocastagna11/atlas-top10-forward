"""Atlas v0.8 SHADOW paper engine, NOT the production v0.7 ledger.

Frozen decisions from completed prior calendar months, next-session OPEN fills, per-
security calendars, prior-day FX for executions, independent cash/tax accounting.
Sources are Yahoo public data (not exchange-certified). Never routes real orders.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta, datetime, timezone
from math import sqrt, isfinite
from statistics import stdev
from zoneinfo import ZoneInfo
from urllib.parse import quote
import copy, hashlib, json, requests, time

FIRST_DAY = '2026-11-05'  # after v0.8 rules frozen on Nov 4; NO historical re-entry
START = '2026-11-04'       # seed NAV = 10k at prior close; no earlier backfill
BASKET = ['AAPL','NVDA','MSFT','GOOG','AMZN','TSM','SPCX','2222.SR','TSLA','META']
CURRENCIES = {s:'SAR' if s=='2222.SR' else 'USD' for s in BASKET}
SYMBOLS = BASKET + ['IUSQ.DE','EURUSD=X','EURSAR=X']
SECURITY_IDS = {s:{'issuer_key':{'AAPL':'APPLE_US','NVDA':'NVIDIA_US','MSFT':'MICROSOFT_US','GOOG':'ALPHABET_US','AMZN':'AMAZON_US','TSM':'TSMC_TW_ADR','SPCX':'SPACEX_US_2026','2222.SR':'SAUDI_ARAMCO_SA','TSLA':'TESLA_US','META':'META_US'}[s], 'currency':CURRENCIES[s], 'venue':'TADAWUL' if s=='2222.SR' else 'NASDAQ_OR_NYSE'} for s in BASKET}
SECURITY_IDS['SPCX']['isin']='US84615Q1031'; SECURITY_IDS['SPCX']['first_listed']='2026-06-12'
BENCH='IUSQ.DE'
FIXED_FEE=1.; SLIP=.001; CG_TAX=.26; DIV_TAX=.26; BOLLO=.002
VENUE_FEE={'2222.SR':.00155}  # published Saudi Exchange equity total trading commission rate, approximate
STRATEGIES=('MA10_Open','MA6_MA12_Open','Adaptive_Risk')


def seed(strategy):
    if strategy not in STRATEGIES: raise ValueError('unknown strategy')
    return dict(schema=8,strategy=strategy,seed_date=START,first_trade_eligible=FIRST_DAY,
        last_date=START,initialized=False,cash=10000.,positions={},tax_losses=[],
        bollo_liability=0.,dividend_tax=0.,benchmark=dict(shares=0.,cash=10000.,bollo=0.),
        last_signal_month='',target_exposure=0.,pending=[],orders=[],nav=[dict(date=START,nav=10000.,benchmark=10000.,return_pct=0.,benchmark_return_pct=0.,orders=0,cash=10000.,invested=0.,exposure=0.,status='SEED')],
        corporate_events=[],dividend_events=[],pending_roster=False,warnings=[],
        security_ids=copy.deepcopy(SECURITY_IDS),signal_log=[],canceled_tasks=[])


def retrieve_yahoo(symbol,start='2025-08-01',end='2026-11-05'):
    begin=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    endunix=int((datetime.fromisoformat(end).replace(tzinfo=timezone.utc)+timedelta(days=1)).timestamp())
    errors=[]
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            r=requests.get(f'https://{host}/v8/finance/chart/{quote(symbol,safe="")}',params={'period1':begin,'period2':endunix,'interval':'1d','events':'div,splits'},headers={'User-Agent':'Mozilla/5.0 AtlasPaperResearch/0.8'},timeout=18)
            r.raise_for_status();x=r.json()['chart'];
            if x.get('error'):raise ValueError(str(x['error']))
            data=x['result'][0];break
        except (requests.RequestException, KeyError, ValueError, TypeError, IndexError) as e:errors.append(str(e)[:160]);time.sleep(.25)
    else:raise ValueError(f'NO_DATA_{symbol}: {errors}')
    meta=data['meta'];tz=ZoneInfo(meta.get('exchangeTimezoneName') or 'UTC')
    raw=data['indicators']['quote'][0]
    timestamps=data.get('timestamp') or []
    bars={}
    for i,t in enumerate(timestamps):
        op=(raw.get('open') or [])[i] if i<len(raw.get('open') or []) else None
        cl=(raw.get('close') or [])[i] if i<len(raw.get('close') or []) else None
        if all(isinstance(q,(int,float)) and isfinite(q) and q>0 for q in (op,cl)):
            d=datetime.fromtimestamp(t,tz=tz).date().isoformat()
            bars[d]={'open':float(op),'close':float(cl),'volume':(raw.get('volume') or [None]*len(timestamps))[i]}
    spl={}
    for y in (data.get('events') or {}).get('splits',{}).values():
        d=datetime.fromtimestamp(y['date'],tz=tz).date().isoformat();factor=float(y.get('numerator',0))/float(y.get('denominator') or 1)
        if factor<=0:raise ValueError(f'Invalid split {symbol} {d}')
        spl[d]=factor
    dividends={}
    for y in (data.get('events') or {}).get('dividends',{}).values():
        d=datetime.fromtimestamp(y['date'],tz=tz).date().isoformat()
        dividends[d]=dividends.get(d,0.)+float(y['amount'])
    # Yahoo quote OHLC may be historically split adjusted. Restore nominal price
    # of the period before later splits; dividends are NOT price-adjusted here.
    for d,v in bars.items():
        subsequent=[factor for sd,factor in spl.items() if sd>d]
        f=1.
        for ratio in subsequent:f*=ratio
        v['open']*=f;v['close']*=f
    ident=meta.get('longName') or meta.get('shortName') or ''
    if symbol=='SPCX' and ('space' not in ident.lower() and 'exploration' not in ident.lower()):
        raise ValueError(f'SPCX_IDENTITY_MISMATCH: {ident!r}')
    if symbol=='SPCX':bars={d:v for d,v in bars.items() if d>='2026-06-12'}
    if symbol in CURRENCIES and meta.get('currency')!=CURRENCIES[symbol]:
        raise ValueError(f'WRONG_CURRENCY {symbol}: {meta.get("currency")} != {CURRENCIES[symbol]}')
    if symbol==BENCH and meta.get('currency')!='EUR':raise ValueError('Benchmark IUSQ not EUR-quoted')
    return dict(bars=bars,splits=spl,dividends=dividends,currency=meta.get('currency'),identity=ident,
                feed='Yahoo chart 1d',exchange_timezone=meta.get('exchangeTimezoneName'),retrieved_at_utc=datetime.now(timezone.utc).isoformat())


def quote_last(data,sym,asof,max_age=7,strict_before=False):
    bars=data[sym]['bars']
    ds=[d for d in bars if d<asof] if strict_before else [d for d in bars if d<=asof]
    if not ds:raise ValueError(f'MISSING_QUOTE {sym} before {asof}')
    d=max(ds)
    if (date.fromisoformat(asof)-date.fromisoformat(d)).days>max_age:raise ValueError(f'STALE_QUOTE {sym} {d} vs {asof}')
    return d,bars[d]


def fx(data,cur,day,for_open):
    if cur=='EUR':return (1.,day)
    ticker={'USD':'EURUSD=X','SAR':'EURSAR=X'}[cur]
    dated,q=quote_last(data,ticker,day,4,strict_before=for_open)
    return 1/float(q['close']),dated


def last_full_months(data,asof):
    m={}
    for day,row in data[BENCH]['bars'].items():
        if day[:7]<asof[:7] and day<=asof:
            if day[:7] not in m or day>m[day[:7]][0]:m[day[:7]]=(day,float(row['close']))
    return [(v[0],v[1]) for k,v in sorted(m.items())]


def trailing_vol(data,asof,window=20):
    past=[(d,float(q['close'])) for d,q in sorted(data[BENCH]['bars'].items()) if d<asof]
    if len(past)<window+1:raise ValueError('PENDING_VOL_HISTORY')
    r=[past[i][1]/past[i-1][1]-1 for i in range(len(past)-window,len(past))]
    return stdev(r)*sqrt(252)


def target_risk(strategy,data,asof,prior_risk=False):
    months=last_full_months(data,asof)
    if len(months)<12:raise ValueError('PENDING_12_MONTH_SIGNAL_HISTORY')
    closes=[v for _,v in months]
    ma10=sum(closes[-10:])/10;ma6=sum(closes[-6:])/6;ma12=sum(closes[-12:])/12
    last=closes[-1]
    if strategy=='MA10_Open': metric,slow=last,ma10
    else:metric,slow=ma6,ma12
    active= prior_risk
    if metric>1.01*slow:active=True
    elif metric<.99*slow:active=False
    vol=trailing_vol(data,asof) if strategy=='Adaptive_Risk' else None
    if strategy=='Adaptive_Risk':
        ma3=sum(closes[-3:])/3
        confirm=last>=ma3
        scale=max(.25,min(1.,.16/max(vol,1e-9)))
        frac=(scale if confirm else .5*scale) if active else 0.
    else:
        ma3=None;confirm=None;scale=None
        frac=1. if active else 0.
    return {'month':asof[:7],'signal_month':months[-1][0][:7], 'metric':metric,'slow':slow,
            'ma3':ma3,'vol20_annual':vol,'fast_confirm':confirm,'vol_scale':scale,
            'risk_on':active,'exposure':round(frac,9), 'decision_asof':months[-1][0]}


def trim_losses(losses,year):return [x for x in losses if x['expires']>=year and x['amount']>1e-9]


def sale_tax(state,gain,year):
    losses=trim_losses(state['tax_losses'],year)
    if gain<0:
        losses.append({'amount':-gain,'expires':year+4});state['tax_losses']=losses;return 0.
    due=gain
    for lot in sorted(losses,key=lambda x:x['expires']):
        use=min(due,lot['amount']);lot['amount']-=use;due-=use
    state['tax_losses']=trim_losses(losses,year)
    return max(0.,due)*CG_TAX


def value_positions(state,data,day):
    v={}
    for sym,p in state['positions'].items():
        dated,bar=quote_last(data,sym,day)
        conv,_=fx(data,CURRENCIES[sym],day,for_open=False)
        v[sym]=p['shares']*bar['close']*conv
    return v


def exposure_tasks(state,data,day,signal,roster=BASKET):
    """Create tasks from PREVIOUS close; DO NOT use today's OPEN/CLOSE."""
    prev=state['last_date']
    vals=value_positions(state,data,prev) if state['positions'] else {}
    equity=sum(vals.values());nav=state['cash']+equity-state['bollo_liability']
    desired=nav*signal['exposure']
    tasks=[]
    outgoing=set(vals)-set(roster)
    for sym in sorted(outgoing):tasks.append(dict(symbol=sym,side='SELL',full=True,budget_eur=vals[sym],created=day,reason='year-end roster removal'))
    ongoing={k:v for k,v in vals.items() if k in roster}
    current=sum(ongoing.values())
    newcomers=sorted(set(roster)-set(vals))
    newcomer_budget=desired*len(newcomers)/len(roster) if desired>1 and roster else 0.
    incumbent_budget=max(0.,desired-newcomer_budget)
    if incumbent_budget<=1e-8:
        for sym,val in ongoing.items():tasks.append(dict(symbol=sym,side='SELL',full=True,budget_eur=val,created=day,reason='risk-off or portfolio exit'))
    elif current>1e-8:
        # No rebalance of continuing winners if exposure & membership unchanged.
        scale=incumbent_budget/current
        for sym,val in ongoing.items():
            delta=val*(scale-1)
            if abs(delta)>1.:tasks.append(dict(symbol=sym,side='BUY' if delta>0 else 'SELL',full=False,budget_eur=abs(delta),created=day,reason='monthly risk scale'))
    if newcomer_budget>len(newcomers)*1.01 and newcomers:
        for sym in newcomers:
            tasks.append(dict(symbol=sym,side='BUY',full=False,budget_eur=newcomer_budget/len(newcomers),created=day,reason='initial or annual entrant'))
    return tasks


def fill_tasks(state,data,day):
    orders=[];pending=[]
    # SELL first: cash from exits can fund newcomers on another exchange calendar.
    for task in sorted(state['pending'],key=lambda p:(p['side']!='SELL',p['symbol'])):
        sym=task['symbol'];q=data[sym]['bars'].get(day)
        if q is None:
            pending.append(task);continue
        conv,fxdate=fx(data,CURRENCIES[sym],day,for_open=True)
        px=q['open'];exec_px=px*(1-SLIP if task['side']=='SELL' else 1+SLIP)
        venuefee=VENUE_FEE.get(sym,0.)
        if task['side']=='SELL':
            position=state['positions'].get(sym)
            if not position:continue
            shares=(position['shares'] if task['full'] else min(position['shares'],task['budget_eur']/(px*conv)))
            gross=shares*px*conv
            if gross<=FIXED_FEE:continue
            proceeds=shares*exec_px*conv - FIXED_FEE-gross*venuefee
            part=shares/position['shares'];basis=position['basis']*part
            gain=proceeds-basis;tax=sale_tax(state,gain,int(day[:4]))
            state['cash']+=proceeds-tax
            if shares>=position['shares']-1e-10:del state['positions'][sym]
            else:position['shares']-=shares;position['basis']-=basis
        else:
            available=min(float(task['budget_eur']),state['cash'])
            if available<=FIXED_FEE+1:pending.append(task);continue
            shares=(available-FIXED_FEE)/(exec_px*conv+px*conv*venuefee)
            if shares<=0:pending.append(task);continue
            gross=shares*px*conv
            expense=shares*exec_px*conv+FIXED_FEE+gross*venuefee
            if expense>state['cash']+1e-7:raise AssertionError('overspend')
            state['cash']-=expense
            pos=state['positions'].setdefault(sym,{'shares':0.,'basis':0.})
            pos['shares']+=shares;pos['basis']+=expense;proceeds=0.;gain=0.;tax=0.
        state['cash']=max(0.,state['cash'])
        orders.append(dict(date=day,symbol=sym,side=task['side'],shares=shares,raw_open_local=px,
            fill_price_local=exec_px,currency=CURRENCIES[sym],eur_per_local=conv,fx_asof=fxdate,
            quote_source=data[sym]['feed'],commission_eur=FIXED_FEE,slippage_eur=shares*px*conv*SLIP,
            venue_fee_eur=gross*venuefee,net_cash_eur=proceeds if task['side']=='SELL' else -expense,
            realized_gain_eur=gain,tax_eur=tax,reason=task['reason'],issued_at=task['created']))
    state['pending']=pending
    state['orders'].extend(orders)
    return orders


def apply_events(state,data,day):
    # Events before opening: no dividends on shares bought the same date.
    for sym,pos in list(state['positions'].items()):
        if day in data[sym].get('splits',{}):
            ratio=data[sym]['splits'][day]
            before=pos['shares'];pos['shares']*=ratio
            state['corporate_events'].append(dict(date=day,symbol=sym,type='SPLIT',ratio=ratio,shares_before=before,shares_after=pos['shares']))
        if day in data[sym].get('dividends',{}):
            gross=pos['shares']*data[sym]['dividends'][day]*fx(data,CURRENCIES[sym],day,for_open=True)[0]
            state['cash']+=gross*(1-DIV_TAX);state['dividend_tax']+=gross*DIV_TAX
            state['dividend_events'].append(dict(date=day,symbol=sym,gross=gross,tax=gross*DIV_TAX,net=gross*(1-DIV_TAX),note='Ex-date credit proxy; pay date/foreign withholding unmodeled'))
    if state['benchmark']['shares'] and day in data[BENCH].get('splits',{}):
        ratio=data[BENCH]['splits'][day];state['benchmark']['shares']*=ratio
        state['corporate_events'].append(dict(date=day,symbol=BENCH,type='BENCHMARK_SPLIT',ratio=ratio))


def benchmark_entry(state,data,day):
    bench=state['benchmark']
    if bench['shares'] or day<FIRST_DAY:return
    bar=data[BENCH]['bars'].get(day)
    if not bar:return
    budget=bench['cash'];p=bar['open']*(1+SLIP)
    bench['shares']=(budget-FIXED_FEE)/p
    bench['cash']=0.


def advance(state,data,day,roster=BASKET,roster_verified=True):
    """Apply one market day atomically by caller deepcopy. No backfilled trades."""
    if day<=state['last_date']:return None
    if not any(day in data[s]['bars'] for s in BASKET+[BENCH]):return None
    # Historical EOD valuation requires previous closes; no future OHLC in decisions.
    prev=state['last_date']
    prevvalue=sum(value_positions(state,data,prev).values()) if state['positions'] else 0.
    elapsed=(date.fromisoformat(day)-date.fromisoformat(prev)).days
    state['bollo_liability']+=prevvalue*BOLLO*elapsed/365.25
    bb=state['benchmark']
    if bb['shares']:
        _,pb=quote_last(data,BENCH,prev)
        bb['bollo']+=bb['shares']*pb['close']*BOLLO*elapsed/365.25
    # A decision is constructed BEFORE today's market opens and before any
    # split/ex-date adjustment, solely on positions and prices at the PRIOR close.
    if day>=FIRST_DAY and state['last_signal_month']!=day[:7]:
        if day>='2027-02-01' and not roster_verified:
            state['pending_roster']=True
            if 'YEAR_END_ROSTER_UNVERIFIED' not in state['warnings']:
                state['warnings'].append('YEAR_END_ROSTER_UNVERIFIED')
            # Keep old exposure and update mark-to-market only; no new transactions.
            state['pending']=[]
        else:
            signal=target_risk(state['strategy'],data,day,prior_risk=bool(state['target_exposure']))
            state['target_exposure']=signal['exposure']
            state['signal_log'].append(signal)
            state['last_signal_month']=day[:7]
            if state['pending']:
                state['canceled_tasks'].extend([{**p,'canceled':day,'reason_cancel':'new signal'} for p in state['pending']])
            state['pending']=exposure_tasks(state,data,day,signal,roster)
            state['initialized']=True
    # Split occurs BEFORE market open. The pending share budget was calculated
    # at the previous close and is unaffected by the post-split share count.
    apply_events(state,data,day)
    if day>=FIRST_DAY:
        benchmark_entry(state,data,day)
    # Do not start the simulated stock portfolio before the comparable
    # benchmark has its first actual EUR session/open.
    if state['pending'] and state['benchmark']['shares']:
        fill_tasks(state,data,day)
    positions=value_positions(state,data,day) if state['positions'] else {}
    equity=sum(positions.values())
    _,benchclose=quote_last(data,BENCH,day)
    bn=bb['shares']*benchclose['close']+bb['cash']-bb['bollo']
    nav=state['cash']+equity-state['bollo_liability']
    if min(nav,bn)<-1e-7:raise ValueError('negative NAV')
    state['last_date']=day
    st='PENDING_ORDERS' if state['pending'] else 'COMPLETE'
    row=dict(date=day,nav=round(nav,4),benchmark=round(bn,4),return_pct=round(100*(nav/10000-1),5),
             benchmark_return_pct=round(100*(bn/10000-1),5),orders=len(state['orders']),
             cash=round(state['cash'],4),invested=round(equity,4),exposure=round(state['target_exposure'],5),status=st)
    state['nav'].append(row)
    return row


def validate(state):
    if abs(state['cash'])>1e12 or state['cash']<-1e-6:raise AssertionError('cash invalid')
    if len(state['orders'])!=len(set((o['date'],o['symbol'],o['side'],o['issued_at']) for o in state['orders'])):
        raise AssertionError('duplicate order id')
    if any(v['shares']<=0 or v['basis']<=0 for v in state['positions'].values()):raise AssertionError('bad position')
    if state['last_date']!=state['nav'][-1]['date']:raise AssertionError('last date mismatch')
    if any(o['fx_asof']>=o['date'] for o in state['orders']):raise AssertionError('FX lookahead')
    return True